"""Folder-queue protocol for OCR by an arbitrary external model.

The synchronous engines in tools/ocr_engines.py cover providers we can call
over HTTPS. The "external" engine exists for everything we cannot: a local
GUI tool, a batch cluster, a research model behind an air gap, or a human.
For those, the filesystem IS the API — this module owns both sides of it:

    <data_root>/ocr_external/
        INSTRUCTIONS.md          written for the EXTERNAL MODEL's operator;
                                 refreshed whenever its version header changes
        pending/<job_id>/        submit() writes input.<ext> + job.json here;
                                 the external side answers with result.json
                                 and then an empty DONE marker (or an ERROR
                                 file whose text is the failure reason)
        done/<job_id>/           collect() moves completed jobs here
        failed/<job_id>/         ... and failures/malformed results here,
                                 with a COLLECT_DIAGNOSTIC.txt explaining why

Design rules, all learned the hard way elsewhere in this repo:

* Every write is atomic (tmp + os.replace), and a whole job directory is
  staged before it appears under pending/, so the external side can never
  observe a half-written job.
* collect() only reads result.json AFTER the DONE/ERROR marker exists — the
  marker is the external side's commit point, mirroring how the Android app
  keeps an original over a torn write.
* Results are validated with the same bounds the corrections geometry
  pipeline enforces (polygons of 3..16 points in [0, 1], at most 500
  regions), so a queue result is indistinguishable downstream from a
  synchronous engine's. A malformed result moves to failed/ with a
  diagnostic and is reported in collect()'s return value — never raised.
* collect() is idempotent and safe against concurrent collectors: a job
  directory vanishing mid-scan (the other collector won the os.replace)
  is silently skipped, and only the collector whose move succeeded reports
  the job.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

JOB_SCHEMA = "org.whl.external-ocr-job/1"
RESULT_SCHEMA = "org.whl.external-ocr-result/1"
INSTRUCTIONS_HEADER = "<!-- org.whl.external-ocr-instructions/1 -->"
ENGINE_VERSION = "external-queue/1"

QUEUE_DIRNAME = "ocr_external"
JOB_FILE = "job.json"
RESULT_FILE = "result.json"
DONE_FILE = "DONE"
ERROR_FILE = "ERROR"
DIAGNOSTIC_FILE = "COLLECT_DIAGNOSTIC.txt"

# Bounds. The geometry limits mirror the corrections artifact repository's
# capture-geometry validation; the file/text caps keep a hostile or buggy
# external worker from feeding the app a giant payload.
MAX_RESULT_FILE_BYTES = 16 * 1024 * 1024
MAX_JOB_FILE_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_REGIONS = 500
MIN_POLYGON_POINTS = 3
MAX_POLYGON_POINTS = 16
MAX_REGION_TEXT_CHARS = 10_000
MAX_ID_CHARS = 120
MAX_TYPE_CHARS = 80
MAX_ENGINE_ID_CHARS = 80
MAX_MODEL_CHARS = 120
MAX_ERROR_CHARS = 2_000
MAX_CONTEXT_KEYS = 16
MAX_CONTEXT_KEY_CHARS = 64
MAX_CONTEXT_VALUE_CHARS = 500

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/tiff": "tif",
}


# --- paths -------------------------------------------------------------------


def _default_data_root() -> Path:
    import libcommon  # deferred: resolves WHL_DATA_ROOT at ITS import time
    return Path(libcommon.DATA_ROOT)


def queue_root(data_root: Path | str | None = None) -> Path:
    base = Path(data_root) if data_root is not None else _default_data_root()
    return base / QUEUE_DIRNAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# --- submit ------------------------------------------------------------------


def _image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    import io
    from PIL import Image
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception as exc:
        raise ValueError(
            f"image_bytes is not a decodable image ({type(exc).__name__})"
        ) from exc


def _validated_context(context: Mapping | None) -> dict:
    """Opaque caller keys (item_id, artifact_id, ...) echoed back verbatim.

    Scalars only, bounded — this rides inside job.json and comes back in
    collect(), so it must never be a vehicle for arbitrary payloads.
    """
    if context is None:
        return {}
    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping")
    if len(context) > MAX_CONTEXT_KEYS:
        raise ValueError(f"context carries more than {MAX_CONTEXT_KEYS} keys")
    out: dict = {}
    for key, value in context.items():
        name = str(key)
        if not name or len(name) > MAX_CONTEXT_KEY_CHARS:
            raise ValueError(f"context key {name[:80]!r} is empty or too long")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"context[{name!r}] must be a scalar or None")
        if isinstance(value, str) and len(value) > MAX_CONTEXT_VALUE_CHARS:
            raise ValueError(f"context[{name!r}] exceeds "
                             f"{MAX_CONTEXT_VALUE_CHARS} characters")
        out[name] = value
    return out


def _job_payload(job_id: str, *, image_file: str, sha256: str,
                 media_type: str, width: int, height: int,
                 context: dict) -> dict:
    """job.json: identity + the full expected-output contract, inline.

    The contract is repeated per job (not only in INSTRUCTIONS.md) so a
    worker that is handed a single job directory still has everything it
    needs.
    """
    return {
        "schema": JOB_SCHEMA,
        "job_id": job_id,
        "submitted_at": _utc_now(),
        "image": {
            "file": image_file,
            "sha256": sha256,
            "media_type": media_type,
            "width": width,
            "height": height,
        },
        "context": context,
        "expected_result": {
            "file": RESULT_FILE,
            "schema": RESULT_SCHEMA,
            "write_order": (
                f"write {RESULT_FILE} completely, then create an empty "
                f"{DONE_FILE} file in this directory"
            ),
            "on_failure": (
                f"write the failure reason as plain text into a file named "
                f"{ERROR_FILE} in this directory"
            ),
            "coordinates": (
                "polygon points are normalized 0..1 against the input image "
                "(x = pixel_x / width, y = pixel_y / height); origin is the "
                "top-left corner, x grows right, y grows down; each polygon "
                "lists its vertices in drawing order"
            ),
            "limits": {
                "regions": MAX_REGIONS,
                "polygon_points": [MIN_POLYGON_POINTS, MAX_POLYGON_POINTS],
                "text_chars": MAX_TEXT_CHARS,
                "region_text_chars": MAX_REGION_TEXT_CHARS,
            },
            "contract": {
                "schema": RESULT_SCHEMA,
                "job_id": job_id,
                "engine": {
                    "id": "<short identifier of your model, e.g. my-ocr>",
                    "model": "<provider model name, e.g. my-ocr-v2>",
                },
                "text": "<full plain text of the image, reading order>",
                "regions": [{
                    "id": "p0-r0",
                    "type": "text",
                    "text": "<text of this region>",
                    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]],
                    "confidence": 0.95,
                }],
            },
        },
    }


def submit(image_bytes: bytes, *, media_type: str,
           data_root: Path | str | None = None,
           context: Mapping | None = None) -> str:
    """Queue one image for the external model; returns the new job id.

    The image bytes are stored verbatim as pending/<job_id>/input.<ext>
    next to a job.json carrying identity, dimensions (via PIL), the caller's
    opaque ``context`` (echoed back by collect()), and the full expected-
    output contract. The job directory is staged and appears atomically.
    """
    if not isinstance(image_bytes, (bytes, bytearray, memoryview)):
        raise TypeError("image_bytes must be bytes")
    image_bytes = bytes(image_bytes)
    if not image_bytes:
        raise ValueError("image_bytes is empty")
    media = str(media_type or "").strip().lower()
    if not media:
        raise ValueError("media_type is required")
    width, height = _image_dimensions(image_bytes)
    checked_context = _validated_context(context)

    root = queue_root(data_root)
    for name in ("pending", "done", "failed"):
        (root / name).mkdir(parents=True, exist_ok=True)
    _refresh_instructions(root)

    job_id = str(uuid.uuid4())
    image_file = f"input.{_EXTENSIONS.get(media, 'bin')}"
    stage = root / ".staging" / job_id
    stage.mkdir(parents=True, exist_ok=True)
    try:
        _write_atomic(stage / image_file, image_bytes)
        payload = _job_payload(
            job_id,
            image_file=image_file,
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            media_type=media,
            width=width,
            height=height,
            context=checked_context,
        )
        _write_atomic(
            stage / JOB_FILE,
            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        )
        os.replace(stage, root / "pending" / job_id)
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return job_id


# --- collect -----------------------------------------------------------------


def _bounded_read_text(path: Path, limit: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _job_summary(job_dir: Path) -> tuple[dict, str, str]:
    """(context, submitted_at, image_file) from job.json; empty on damage."""
    raw = _bounded_read_text(job_dir / JOB_FILE, MAX_JOB_FILE_BYTES)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}, "", ""
    if not isinstance(payload, dict):
        return {}, "", ""
    context = payload.get("context")
    if not isinstance(context, dict):
        context = {}
    image = payload.get("image")
    image_file = ""
    if isinstance(image, dict):
        image_file = str(image.get("file") or "")[:200]
    return (
        {str(k)[:MAX_CONTEXT_KEY_CHARS]: v for k, v in
         list(context.items())[:MAX_CONTEXT_KEYS]
         if v is None or isinstance(v, (str, int, float, bool))},
        str(payload.get("submitted_at") or "")[:40],
        image_file,
    )


def _finite01(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return number


def _validated_region(region, index: int) -> tuple[dict | None, str | None]:
    if not isinstance(region, dict):
        return None, f"regions[{index}] must be an object"
    region_id = str(region.get("id") or "").strip()
    if not region_id or len(region_id) > MAX_ID_CHARS:
        return None, (f"regions[{index}].id must be a non-empty string of at "
                      f"most {MAX_ID_CHARS} characters")
    region_type = str(region.get("type") or "text").strip() or "text"
    if len(region_type) > MAX_TYPE_CHARS:
        return None, f"regions[{index}].type exceeds {MAX_TYPE_CHARS} characters"
    text = region.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        return None, f"regions[{index}].text must be a string"
    if len(text) > MAX_REGION_TEXT_CHARS:
        return None, (f"regions[{index}].text exceeds "
                      f"{MAX_REGION_TEXT_CHARS} characters")
    raw_polygon = region.get("polygon")
    if (not isinstance(raw_polygon, list)
            or not MIN_POLYGON_POINTS <= len(raw_polygon)
            <= MAX_POLYGON_POINTS):
        return None, (f"regions[{index}].polygon must be a list of "
                      f"{MIN_POLYGON_POINTS}..{MAX_POLYGON_POINTS} points")
    polygon: list[list[float]] = []
    for point in raw_polygon:
        if (not isinstance(point, (list, tuple)) or len(point) < 2):
            return None, (f"regions[{index}].polygon points must be "
                          f"[x, y] pairs")
        x = _finite01(point[0])
        y = _finite01(point[1])
        if x is None or y is None:
            return None, (f"regions[{index}].polygon coordinates must be "
                          f"finite numbers in [0, 1]")
        polygon.append([x, y])
    confidence = region.get("confidence")
    if confidence is not None:
        confidence = _finite01(confidence)
        if confidence is None:
            return None, (f"regions[{index}].confidence must be null or a "
                          f"finite number in [0, 1]")
    return {
        "id": region_id,
        "type": region_type,
        "text": text,
        "polygon": polygon,
        "confidence": confidence,
    }, None


def _validated_result(job_dir: Path, job_id: str) -> tuple[dict | None, str | None]:
    """result.json -> (normalized OcrEngineResult, None) or (None, problem)."""
    path = job_dir / RESULT_FILE
    try:
        size = path.stat().st_size
    except OSError:
        return None, f"{RESULT_FILE} is missing"
    if size > MAX_RESULT_FILE_BYTES:
        return None, (f"{RESULT_FILE} is {size} bytes; the limit is "
                      f"{MAX_RESULT_FILE_BYTES}")
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except OSError:
        return None, f"{RESULT_FILE} could not be read"
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{RESULT_FILE} is not valid JSON ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return None, f"{RESULT_FILE} must be a JSON object"
    if payload.get("schema") != RESULT_SCHEMA:
        return None, f'schema must be "{RESULT_SCHEMA}"'
    if str(payload.get("job_id") or "") != job_id:
        return None, "job_id does not match the job directory"
    engine = payload.get("engine")
    if not isinstance(engine, dict):
        return None, "engine must be an object with id and model"
    declared_id = str(engine.get("id") or "").strip()
    model = str(engine.get("model") or "").strip()
    if not declared_id or len(declared_id) > MAX_ENGINE_ID_CHARS:
        return None, (f"engine.id must be a non-empty string of at most "
                      f"{MAX_ENGINE_ID_CHARS} characters")
    if not model or len(model) > MAX_MODEL_CHARS:
        return None, (f"engine.model must be a non-empty string of at most "
                      f"{MAX_MODEL_CHARS} characters")
    text = payload.get("text")
    if not isinstance(text, str):
        return None, "text must be a string"
    if len(text) > MAX_TEXT_CHARS:
        return None, f"text exceeds {MAX_TEXT_CHARS} characters"
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, list):
        return None, "regions must be a list"
    if len(raw_regions) > MAX_REGIONS:
        return None, f"more than {MAX_REGIONS} regions"
    regions: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw_region in enumerate(raw_regions):
        region, problem = _validated_region(raw_region, index)
        if problem:
            return None, problem
        if region["id"] in seen_ids:
            return None, f"regions[{index}].id {region['id']!r} is duplicated"
        seen_ids.add(region["id"])
        regions.append(region)
    return {
        "engine": "external",
        "model": model,
        "engine_version": ENGINE_VERSION,
        "text": text,
        "regions": regions,
        "raw_meta": {
            "job_id": job_id,
            "declared_engine_id": declared_id,
            "schema": RESULT_SCHEMA,
        },
    }, None


def _move_job(job_dir: Path, target_parent: Path) -> bool:
    """os.replace the whole job dir; False when a rival collector won."""
    for suffix in ("", "-dup1", "-dup2", "-dup3"):
        target = target_parent / (job_dir.name + suffix)
        try:
            os.replace(job_dir, target)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            continue  # target already exists — try a suffixed name
    return False


def _fail_job(job_dir: Path, failed_dir: Path, problem: str) -> bool:
    try:
        _write_atomic(
            job_dir / DIAGNOSTIC_FILE,
            (f"The desktop collector rejected this job's result.\n\n"
             f"{problem}\n\n"
             f"Fix {RESULT_FILE} against the contract in {JOB_FILE} / "
             f"INSTRUCTIONS.md and re-submit a new job; this directory is "
             f"kept for inspection only.\n").encode("utf-8"),
        )
    except OSError:
        pass  # the diagnostic is best-effort; the move is what matters
    return _move_job(job_dir, failed_dir)


def collect(*, data_root: Path | str | None = None) -> list[dict]:
    """Harvest finished jobs from pending/. Never raises for a bad job.

    Returns one entry per job THIS call moved out of pending/:

        {"job_id": str, "status": "done"|"failed",
         "result": OcrEngineResult-dict or None,
         "error": str or None,
         "context": dict,          # echoed from submit()
         "submitted_at": str}

    A job is only touched once its DONE or ERROR marker exists; validated
    results move to done/, everything else moves to failed/ with a
    diagnostic. Jobs a concurrent collector grabs mid-scan are skipped.
    """
    root = queue_root(data_root)
    pending = root / "pending"
    done_dir = root / "done"
    failed_dir = root / "failed"
    try:
        entries = sorted(pending.iterdir())
    except OSError:
        return []
    for parent in (done_dir, failed_dir):
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return []
    collected: list[dict] = []
    for job_dir in entries:
        try:
            if not job_dir.is_dir():
                continue
            has_error = (job_dir / ERROR_FILE).exists()
            has_done = (job_dir / DONE_FILE).exists()
        except OSError:
            continue
        if not (has_error or has_done):
            continue  # still the external side's turn — do not even read
        job_id = job_dir.name
        context, submitted_at, _image_file = _job_summary(job_dir)
        entry: dict = {
            "job_id": job_id,
            "status": "",
            "result": None,
            "error": None,
            "context": context,
            "submitted_at": submitted_at,
        }
        if has_error:
            message = _bounded_read_text(
                job_dir / ERROR_FILE, MAX_ERROR_CHARS
            ).strip() or "the external worker reported an error"
            entry.update(status="failed", error=message)
            moved = _move_job(job_dir, failed_dir)  # ERROR file rides along
        else:
            result, problem = _validated_result(job_dir, job_id)
            if problem:
                entry.update(status="failed", error=problem)
                moved = _fail_job(job_dir, failed_dir, problem)
            else:
                entry.update(status="done", result=result)
                moved = _move_job(job_dir, done_dir)
        if moved:
            collected.append(entry)
    return collected


def pending_jobs(*, data_root: Path | str | None = None) -> list[dict]:
    """Summaries for the UI: what is queued and what awaits collection."""
    pending = queue_root(data_root) / "pending"
    try:
        entries = sorted(pending.iterdir())
    except OSError:
        return []
    jobs: list[dict] = []
    for job_dir in entries:
        try:
            if not job_dir.is_dir():
                continue
            ready = ((job_dir / DONE_FILE).exists()
                     or (job_dir / ERROR_FILE).exists())
        except OSError:
            continue
        context, submitted_at, image_file = _job_summary(job_dir)
        jobs.append({
            "job_id": job_dir.name,
            "submitted_at": submitted_at,
            "image_file": image_file,
            "context": context,
            "ready": ready,
        })
    jobs.sort(key=lambda job: (job["submitted_at"], job["job_id"]))
    return jobs


# --- INSTRUCTIONS.md ---------------------------------------------------------
#
# Written for the EXTERNAL MODEL's operator, who has no access to this
# codebase. The first line is a version header; submit() rewrites the file
# whenever the header on disk differs, so protocol changes propagate.

_INSTRUCTIONS_TEXT = INSTRUCTIONS_HEADER + """

# External OCR queue — how to process these jobs

This folder is a file-based job queue. A desktop application (the Library
Tool) drops OCR jobs into `pending/`; you — an external OCR model or its
operator — process each job and write the result back beside the input.
The desktop application periodically collects finished jobs and moves them
to `done/` or `failed/`. You never need network access to this application;
the files below are the entire interface.

## Folder layout

    pending/<job_id>/     one directory per job, named by a UUID
        input.<ext>       the image to OCR (jpg / png / webp / tif / bin)
        job.json          job metadata + the exact result contract (below)
        result.json       YOU write this (see schema below)
        DONE              YOU create this EMPTY file — but only after
                          result.json is completely written
        ERROR             alternative to DONE: a plain-text file whose
                          content is the reason this job failed
    done/                 collected successful jobs (managed by the app)
    failed/               collected failed/rejected jobs (managed by the app)

## Processing a job

1. Read `job.json`. It names the image file, its SHA-256, media type and
   pixel dimensions, and repeats the full result contract.
2. OCR the image file.
3. Write `result.json` in the SAME job directory, exactly matching the
   schema below. Write it COMPLETELY — write to a temporary name and rename
   if you can.
4. Only after `result.json` is fully on disk, create an empty file named
   `DONE` in the job directory. The collector ignores the job until `DONE`
   exists, so a half-written `result.json` is harmless as long as `DONE`
   comes last.
5. If you cannot process the job, instead write a file named `ERROR` whose
   text content is a short human-readable reason.

Never modify `input.<ext>` or `job.json`, and never delete a job directory;
the collector moves directories itself.

## result.json schema (`org.whl.external-ocr-result/1`)

```json
{
  "schema": "org.whl.external-ocr-result/1",
  "job_id": "<the job directory's UUID, copied from job.json>",
  "engine": {
    "id": "my-ocr",
    "model": "my-ocr-v2.1"
  },
  "text": "THE HERBALL\\nor Generall Historie of Plantes.\\nLondon, 1597.",
  "regions": [
    {
      "id": "p0-r0",
      "type": "title",
      "text": "THE HERBALL",
      "polygon": [[0.12, 0.08], [0.88, 0.08], [0.88, 0.21], [0.12, 0.21]],
      "confidence": 0.97
    },
    {
      "id": "p0-r1",
      "type": "text",
      "text": "or Generall Historie of Plantes.",
      "polygon": [[0.15, 0.24], [0.85, 0.24], [0.85, 0.31], [0.15, 0.31]],
      "confidence": 0.92
    }
  ]
}
```

Field rules:

* `schema`   — exactly `"org.whl.external-ocr-result/1"`.
* `job_id`   — must equal the job directory name / `job.json`'s `job_id`.
* `engine.id`    — a short identifier for your model (max 80 chars).
* `engine.model` — the model name/version (max 120 chars).
* `text`     — the full plain text of the image in reading order
               (max 1,000,000 characters). Required; may be `""`.
* `regions`  — zero or more located regions (max 500). Emit whatever
               granularity your model has — lines, paragraphs, or typed
               layout blocks. Each region:
  * `id`         — unique within this result, max 120 chars. Suggested
                   convention: `p<page>-r<n>` (or `p<page>-l<n>` for lines).
  * `type`       — a lowercase word such as `text`, `title`,
                   `section_header`, `image`, `table`, `header`, `footer`,
                   `page_number`, `list`, `caption`. Use `text` when unsure.
  * `text`       — the text of that region (max 10,000 chars; `""` for
                   figures).
  * `polygon`    — 3 to 16 `[x, y]` vertices in drawing order.
  * `confidence` — a number 0..1, or `null` when your model has none.

## Coordinate conventions

* Coordinates are NORMALIZED to the input image: `x = pixel_x / width`,
  `y = pixel_y / height`, using the `width`/`height` in `job.json` (the
  image file's own pixel size).
* The origin is the TOP-LEFT corner; x grows rightward, y grows downward.
* Every coordinate must be a finite number in `[0, 1]`. Do not send pixel
  values.
* An axis-aligned box becomes 4 vertices: top-left, top-right,
  bottom-right, bottom-left.

## What the collector does

Once `DONE` exists, the application validates `result.json` strictly
against the rules above. A valid result moves the whole job directory to
`done/`; an invalid one moves it to `failed/` with a
`COLLECT_DIAGNOSTIC.txt` naming the first problem found. An `ERROR` file
moves the job to `failed/` with your message preserved. The `context`
values inside `job.json` are internal application bookkeeping — echo
nothing, change nothing, they ride with the directory.
"""


def _refresh_instructions(root: Path) -> None:
    path = root / "INSTRUCTIONS.md"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline().strip()
    except OSError:
        first_line = ""
    if first_line == INSTRUCTIONS_HEADER:
        return
    _write_atomic(path, _INSTRUCTIONS_TEXT.encode("utf-8"))
