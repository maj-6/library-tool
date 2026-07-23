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

The standalone wrapper reads MISTRAL_API_KEY from the environment. Without it,
the wrapper still performs image processing and skips OCR.
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import re
import sys
import urllib.request
from pathlib import Path


def _load_raster_processing():
    try:
        return importlib.import_module("librarytool.processing")
    except ModuleNotFoundError as exc:
        if exc.name not in {"librarytool", "librarytool.processing"}:
            raise
        # Keep ``python tools/capture_pipeline.py`` working from a source
        # checkout, matching the explorer's direct-launch path setup.
        source_root = Path(__file__).resolve().parents[1] / "src"
        source_root_text = str(source_root)
        if source_root_text not in sys.path:
            sys.path.insert(0, source_root_text)
        return importlib.import_module("librarytool.processing")


_raster_processing = _load_raster_processing()

MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
OCR_MODEL = "mistral-ocr-latest"
EXTRACT_MODEL = "mistral-small-latest"

STANDARD_WIDTH = 1600     # px; preserves title-page readability
STANDARD_QUALITY = 82     # JPEG quality for the stored copy

# The dedicated bibliographic fields (everything else lands in "extra").
FIELDS = ("title", "subtitle", "author", "volume", "edition",
          "publisher", "year", "city", "language", "spine_title")


# --- 1. perspective correction ------------------------------------------------

def _order_quad(pts):
    """Order 4 points as tl, tr, br, bl."""
    return _raster_processing.order_capture_quad(pts)


def find_page_quad(img_bytes: bytes):
    """The page's 4-corner outline in full-res pixel coords, or None."""
    return _raster_processing.find_capture_page_quad(img_bytes)


def perspective_correct(img_bytes: bytes, quality: int = 92) -> bytes:
    """Warp the detected page flat; the original bytes when detection fails."""
    return _raster_processing.apply_capture_perspective_compat(
        img_bytes,
        quality=quality,
    )


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


def mistral_ocr_pages(img_bytes: bytes, api_key: str, timeout: float = 90.0,
                      want_images: bool = False,
                      want_blocks: bool = False) -> list[dict]:
    """OCR one image via Mistral; returns the raw page dicts.

    Each page carries `markdown`, `dimensions` {width, height, dpi}, and —
    with want_images — `images` [{id, top_left_x/y, bottom_right_x/y,
    image_base64}] for every figure the model cut out of the page. The
    markdown references those figures as ![id](id). With want_blocks (OCR 4)
    each page also carries `blocks` [{type, top_left_x/y, bottom_right_x/y,
    content}] — typed text regions in reading order, pixel coords like the
    figure boxes.
    """
    mime = "image/png" if img_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    b64 = base64.b64encode(img_bytes).decode("ascii")
    payload = {
        "model": OCR_MODEL,
        "document": {"type": "image_url",
                     "image_url": f"data:{mime};base64,{b64}"},
    }
    if want_images:
        payload["include_image_base64"] = True
    if want_blocks:
        payload["include_blocks"] = True
    data = _mistral_post(MISTRAL_OCR_URL, payload, api_key, timeout)
    return data.get("pages") or []


def mistral_ocr(img_bytes: bytes, api_key: str, timeout: float = 90.0) -> str:
    """OCR one image via Mistral; returns the concatenated markdown text."""
    pages = mistral_ocr_pages(img_bytes, api_key, timeout)
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
  "spine_title" - the title printed on the spine only when it differs materially
                  from the published title; "" when it is absent or equivalent
  "extra"      - an object of any OTHER bibliographic facts found, using short
                 snake_case keys, e.g. printer, series, translator, illustrator,
                 copyright_year, copyright_holder, printing_number, dedication.
                 {} when none.

Do not invent data that is not in the text. Output ONLY the JSON object.

OCR TEXT:
"""


def empty_bibliography() -> dict:
    empty: dict = {k: "" for k in FIELDS}
    empty["extra"] = {}
    return empty


def normalize_bibliography(obj) -> dict:
    """Coerce a model's JSON reply to the strict {fields..., extra:{}} shape.

    Shared by every extraction path (Mistral here, DeepSeek in the explorer's
    smart check) so a record looks the same regardless of which model wrote it.
    Anything that isn't a dict normalizes to the empty record.
    """
    if not isinstance(obj, dict):
        return empty_bibliography()
    out = {k: str(obj.get(k) or "").strip() for k in FIELDS}
    extra = obj.get("extra")

    def _flat(v):                       # nested values -> JSON, not Python reprs
        return (json.dumps(v, ensure_ascii=False)
                if isinstance(v, (dict, list)) else str(v).strip())

    def _keep(v):
        if isinstance(v, (dict, list)):
            return bool(v)
        return bool(str(v or "").strip())

    out["extra"] = ({str(k): _flat(v) for k, v in extra.items() if _keep(v)}
                    if isinstance(extra, dict) else {})
    return out


def extract_bibliography(ocr_text: str, api_key: str, timeout: float = 60.0) -> dict:
    """OCR text -> {fields..., extra:{}} via a structured Mistral chat call."""
    text = str(ocr_text or "").strip()
    if not text:
        return empty_bibliography()
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
        return empty_bibliography()
    return normalize_bibliography(obj)


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
    if not api_key:
        errors.append("OCR skipped (no Mistral API key configured)")
    for i, raw in enumerate(photo_bytes_list, 1):
        try:
            processed = process_photo(raw)
        except Exception as exc:
            processed = raw
            errors.append(f"photo {i}: processing failed ({type(exc).__name__})")
        photos.append(processed)
        if not api_key:
            continue
        # one retry: a transient 429/5xx/network blip must not permanently
        # cost this capture its extraction (it is only OCRed once, at import)
        for attempt in (1, 2):
            try:
                text = mistral_ocr(ocr_preprocess(processed), api_key)
                if text:
                    texts.append(f"--- Photo {i} ---\n{text}")
                break
            except Exception as exc:
                if attempt == 2:
                    errors.append(f"photo {i}: OCR failed ({type(exc).__name__}: {exc})")
                else:
                    import time
                    time.sleep(2.0)
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
    import cli_credentials

    ap = argparse.ArgumentParser(description="Run the capture pipeline on an image")
    ap.add_argument("image")
    ap.add_argument("--out", default="", help="write the processed JPEG here")
    a = ap.parse_args()
    api_key = cli_credentials.mistral_api_key(required=False)
    raw = open(a.image, "rb").read()
    quad = find_page_quad(raw)
    print("page quad:", "found" if quad is not None else "not found (using original)")
    result = process_capture([raw], api_key)
    if a.out:
        open(a.out, "wb").write(result["photos"][0])
        print("processed ->", a.out, f"({len(result['photos'][0])//1024} KB)")
    if api_key:
        print(json.dumps({"fields": result["fields"], "extra": result["extra"],
                          "errors": result["errors"]}, indent=2))
