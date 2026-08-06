"""Multi-engine OCR adapters producing one shared, normalized result shape.

The desktop app re-OCRs book-capture images. Historically only Mistral could
do it (tools/capture_pipeline.py); this module makes the engine a choice while
keeping ONE downstream contract, so the corrections/geometry pipeline never
learns provider quirks. Every adapter returns an ``OcrEngineResult`` dict:

    {"engine":        "<engine id from ENGINES>",
     "model":         "<provider model name>",
     "engine_version":"<adapter version tag>",
     "text":          "<full plain text>",
     "regions":       [{"id": "p0-r0",
                        "type": "text|title|image|table|header|footer|...",
                        "text": "...",
                        "polygon": [[x, y], ...],   # normalized 0..1 against
                                                    # the INPUT image, >=3 pts
                        "confidence": 0.0-1.0 or None}],
     "raw_meta":      {...}}                        # bounded debug metadata

The region shape deliberately matches the phone-capture geometry records that
``_capture_geometry_annotations`` (corrections_artifact_repository.py) already
validates: polygons of 3..16 points in [0, 1], at most 500 regions, free-form
identifier region types. Anything an adapter cannot express in those bounds is
dropped or collapsed rather than passed through.

Engines:

* ``mistral``          — reuses capture_pipeline.mistral_ocr_pages(want_blocks=
                         True) and normalizes pixel blocks against the page
                         dimensions EXACTLY the way the Android app does in
                         Pipeline.kt parseOcrResponse (same drop rules, same
                         fallback ids), so a desktop re-OCR and a phone OCR of
                         the same photo produce comparable geometry.
* ``textract-layout``  — AWS Textract AnalyzeDocument with FeatureTypes
                         ["LAYOUT"] over plain HTTPS with hand-rolled SigV4.
                         boto3 is deliberately NOT used: the frozen desktop
                         sidecar installs tools/requirements.txt only, and a
                         boto3 import would make the engine DOA in the
                         installer while CI stayed green.
* ``datalab-ocr``      — Datalab's OCR model on Replicate. Its output shape
                         differs fundamentally from Textract/Mistral (surya-
                         style per-page ``text_lines`` in PIXEL space, no
                         layout typing); see _normalize_datalab_output.
* ``external``         — a folder queue processed by an arbitrary outside
                         model. Asynchronous by nature: run_ocr() refuses it
                         and directs callers to tools/external_ocr_queue.py.

No engine writes anything to disk and the only network client is
urllib.request — the same one capture_pipeline already ships with.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import math
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

# Downstream bounds, mirrored from the corrections artifact repository's
# capture-geometry validation so nothing we emit is rejected there.
MAX_REGIONS = 500
MAX_POLYGON_POINTS = 16
MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # mirrors capture_pipeline's Mistral cap
_COORD_DECIMALS = 6
_META_MAX_KEYS = 32
_META_MAX_CHARS = 500

TEXTRACT_TARGET = "Textract.AnalyzeDocument"
TEXTRACT_CONTENT_TYPE = "application/x-amz-json-1.1"

DATALAB_MODEL = "datalab-to/ocr"
DATALAB_CREATE_URL = (
    f"https://api.replicate.com/v1/models/{DATALAB_MODEL}/predictions"
)
_REPLICATE_URL_PREFIX = "https://api.replicate.com/"
_DATALAB_POLL_SECONDS = 2.0

# AWS LAYOUT_* block types -> the region vocabulary the capture geometry
# pipeline displays. Unknown LAYOUT_* types fall back to their lowercased
# suffix so new AWS types degrade to something legible instead of vanishing.
TEXTRACT_LAYOUT_TYPES = {
    "LAYOUT_TITLE": "title",
    "LAYOUT_SECTION_HEADER": "section_header",
    "LAYOUT_TEXT": "text",
    "LAYOUT_FIGURE": "image",
    "LAYOUT_TABLE": "table",
    "LAYOUT_HEADER": "header",
    "LAYOUT_FOOTER": "footer",
    "LAYOUT_PAGE_NUMBER": "page_number",
    "LAYOUT_LIST": "list",
    "LAYOUT_KEY_VALUE": "key_value",
}

#: Engine registry. ``sync`` engines answer inside one run_ocr() call;
#: the ``external`` engine is a folder queue (tools/external_ocr_queue.py).
ENGINES: dict[str, dict] = {
    "mistral": {
        "id": "mistral",
        "label": "Mistral OCR",
        "required_secrets": ("mistral_api_key",),
        "optional_secrets": (),
        "sync": True,
    },
    "textract-layout": {
        "id": "textract-layout",
        "label": "AWS Textract Layout",
        "required_secrets": (
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_region",
        ),
        "optional_secrets": ("aws_session_token",),
        "sync": True,
    },
    "datalab-ocr": {
        "id": "datalab-ocr",
        "label": "Datalab OCR (Replicate)",
        "required_secrets": ("replicate_api_token",),
        "optional_secrets": (),
        "sync": True,
    },
    "external": {
        "id": "external",
        "label": "External model (folder queue)",
        "required_secrets": (),
        "optional_secrets": (),
        "sync": False,
    },
}


class OcrEngineError(RuntimeError):
    """The one failure type every engine raises.

    ``retryable`` tells callers whether trying again might help (429/5xx/
    timeouts/network blips) or is pointless (missing secrets, bad request,
    provider rejected the document).
    """

    def __init__(self, engine_id: str, message: str, retryable: bool = False):
        super().__init__(f"{engine_id}: {message}")
        self.engine_id = str(engine_id)
        self.retryable = bool(retryable)


# --- shared plumbing ---------------------------------------------------------


def _capture_pipeline():
    """Import capture_pipeline lazily; it owns the librarytool path setup."""
    try:
        import capture_pipeline
    except ModuleNotFoundError:
        tools_dir = str(Path(__file__).resolve().parent)
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import capture_pipeline
    return capture_pipeline


def _sleep(seconds: float) -> None:  # separate so tests can no-op the wait
    time.sleep(seconds)


def _http_error_snippet(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(2048)
    except Exception:
        return ""
    try:
        return raw.decode("utf-8", "replace").strip()[:500]
    except Exception:
        return ""


def _wrap_transport_error(engine_id: str, exc: Exception) -> OcrEngineError:
    """Classify a urllib failure into one retryable-or-not OcrEngineError."""
    if isinstance(exc, urllib.error.HTTPError):
        status = getattr(exc, "code", 0) or 0
        snippet = _http_error_snippet(exc)
        retryable = status == 429 or status >= 500 or "throttl" in snippet.lower()
        detail = snippet or str(getattr(exc, "reason", "") or "HTTP error")
        return OcrEngineError(engine_id, f"HTTP {status}: {detail}", retryable)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return OcrEngineError(engine_id, "request timed out", True)
    if isinstance(exc, urllib.error.URLError):
        return OcrEngineError(
            engine_id, f"network error: {getattr(exc, 'reason', exc)}", True
        )
    return OcrEngineError(engine_id, f"{type(exc).__name__}: {exc}", False)


def _http_json(engine_id: str, url: str, *, method: str = "POST",
               body: bytes | None = None,
               headers: Mapping[str, str] | None = None,
               timeout: float = 120.0) -> dict:
    """One bounded JSON round-trip; the only network door in this module.

    Tests monkeypatch this symbol, which is also why every provider call
    routes through it rather than touching urllib directly.
    """
    req = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            declared = None
            try:
                raw_declared = resp.headers.get("Content-Length")
                if raw_declared is not None:
                    declared = int(raw_declared)
            except (AttributeError, TypeError, ValueError):
                declared = None
            if declared is not None and declared > MAX_RESPONSE_BYTES:
                raise OcrEngineError(
                    engine_id, "response exceeds the size limit", False
                )
            encoded = resp.read(MAX_RESPONSE_BYTES + 1)
    except OcrEngineError:
        raise
    except Exception as exc:
        raise _wrap_transport_error(engine_id, exc) from None
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise OcrEngineError(engine_id, "response exceeds the size limit", False)
    try:
        decoded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise OcrEngineError(
            engine_id, "provider returned an invalid JSON response", False
        ) from None
    if not isinstance(decoded, dict):
        raise OcrEngineError(
            engine_id, "provider returned an invalid JSON response", False
        )
    return decoded


def _finite(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _confidence(value) -> float | None:
    number = _finite(value)
    if number is None:
        return None
    return min(1.0, max(0.0, number))


def _clamp01(value: float) -> float:
    return round(min(1.0, max(0.0, value)), _COORD_DECIMALS)


def _bbox_polygon(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _collapse_to_bbox(points: list[list[float]]) -> list[list[float]]:
    """A >16-point polygon would be rejected downstream; keep its bbox."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return _bbox_polygon(min(xs), min(ys), max(xs), max(ys))


def _bounded_meta(meta: Mapping) -> dict:
    """Debug metadata only — never a giant provider payload."""
    out: dict = {}
    for key, value in list(dict(meta or {}).items())[:_META_MAX_KEYS]:
        name = str(key)[:80]
        if value is None or isinstance(value, bool):
            out[name] = value
        elif isinstance(value, (int, float)) and _finite(value) is not None:
            out[name] = value
        elif isinstance(value, str):
            out[name] = value[:_META_MAX_CHARS]
        else:
            try:
                out[name] = json.dumps(value, ensure_ascii=False)[:_META_MAX_CHARS]
            except (TypeError, ValueError):
                out[name] = str(type(value).__name__)
    return out


def _result(engine_id: str, *, model: str, engine_version: str, text: str,
            regions: list[dict], raw_meta: Mapping) -> dict:
    return {
        "engine": engine_id,
        "model": str(model),
        "engine_version": engine_version,
        "text": text,
        "regions": regions[:MAX_REGIONS],
        "raw_meta": _bounded_meta(raw_meta),
    }


# --- SigV4 (hand-rolled; boto3 is deliberately out of the sidecar) -----------


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sigv4_signing_key(secret_key: str, datestamp: str, region: str,
                       service: str) -> bytes:
    key = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac_sha256(key, region)
    key = _hmac_sha256(key, service)
    return _hmac_sha256(key, "aws4_request")


def _sigv4_canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append((
            urllib.parse.quote(urllib.parse.unquote_plus(name), safe="-_.~"),
            urllib.parse.quote(urllib.parse.unquote_plus(value), safe="-_.~"),
        ))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _sigv4_canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    items = sorted(
        (str(name).strip().lower(), re.sub(r"\s+", " ", str(value)).strip())
        for name, value in headers.items()
    )
    canonical = "".join(f"{name}:{value}\n" for name, value in items)
    signed = ";".join(name for name, _ in items)
    return canonical, signed


def _sigv4_derive(*, method: str, url: str, region: str, service: str,
                  headers: Mapping[str, str], payload: bytes,
                  secret_key: str, amz_date: str) -> dict:
    """Canonical request -> string to sign -> signature, per the SigV4 spec.

    Split from sigv4_sign so the unit test can compare each intermediate
    against the documented AWS test vector, not just the final hex.
    """
    parsed = urllib.parse.urlsplit(url)
    datestamp = amz_date[:8]
    payload_hash = hashlib.sha256(payload or b"").hexdigest()
    canonical_headers, signed_headers = _sigv4_canonical_headers(headers)
    canonical_request = "\n".join([
        method.upper(),
        urllib.parse.quote(parsed.path or "/", safe="/-_.~"),
        _sigv4_canonical_query(parsed.query),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _sigv4_signing_key(secret_key, datestamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "canonical_request": canonical_request,
        "signed_headers": signed_headers,
        "string_to_sign": string_to_sign,
        "scope": scope,
        "signature": signature,
    }


def sigv4_sign(*, method: str, url: str, region: str, service: str,
               headers: Mapping[str, str], payload: bytes, access_key: str,
               secret_key: str, session_token: str = "",
               amz_date: str = "") -> dict:
    """Headers ready to send: the input headers + x-amz-date + Authorization.

    ``amz_date`` (YYYYMMDDTHHMMSSZ) is injectable for deterministic tests;
    production callers leave it empty and get the current UTC instant. The
    Host header is signed but not returned — urllib adds it from the URL.
    """
    parsed = urllib.parse.urlsplit(url)
    when = amz_date or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    all_headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    all_headers["host"] = parsed.netloc
    all_headers["x-amz-date"] = when
    if session_token:
        all_headers["x-amz-security-token"] = session_token
    derived = _sigv4_derive(
        method=method, url=url, region=region, service=service,
        headers=all_headers, payload=payload, secret_key=secret_key,
        amz_date=when,
    )
    sent = dict(all_headers)
    sent.pop("host", None)
    sent["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{derived['scope']}, "
        f"SignedHeaders={derived['signed_headers']}, "
        f"Signature={derived['signature']}"
    )
    return sent


# --- mistral -----------------------------------------------------------------


def _normalize_mistral_pages(pages: list) -> tuple[str, list[dict]]:
    """Pixel blocks -> normalized regions, mirroring Pipeline.kt.

    The Android app's parseOcrResponse is the reference implementation for
    Mistral OCR-4 blocks (rect polygon from top_left/bottom_right divided by
    the page ``dimensions``; degenerate or out-of-range blocks dropped whole;
    fallback ids "p{page}-r{blockIndex}" keyed by the RAW block index so a
    dropped neighbour never renumbers survivors). Desktop re-OCR must produce
    the same geometry for the same response, so this mirrors it exactly —
    the one difference is that all pages are normalized, not just the first
    page that yields regions.
    """
    markdown = "\n\n".join(
        str(page.get("markdown") or "")
        for page in pages if isinstance(page, Mapping)
    ).strip()
    regions: list[dict] = []
    for page_index, page in enumerate(pages):
        if not isinstance(page, Mapping):
            continue
        dimensions = page.get("dimensions")
        blocks = page.get("blocks")
        if not isinstance(dimensions, Mapping) or not isinstance(blocks, list):
            continue
        width = _finite(dimensions.get("width"))
        height = _finite(dimensions.get("height"))
        if not width or not height or width <= 0 or height <= 0:
            continue
        for block_index, block in enumerate(blocks):
            if not isinstance(block, Mapping):
                continue
            x0 = _finite(block.get("top_left_x"))
            y0 = _finite(block.get("top_left_y"))
            x1 = _finite(block.get("bottom_right_x"))
            y1 = _finite(block.get("bottom_right_y"))
            if None in (x0, y0, x1, y1) or x1 <= x0 or y1 <= y0:
                continue
            polygon = _bbox_polygon(x0 / width, y0 / height,
                                    x1 / width, y1 / height)
            if any(not math.isfinite(v) or not 0.0 <= v <= 1.0
                   for point in polygon for v in point):
                continue  # drop, never clamp — matches the Android rule
            polygon = [[round(x, _COORD_DECIMALS), round(y, _COORD_DECIMALS)]
                       for x, y in polygon]
            block_id = str(block.get("id") or "").strip()
            regions.append({
                "id": block_id or f"p{page_index}-r{block_index}",
                "type": (str(block.get("type") or "text").strip() or "text"),
                "text": str(block.get("content") or ""),
                "polygon": polygon,
                "confidence": _confidence(block.get("confidence")),
            })
    return markdown, regions[:MAX_REGIONS]


def _run_mistral(image_bytes: bytes, secrets: Mapping[str, str],
                 timeout: float) -> dict:
    pipeline = _capture_pipeline()
    api_key = str(secrets["mistral_api_key"]).strip()
    try:
        pages = pipeline.mistral_ocr_pages(
            image_bytes, api_key, timeout, want_blocks=True
        )
    except OcrEngineError:
        raise
    except Exception as exc:
        raise _wrap_transport_error("mistral", exc) from None
    text, regions = _normalize_mistral_pages(pages or [])
    return _result(
        "mistral",
        model=pipeline.OCR_MODEL,
        engine_version="ocr-4-blocks",  # matches the Android tag on purpose
        text=text,
        regions=regions,
        raw_meta={
            "page_count": len(pages or []),
            "region_count": len(regions),
        },
    )


# --- textract-layout ---------------------------------------------------------


def _textract_polygon(block: Mapping) -> list[list[float]] | None:
    """Geometry.Polygon (already 0..1) -> our polygon; BoundingBox fallback."""
    geometry = block.get("Geometry")
    if not isinstance(geometry, Mapping):
        return None
    points: list[list[float]] = []
    raw_polygon = geometry.get("Polygon")
    if isinstance(raw_polygon, list):
        for point in raw_polygon:
            if not isinstance(point, Mapping):
                return None
            x = _finite(point.get("X"))
            y = _finite(point.get("Y"))
            if x is None or y is None:
                return None
            points.append([_clamp01(x), _clamp01(y)])
    if len(points) > MAX_POLYGON_POINTS:
        points = _collapse_to_bbox(points)
    if len(points) >= 3:
        return points
    box = geometry.get("BoundingBox")
    if isinstance(box, Mapping):
        left = _finite(box.get("Left"))
        top = _finite(box.get("Top"))
        width = _finite(box.get("Width"))
        height = _finite(box.get("Height"))
        if None not in (left, top, width, height) and width > 0 and height > 0:
            return [[_clamp01(x), _clamp01(y)] for x, y in _bbox_polygon(
                left, top, left + width, top + height)]
    return None


def _normalize_textract_blocks(blocks: list) -> tuple[str, list[dict]]:
    """LAYOUT_* blocks -> typed regions; child LINE text via Relationships."""
    lines_by_id: dict[str, str] = {}
    line_texts: list[str] = []
    for block in blocks:
        if (isinstance(block, Mapping) and block.get("BlockType") == "LINE"
                and block.get("Id")):
            text = str(block.get("Text") or "")
            lines_by_id[str(block["Id"])] = text
            line_texts.append(text)
    regions: list[dict] = []
    counters: dict[int, int] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        block_type = str(block.get("BlockType") or "")
        if not block_type.startswith("LAYOUT_"):
            continue
        polygon = _textract_polygon(block)
        if polygon is None:
            continue
        try:
            page = max(0, int(block.get("Page") or 1) - 1)
        except (TypeError, ValueError):
            page = 0
        child_ids: list[str] = []
        for relationship in block.get("Relationships") or []:
            if (isinstance(relationship, Mapping)
                    and relationship.get("Type") == "CHILD"):
                child_ids.extend(
                    str(i) for i in relationship.get("Ids") or []
                )
        text = "\n".join(
            lines_by_id[i] for i in child_ids if i in lines_by_id
        )
        raw_confidence = _finite(block.get("Confidence"))
        index = counters.get(page, 0)
        counters[page] = index + 1
        regions.append({
            "id": f"p{page}-r{index}",
            "type": TEXTRACT_LAYOUT_TYPES.get(
                block_type,
                block_type[len("LAYOUT_"):].lower() or "text",
            ),
            "text": text,
            "polygon": polygon,
            "confidence": (
                None if raw_confidence is None
                else min(1.0, max(0.0, raw_confidence / 100.0))
            ),
        })
    regions = regions[:MAX_REGIONS]
    text = "\n\n".join(r["text"] for r in regions if r["text"]).strip()
    if not text:  # LAYOUT can be empty on trivial documents; LINEs still read
        text = "\n".join(t for t in line_texts if t).strip()
    return text, regions


def _run_textract(image_bytes: bytes, secrets: Mapping[str, str],
                  timeout: float) -> dict:
    engine_id = "textract-layout"
    region = str(secrets["aws_region"]).strip()
    url = f"https://textract.{region}.amazonaws.com/"
    body = json.dumps({
        "Document": {"Bytes": base64.b64encode(image_bytes).decode("ascii")},
        "FeatureTypes": ["LAYOUT"],
    }).encode("utf-8")
    headers = sigv4_sign(
        method="POST", url=url, region=region, service="textract",
        headers={
            "content-type": TEXTRACT_CONTENT_TYPE,
            "x-amz-target": TEXTRACT_TARGET,
        },
        payload=body,
        access_key=str(secrets["aws_access_key_id"]).strip(),
        secret_key=str(secrets["aws_secret_access_key"]).strip(),
        session_token=str(secrets.get("aws_session_token") or "").strip(),
    )
    data = _http_json(engine_id, url, method="POST", body=body,
                      headers=headers, timeout=timeout)
    blocks = data.get("Blocks")
    if not isinstance(blocks, list):
        blocks = []
    text, regions = _normalize_textract_blocks(blocks)
    return _result(
        engine_id,
        model="aws-textract-analyze-document",
        engine_version="textract-layout/1",
        text=text,
        regions=regions,
        raw_meta={
            "model_version": data.get("AnalyzeDocumentModelVersion"),
            "document_metadata": data.get("DocumentMetadata"),
            "block_count": len(blocks),
            "region_count": len(regions),
        },
    )


# --- datalab-ocr (Replicate) -------------------------------------------------
#
# SPECIAL OUTPUT HANDLING. Datalab's OCR (surya-style) does NOT look like
# Textract or Mistral: there is no layout typing and no normalized geometry.
# It returns per-page ``text_lines``, each carrying a PIXEL-space
# ``bbox`` [x0, y0, x1, y1] and/or ``polygon`` [[x, y], ...], plus a page
# ``image_bbox`` [x0, y0, x1, y1] naming the pixel frame those coordinates
# live in. The output wrapper also varies between a dict (``{"pages": [...]}``
# or a single page object) and a bare list of pages. Keep this normalizer
# separate from the others and tolerant of all of those shapes:
#   * pixels are normalized against the page's image_bbox span (origin
#     subtracted), falling back to the input image's own dimensions when
#     image_bbox is absent;
#   * every region is typed "text" (line-level only);
#   * region ids are "p{page}-l{lineIndex}" with the RAW line index;
#   * full text = lines joined in the given (reading) order.


def _datalab_pages(output) -> list[Mapping]:
    if isinstance(output, Mapping):
        pages = output.get("pages")
        if isinstance(pages, list):
            return [p for p in pages if isinstance(p, Mapping)]
        if "text_lines" in output:
            return [output]
        return []
    if isinstance(output, list):
        return [p for p in output if isinstance(p, Mapping)]
    return []


def _datalab_line_points(line: Mapping) -> list[list[float]] | None:
    """A line's pixel geometry: prefer ``polygon``, fall back to ``bbox``."""
    raw_polygon = line.get("polygon")
    if isinstance(raw_polygon, list) and len(raw_polygon) >= 3:
        points: list[list[float]] = []
        for point in raw_polygon:
            if (isinstance(point, (list, tuple)) and len(point) >= 2):
                x = _finite(point[0])
                y = _finite(point[1])
                if x is not None and y is not None:
                    points.append([x, y])
                    continue
            return None
        if len(points) > MAX_POLYGON_POINTS:
            points = _collapse_to_bbox(points)
        return points
    bbox = line.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        x0, y0, x1, y1 = (_finite(v) for v in bbox)
        if None not in (x0, y0, x1, y1) and x1 > x0 and y1 > y0:
            return _bbox_polygon(x0, y0, x1, y1)
    return None


def _normalize_datalab_output(
        output, *, fallback_size: tuple[int, int] | None = None,
) -> tuple[str, list[dict]]:
    pages = _datalab_pages(output)
    regions: list[dict] = []
    page_texts: list[str] = []
    for page_index, page in enumerate(pages):
        origin_x = origin_y = 0.0
        width = height = None
        image_bbox = page.get("image_bbox")
        if isinstance(image_bbox, (list, tuple)) and len(image_bbox) == 4:
            x0, y0, x1, y1 = (_finite(v) for v in image_bbox)
            if None not in (x0, y0, x1, y1) and x1 > x0 and y1 > y0:
                origin_x, origin_y = x0, y0
                width, height = x1 - x0, y1 - y0
        if width is None and fallback_size:
            width, height = float(fallback_size[0]), float(fallback_size[1])
            if width <= 0 or height <= 0:
                width = height = None
        lines = page.get("text_lines")
        if not isinstance(lines, list):
            lines = []
        texts: list[str] = []
        for line_index, line in enumerate(lines):
            if not isinstance(line, Mapping):
                continue
            line_text = str(line.get("text") or "")
            if line_text:
                texts.append(line_text)
            if width is None:  # no pixel frame -> text only, no geometry
                continue
            points = _datalab_line_points(line)
            if points is None:
                continue
            polygon = [
                [_clamp01((x - origin_x) / width),
                 _clamp01((y - origin_y) / height)]
                for x, y in points
            ]
            regions.append({
                "id": f"p{page_index}-l{line_index}",
                "type": "text",
                "text": line_text,
                "polygon": polygon,
                "confidence": _confidence(line.get("confidence")),
            })
        page_texts.append("\n".join(texts))
    text = "\n\n".join(t for t in page_texts if t).strip()
    return text, regions[:MAX_REGIONS]


def _image_size_or_none(image_bytes: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception:
        return None


def _run_datalab(image_bytes: bytes, secrets: Mapping[str, str],
                 timeout: float) -> dict:
    engine_id = "datalab-ocr"
    token = str(secrets["replicate_api_token"]).strip()
    mime = ("image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
            else "image/jpeg")
    data_uri = (
        f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )
    auth = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + max(1.0, float(timeout))
    prediction = _http_json(
        engine_id, DATALAB_CREATE_URL, method="POST",
        body=json.dumps({"input": {"image": data_uri}}).encode("utf-8"),
        headers={
            **auth,
            "Content-Type": "application/json",
            # Hold the connection open server-side where supported; the poll
            # loop below covers deployments that answer immediately anyway.
            "Prefer": f"wait={max(1, min(60, int(timeout)))}",
        },
        timeout=timeout,
    )
    while True:
        status = str(prediction.get("status") or "")
        if status == "succeeded":
            break
        if status in ("failed", "canceled"):
            raise OcrEngineError(
                engine_id,
                f"prediction {status}: "
                f"{str(prediction.get('error') or '')[:500] or 'no detail'}",
                False,
            )
        poll_url = str((prediction.get("urls") or {}).get("get") or "")
        if not poll_url.startswith(_REPLICATE_URL_PREFIX):
            raise OcrEngineError(
                engine_id,
                f"prediction is {status or 'in an unknown state'} "
                "and exposed no poll URL",
                False,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OcrEngineError(
                engine_id,
                f"timed out after {timeout:.0f}s waiting for the prediction",
                True,
            )
        _sleep(min(_DATALAB_POLL_SECONDS, max(0.05, remaining)))
        prediction = _http_json(
            engine_id, poll_url, method="GET", headers=auth,
            timeout=min(30.0, timeout),
        )
    text, regions = _normalize_datalab_output(
        prediction.get("output"),
        fallback_size=_image_size_or_none(image_bytes),
    )
    return _result(
        engine_id,
        model=str(prediction.get("model") or DATALAB_MODEL),
        engine_version="datalab-ocr/1",
        text=text,
        regions=regions,
        raw_meta={
            "prediction_id": prediction.get("id"),
            "version": prediction.get("version"),
            "status": prediction.get("status"),
            "metrics": prediction.get("metrics"),
            "region_count": len(regions),
        },
    )


# --- dispatch ----------------------------------------------------------------


_RUNNERS = {
    "mistral": _run_mistral,
    "textract-layout": _run_textract,
    "datalab-ocr": _run_datalab,
}


def run_ocr(engine_id: str, image_bytes: bytes, *,
            secrets: Mapping[str, str], timeout: float = 120.0) -> dict:
    """OCR one image with a synchronous engine -> OcrEngineResult dict.

    ``secrets`` maps the engine's required/optional secret names (see
    ENGINES) to values. Missing secrets raise a non-retryable
    OcrEngineError naming what is missing; "external" always raises,
    because that engine is a folder queue (tools/external_ocr_queue.py),
    not a call.
    """
    spec = ENGINES.get(engine_id)
    if spec is None:
        raise OcrEngineError(
            str(engine_id),
            f"unknown engine id (expected one of: {', '.join(ENGINES)})",
            False,
        )
    if not spec["sync"]:
        raise OcrEngineError(
            engine_id,
            "the external engine is asynchronous by nature: submit jobs with "
            "tools/external_ocr_queue.submit() and pick results up with "
            "external_ocr_queue.collect()",
            False,
        )
    if not isinstance(image_bytes, (bytes, bytearray, memoryview)):
        raise OcrEngineError(engine_id, "image_bytes must be bytes", False)
    image_bytes = bytes(image_bytes)
    if not image_bytes:
        raise OcrEngineError(engine_id, "image_bytes is empty", False)
    secrets = secrets or {}
    missing = [
        name for name in spec["required_secrets"]
        if not str(secrets.get(name) or "").strip()
    ]
    if missing:
        raise OcrEngineError(
            engine_id,
            f"missing required secret(s): {', '.join(missing)}",
            False,
        )
    return _RUNNERS[engine_id](image_bytes, secrets, float(timeout))
