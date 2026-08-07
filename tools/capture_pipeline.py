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
MISTRAL_MAX_RESPONSE_BYTES = 64 * 1024 * 1024

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

def _mistral_post(
        url: str,
        payload: dict,
        api_key: str,
        timeout: float,
        *,
        maximum_response_bytes: int = MISTRAL_MAX_RESPONSE_BYTES,
) -> dict:
    if (
        not isinstance(maximum_response_bytes, int)
        or isinstance(maximum_response_bytes, bool)
        or maximum_response_bytes <= 0
    ):
        raise ValueError("maximum_response_bytes must be a positive integer")
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        declared = None
        try:
            raw_declared = resp.headers.get("Content-Length")
            if raw_declared is not None:
                declared = int(raw_declared)
        except (AttributeError, TypeError, ValueError):
            declared = None
        if declared is not None and declared > maximum_response_bytes:
            raise RuntimeError("Mistral response exceeds its size limit")
        encoded = resp.read(maximum_response_bytes + 1)
    if len(encoded) > maximum_response_bytes:
        raise RuntimeError("Mistral response exceeds its size limit")
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError(
            "Mistral returned an invalid JSON response"
        ) from None
    if not isinstance(decoded, dict):
        raise RuntimeError("Mistral returned an invalid JSON response")
    return decoded


def mistral_ocr_pages(img_bytes: bytes, api_key: str, timeout: float = 90.0,
                      want_images: bool = False,
                      want_blocks: bool = False,
                      *,
                      model: str | None = None) -> list[dict]:
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
        "model": str(model or OCR_MODEL).strip() or OCR_MODEL,
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

_EXTRACT_PROMPT = """You are cataloguing old books. Below is OCR text from photos of a book. \
Extract the bibliographic data as strict JSON.

Each photo's text is introduced by a header naming which part of the book it shows:
  (role: title_page) - the formal title page, and copyright/verso pages. This is the
                       AUTHORITATIVE source for title, subtitle, author, publisher,
                       year and city. Prefer it over every other role.
  (role: cover)      - outer board, dust jacket or wrapper. Usable for title and
                       author, but cover wording is often shortened or stylised;
                       never prefer it over a title page.
  (role: spine)      - the book's spine. Use it ONLY for "spine_title". A spine
                       carries no imprint, so never take publisher, city or year
                       from it.
  (role: other)      - endpapers, bookplates, dealer descriptions, price tags,
                       accession stamps, loose notes. Treat as UNTRUSTED for every
                       field. In particular this text often contains numbers that
                       look like dates but are not: a bookseller's code ("6/52",
                       "$20.00 1626-97"), a library date stamp ("NOV 24 72"), or a
                       shelf mark. NEVER take "year" from a role: other block.
A missing or unrecognised role tag means the part is unknown: fall back to judging
the text on its own merits, and prefer imprint-looking evidence.

Return a single JSON object with exactly these keys (string values; "" when absent):
  "title"      - the main title without the subtitle; render it in regular title case,
                 normalizing all-caps or erratic OCR capitalization
  "subtitle"   - the subtitle if present; render it in regular title case
  "author"     - primary author name(s) only, in "First Last" form, with "; " between
                 multiple; omit honorifics and titles such as Dr., Prof., Rev., or Sir
  "volume"     - volume number as an Arabic numeral string if this is one volume of a set;
                 convert Roman numerals and spelled-out numbers
  "edition"    - edition statement as a short ordinal ("2nd", "3rd, revised") if stated
  "publisher"  - the publishing house
  "year"       - the publication year as a 4-digit Arabic number (convert Roman numerals).
                 Use the imprint/copyright date only. If the only candidate comes from a
                 role: other block, or you cannot tell an imprint from a dealer's or
                 library's mark, return "" rather than guessing.
  "city"       - the place of publication (first city if several)
  "language"   - the language of the book as a lowercase English word ("english")
  "spine_title" - the title as printed on a role: spine photo, and
                  only when it differs materially from the published title;
                  "" when it is absent or equivalent
  "extra"      - an object of any OTHER bibliographic facts found, using short
                 snake_case keys, e.g. printer, series, translator, illustrator,
                 copyright_year, copyright_holder, printing_number, dedication.
                 {} when none.

Do not invent data that is not in the text. If the text is only image placeholders
or is otherwise too sparse to read, return every field as "" rather than supplying a
plausible record from memory. Output ONLY the JSON object.

OCR TEXT:
"""


def empty_bibliography() -> dict:
    empty: dict = {k: "" for k in FIELDS}
    empty["extra"] = {}
    return empty


# Mistral OCR emits `![img-0.jpeg](img-0.jpeg)` for a figure it could not read as
# text. A page of nothing but these is a FAILED read, but it is not an empty
# string, so a bare `if ocr_text:` guard lets it through to the model.
_OCR_IMAGE_PLACEHOLDER = re.compile(r"!\[img-\d+\.jpe?g\]\(img-\d+\.jpe?g\)")
# Section headers this module and Entries.ocrText() add are ours, not evidence.
_OCR_SECTION_HEADER = re.compile(r"^---\s*(?:Photo|Capture)\b.*$", re.M)
_OCR_MIN_READABLE_CHARS = 12


def readable_ocr_chars(ocr_text: str) -> int:
    """Characters of real recovered text, ignoring our own headers and figures.

    Extraction must key off this rather than off emptiness. Capture
    `7b9eba63` carried 52 characters that were purely image placeholders and was
    still handed to the model, which answered with a confident, entirely invented
    record (Gibbon's *Decline and Fall*, John Murray, 1854). Nothing in the reply
    marked it as unsupported, so it catalogued like any other book.
    """
    text = _OCR_SECTION_HEADER.sub(" ", str(ocr_text or ""))
    text = _OCR_IMAGE_PLACEHOLDER.sub(" ", text)
    return len(text.strip())


def has_readable_ocr(ocr_text: str) -> bool:
    """Whether OCR recovered enough text to be worth an extraction call."""
    return readable_ocr_chars(ocr_text) >= _OCR_MIN_READABLE_CHARS


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


def process_photo_traced(img_bytes: bytes) -> tuple[bytes, dict]:
    """``process_photo`` plus a trace of the exact transform it applied.

    The trace lets callers carry phone OCR geometry across the derivation
    (see ``librarytool.processing.capture_geometry``):

      {"quad": [[x, y] * 4] | None,   # TL/TR/BR/BL warp quad, upright pixels
       "source_size": (w, h),         # upright size the detector measured
       "warp_size": (w, h) | None,    # warp output size before standardize
       "exif_orientation": 1..8,      # EXIF tag of the input (informational)
       "final_size": (w, h)}          # size of the returned JPEG

    Every size and coordinate here lives in EXIF-UPRIGHT space, because
    every stage of the derivation does: ``cv2.imdecode`` applies the EXIF
    orientation itself (verified on OpenCV 5.0 — pass
    ``IMREAD_IGNORE_ORIENTATION`` to get the stored grid instead), so the
    detector's quad is already upright, and ``standardize`` reaches the
    same frame through ``ImageOps.exif_transpose``. Coordinates therefore
    need no orientation correction anywhere in the remap.

    The returned bytes are byte-identical to ``process_photo``'s: the same
    detector, warp kernel, and standardize encode run in the same order.
    """
    from librarytool.processing import capture_geometry as _capture_geometry

    quad = find_page_quad(img_bytes)
    warp_size = None
    if quad is not None:
        width, height = _capture_geometry.capture_warp_destination_size(quad)
        # apply_capture_pixel_perspective_compat refuses tiny outputs and
        # returns the original bytes; mirror that so the trace stays honest.
        if width < 200 or height < 200:
            quad, warp_size = None, None
        else:
            warp_size = (width, height)
    # The bytes come from process_photo itself — the one seam tests and
    # callers already own — so the trace can never describe a different
    # derivation than the stored copy. Detection runs once more inside it;
    # that only happens at import time.
    final = process_photo(img_bytes)
    if final == img_bytes:
        # Identity output (a stubbed pipeline): no derivation happened, so
        # the trace must not claim one.
        quad, warp_size = None, None
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(final)) as image:
        final_size = image.size
    # exif_transpose, not the stored size: the quad above came from cv2,
    # which already applied the EXIF orientation, so the frame the quad
    # indexes is the upright one.
    with Image.open(io.BytesIO(img_bytes)) as image:
        source_size = ImageOps.exif_transpose(image).size
    return final, {
        "quad": None if quad is None else [
            [float(x), float(y)] for x, y in
            (quad.tolist() if hasattr(quad, "tolist") else quad)
        ],
        "source_size": source_size,
        "warp_size": warp_size,
        "exif_orientation": _capture_geometry.jpeg_exif_orientation(img_bytes),
        "final_size": final_size,
    }


def remap_phone_geometry_for_import(
        asset_geometry: list, trace: dict, derivative_sha256: str) -> list:
    """Desktop-pinned geometry records for one imported asset, or [].

    ``asset_geometry`` is the phone contract's per-asset geometry list;
    ``trace`` comes from ``process_photo_traced``.  Records that already
    carry a ``display_sha256`` pin are phone-frame-independent and are never
    duplicated.
    """
    from librarytool.processing import capture_geometry as _capture_geometry

    results = []
    final_width, final_height = trace["final_size"]
    for geometry in asset_geometry or []:
        if not isinstance(geometry, dict) or geometry.get("display_sha256"):
            continue
        regions = geometry.get("regions")
        if not isinstance(regions, list) or not regions:
            continue
        try:
            remapped, _dropped = _capture_geometry.remap_phone_regions(
                regions,
                source_size=trace.get("source_size") or (0, 0),
                quad=trace.get("quad"),
                warp_size=trace.get("warp_size"),
            )
            if not remapped:
                continue
            results.append(_capture_geometry.desktop_display_geometry_record(
                geometry,
                regions=remapped,
                display_width=final_width,
                display_height=final_height,
                display_sha256=derivative_sha256,
            ))
        except (ValueError, TypeError, KeyError):
            continue
    return results


def process_capture(photo_bytes_list: list[bytes], api_key: str) -> dict:
    """All photos of one capture -> processed copies + extracted bibliography.

    Returns {"photos": [jpeg bytes...], "ocr_text": str, "fields": {...},
             "extra": {...}, "errors": [str...]}.  OCR/extraction failures are
    reported, not raised — the photos still import so nothing is lost.
    """
    photos: list[bytes] = []
    traces: list[dict | None] = []
    texts: list[str] = []
    errors: list[str] = []
    if not api_key:
        errors.append("OCR skipped (no Mistral API key configured)")
    for i, raw in enumerate(photo_bytes_list, 1):
        try:
            processed, trace = process_photo_traced(raw)
        except Exception as exc:
            processed, trace = raw, None
            errors.append(f"photo {i}: processing failed ({type(exc).__name__})")
        photos.append(processed)
        traces.append(trace)
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
    if ocr_text and api_key and not has_readable_ocr(ocr_text):
        # Abstain rather than let the model fill the silence from memory.
        errors.append(
            "extraction skipped (OCR recovered no readable text — "
            f"{readable_ocr_chars(ocr_text)} usable characters)")
    elif ocr_text and api_key:
        try:
            got = extract_bibliography(ocr_text, api_key)
            extra = got.pop("extra", {}) or {}
            fields = got
        except Exception as exc:
            errors.append(f"extraction failed ({type(exc).__name__}: {exc})")
    return {"photos": photos, "photo_traces": traces, "ocr_text": ocr_text,
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
