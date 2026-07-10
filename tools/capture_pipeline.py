"""Photo -> bibliography pipeline for phone-captured book pages.

The Android capture app uploads lightly-compressed photos of title/copyright
pages to the cloud (Supabase); this module turns one capture's photos into a
book record on the desktop side:

  1. perspective_correct : find the page quadrilateral and warp it flat (cv2);
                           falls back to the original photo when no confident
                           page outline is found.
  2. standardize         : scale to a standard width + JPEG-compress — the
                           readable copy that gets stored with the entry.
  3. ocr_preprocess      : grayscale/contrast-normalized derivative fed to OCR.
  4. mistral_ocr         : Mistral's dedicated OCR API (image -> markdown).
  5. extract_bibliography: a Mistral chat call turning the OCR text into strict
                           JSON bibliographic fields (+ an "extra" dict for
                           anything that has no dedicated column).

Every step is independently callable; process_capture() runs the whole chain.
cv2 is imported lazily so the module (and server) still load without it — the
perspective step then just passes photos through.
"""
from __future__ import annotations

import base64
import io
import json
import re
import urllib.request

MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
OCR_MODEL = "mistral-ocr-latest"
EXTRACT_MODEL = "mistral-small-latest"

STANDARD_WIDTH = 1600     # px; preserves title-page readability
STANDARD_QUALITY = 82     # JPEG quality for the stored copy

# The dedicated bibliographic fields (everything else lands in "extra").
FIELDS = ("title", "subtitle", "author", "volume", "edition",
          "publisher", "year", "city", "language")


# --- 1. perspective correction ------------------------------------------------

def _order_quad(pts):
    """Order 4 points as tl, tr, br, bl."""
    import numpy as np
    pts = np.asarray(pts, dtype="float32").reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()],
                     pts[s.argmax()], pts[d.argmax()]], dtype="float32")


def find_page_quad(img_bytes: bytes):
    """The page's 4-corner outline in full-res pixel coords, or None."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(img, (int(w * scale), int(h * scale))) if scale < 1 else img.copy()
    sh, sw = small.shape[:2]
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_area = 0.0
    for c in sorted(contours, key=cv2.contourArea, reverse=True)[:10]:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        area = cv2.contourArea(approx)
        if area > best_area:
            best, best_area = approx, area
    # demand a confident page: at least a quarter of the frame
    if best is None or best_area < 0.25 * sh * sw:
        return None
    quad = _order_quad(best.reshape(4, 2))
    if scale < 1:
        quad = quad / scale
    return quad


def perspective_correct(img_bytes: bytes, quality: int = 92) -> bytes:
    """Warp the detected page flat; the original bytes when detection fails."""
    quad = find_page_quad(img_bytes)
    if quad is None:
        return img_bytes
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    (tl, tr, br, bl) = quad
    w = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    h = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    if w < 200 or h < 200:                     # degenerate quad — keep original
        return img_bytes
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype="float32")
    m = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(img, m, (w, h))
    ok, out = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return out.tobytes() if ok else img_bytes


# --- 2. standard scale/compression ---------------------------------------------

def standardize(img_bytes: bytes, width: int = STANDARD_WIDTH,
                quality: int = STANDARD_QUALITY) -> bytes:
    """Scale to the standard width (never upscale) and JPEG-compress."""
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(img_bytes))
    img = ImageOps.exif_transpose(img)         # respect the phone's orientation tag
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)),
                         Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


# --- 3. OCR preprocessing ------------------------------------------------------

def ocr_preprocess(img_bytes: bytes) -> bytes:
    """Grayscale + local contrast normalization (CLAHE); PNG for the OCR call.

    Falls back to the input bytes without cv2.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return img_bytes
    img = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return img_bytes
    img = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(img)
    img = cv2.fastNlMeansDenoising(img, None, 7, 7, 21)
    ok, out = cv2.imencode(".png", img)
    return out.tobytes() if ok else img_bytes


# --- 4. Mistral OCR --------------------------------------------------------------

def _mistral_post(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def mistral_ocr(img_bytes: bytes, api_key: str, timeout: float = 90.0) -> str:
    """OCR one image via Mistral; returns the concatenated markdown text."""
    mime = "image/png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    b64 = base64.b64encode(img_bytes).decode("ascii")
    data = _mistral_post(MISTRAL_OCR_URL, {
        "model": OCR_MODEL,
        "document": {"type": "image_url",
                     "image_url": f"data:{mime};base64,{b64}"},
    }, api_key, timeout)
    pages = data.get("pages") or []
    return "\n\n".join(p.get("markdown", "") for p in pages).strip()


# --- 5. bibliographic field extraction -------------------------------------------

_EXTRACT_PROMPT = """You are cataloguing old books. Below is OCR text from photos of a book's \
title page and/or copyright page. Extract the bibliographic data as strict JSON.

Return a single JSON object with exactly these keys (string values; "" when absent):
  "title"      - the main title, in its original capitalization, without the subtitle
  "subtitle"   - the subtitle if present (text after the title, often following a colon)
  "author"     - primary author(s) as printed, "First Last" form, "; " between multiple
  "volume"     - volume number as a plain number string if this is one volume of a set
  "edition"    - edition statement as a short ordinal ("2nd", "3rd, revised") if stated
  "publisher"  - the publishing house
  "year"       - the publication year as a 4-digit Arabic number (convert Roman numerals)
  "city"       - the place of publication (first city if several)
  "language"   - the language of the book as a lowercase English word ("english")
  "extra"      - an object of any OTHER bibliographic facts found, using short
                 snake_case keys, e.g. printer, series, translator, illustrator,
                 copyright_year, copyright_holder, printing_number, dedication.
                 {} when none.

Do not invent data that is not in the text. Output ONLY the JSON object.

OCR TEXT:
"""


def extract_bibliography(ocr_text: str, api_key: str, timeout: float = 60.0) -> dict:
    """OCR text -> {fields..., extra:{}} via a structured Mistral chat call."""
    empty = {k: "" for k in FIELDS}
    empty["extra"] = {}
    text = str(ocr_text or "").strip()
    if not text:
        return empty
    data = _mistral_post(MISTRAL_CHAT_URL, {
        "model": EXTRACT_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": _EXTRACT_PROMPT + text[:12000]}],
    }, api_key, timeout)
    raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return empty
    out = {k: str(obj.get(k) or "").strip() for k in FIELDS}
    extra = obj.get("extra")
    out["extra"] = ({str(k): str(v) for k, v in extra.items() if str(v or "").strip()}
                    if isinstance(extra, dict) else {})
    return out


# --- the whole chain ---------------------------------------------------------------

def process_photo(img_bytes: bytes) -> bytes:
    """Perspective-correct + standardize one photo (the stored copy)."""
    return standardize(perspective_correct(img_bytes))


def process_capture(photo_bytes_list: list[bytes], api_key: str) -> dict:
    """All photos of one capture -> processed copies + extracted bibliography.

    Returns {"photos": [jpeg bytes...], "ocr_text": str, "fields": {...},
             "extra": {...}, "errors": [str...]}.  OCR/extraction failures are
    reported, not raised — the photos still import so nothing is lost.
    """
    photos: list[bytes] = []
    texts: list[str] = []
    errors: list[str] = []
    for i, raw in enumerate(photo_bytes_list, 1):
        try:
            processed = process_photo(raw)
        except Exception as exc:
            processed = raw
            errors.append(f"photo {i}: processing failed ({type(exc).__name__})")
        photos.append(processed)
        if not api_key:
            continue
        try:
            text = mistral_ocr(ocr_preprocess(processed), api_key)
            if text:
                texts.append(f"--- Photo {i} ---\n{text}")
        except Exception as exc:
            errors.append(f"photo {i}: OCR failed ({type(exc).__name__}: {exc})")
    ocr_text = "\n\n".join(texts)
    fields = {k: "" for k in FIELDS}
    extra: dict = {}
    if ocr_text and api_key:
        try:
            got = extract_bibliography(ocr_text, api_key)
            extra = got.pop("extra", {}) or {}
            fields = got
        except Exception as exc:
            errors.append(f"extraction failed ({type(exc).__name__}: {exc})")
    return {"photos": photos, "ocr_text": ocr_text,
            "fields": fields, "extra": extra, "errors": errors}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run the capture pipeline on an image")
    ap.add_argument("image")
    ap.add_argument("--key", default="", help="Mistral API key (omit to skip OCR)")
    ap.add_argument("--out", default="", help="write the processed JPEG here")
    a = ap.parse_args()
    raw = open(a.image, "rb").read()
    quad = find_page_quad(raw)
    print("page quad:", "found" if quad is not None else "not found (using original)")
    result = process_capture([raw], a.key)
    if a.out:
        open(a.out, "wb").write(result["photos"][0])
        print("processed ->", a.out, f"({len(result['photos'][0])//1024} KB)")
    if a.key:
        print(json.dumps({"fields": result["fields"], "extra": result["extra"],
                          "errors": result["errors"]}, indent=2))
