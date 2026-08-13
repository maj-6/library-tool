#!/usr/bin/env python3
"""Run Mistral OCR 4 over extracted manuscript canvases, resumably.

The Mistral key is leased from Library Tool's current-user DPAPI store unless
MISTRAL_API_KEY is explicitly present in the environment. It is never accepted
on the command line or written to output. One raw, bounded JSON response is
persisted per canvas so a stopped run resumes without paying for finished work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import capture_pipeline  # noqa: E402
from librarytool.adapters.windows.secret_store import (  # noqa: E402
    SecretIdRegistry,
    WindowsDpapiSecretStoreRepository,
)
from librarytool.engine.secret_ids import LEGACY_SECRET_IDS  # noqa: E402


MODEL = "mistral-ocr-4-0"
SECRET_ID = "provider:mistral:api-key"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _credential(path: Path | None) -> Iterator[str]:
    environment = str(os.environ.get("MISTRAL_API_KEY") or "").strip()
    if environment:
        yield environment
        return

    if path is None:
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        path = appdata / "Library Tool" / "output" / "secrets.dpapi"
    if not path.is_file():
        raise SystemExit(
            "Mistral is not configured: set MISTRAL_API_KEY or configure it in "
            "Library Tool > Settings > Credentials"
        )
    initial = {
        secret_id: f"absent-v1-{legacy_key.lower()}"
        for legacy_key, secret_id in LEGACY_SECRET_IDS.items()
    }
    repository = WindowsDpapiSecretStoreRepository(
        path,
        registry=SecretIdRegistry(initial),
        store_id="librarytool.desktop.current-user.v1",
    )
    try:
        with repository.credential_leases.lease(SECRET_ID) as lease:
            yield lease.reveal()
    except Exception as exc:
        raise SystemExit(
            "The protected Mistral credential could not be leased for this user"
        ) from exc


def _response_document(pages: list[dict], *, canvas: dict, attempts: int) -> dict:
    return {
        "schema": "world-herb-library/ocr-engine-response/1.0",
        "canvas_id": canvas["id"],
        "source_member": canvas["member"],
        "source_sha256": canvas["sha256"],
        "provider": "mistral",
        "model_requested": MODEL,
        "engine_version": "ocr-4-blocks",
        "generated_at": _utc_now(),
        "attempts": attempts,
        "pages": pages,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--from", dest="first", type=int, default=7)
    parser.add_argument("--to", dest="last", type=int, default=101)
    parser.add_argument("--credential-store", type=Path)
    parser.add_argument("--timeout", type=float, default=240.0)
    parser.add_argument("--retries", type=int, default=3)
    args = parser.parse_args()

    manifest_path = args.manifest or args.images / "manifest.json"
    extraction = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected = [
        canvas
        for canvas in extraction["canvases"]
        if args.first <= int(canvas["sequence"]) <= args.last
    ]
    if not selected:
        raise SystemExit("no canvases selected")
    args.out.mkdir(parents=True, exist_ok=True)

    summary_path = args.out / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        summary = {
            "schema": "world-herb-library/ocr-run/1.0",
            "provider": "mistral",
            "model_requested": MODEL,
            "engine_version": "ocr-4-blocks",
            "started_at": _utc_now(),
            "canvas_range": [args.first, args.last],
            "canvases": {},
        }

    with _credential(args.credential_store) as key:
        for position, canvas in enumerate(selected, start=1):
            canvas_id = canvas["id"]
            output = args.out / f"{canvas_id}.json"
            if output.is_file():
                try:
                    existing = json.loads(output.read_text(encoding="utf-8"))
                    if existing.get("source_sha256") == canvas["sha256"]:
                        pages = existing.get("pages") or []
                        block_count = sum(
                            len(page.get("blocks") or [])
                            for page in pages
                            if isinstance(page, dict)
                        )
                        characters = sum(
                            len(str(page.get("markdown") or ""))
                            for page in pages
                            if isinstance(page, dict)
                        )
                        summary["canvases"][canvas_id] = {
                            "status": "complete",
                            "blocks": block_count,
                            "markdown_characters": characters,
                            "attempts": existing.get("attempts", 1),
                            "completed_at": existing.get("generated_at"),
                            "cached": True,
                        }
                        print(f"[{position}/{len(selected)}] {canvas_id}: cached", flush=True)
                        continue
                except (OSError, json.JSONDecodeError):
                    pass

            source = args.images / canvas["member"]
            payload = source.read_bytes()
            if _sha256(payload) != canvas["sha256"]:
                raise SystemExit(f"source checksum mismatch: {canvas_id}")

            error = None
            for attempt in range(1, args.retries + 1):
                try:
                    pages = capture_pipeline.mistral_ocr_pages(
                        payload,
                        key,
                        timeout=args.timeout,
                        want_images=False,
                        want_blocks=True,
                        confidence_scores_granularity="word",
                        model=MODEL,
                    )
                    document = _response_document(
                        pages, canvas=canvas, attempts=attempt
                    )
                    temporary = output.with_suffix(".json.partial")
                    temporary.write_text(
                        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    temporary.replace(output)
                    block_count = sum(len(page.get("blocks") or []) for page in pages)
                    characters = sum(len(str(page.get("markdown") or "")) for page in pages)
                    summary["canvases"][canvas_id] = {
                        "status": "complete",
                        "blocks": block_count,
                        "markdown_characters": characters,
                        "attempts": attempt,
                        "completed_at": document["generated_at"],
                    }
                    summary_path.write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    print(
                        f"[{position}/{len(selected)}] {canvas_id}: "
                        f"{block_count} blocks, {characters} chars",
                        flush=True,
                    )
                    error = None
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    error = exc
                    if attempt < args.retries:
                        time.sleep(min(8.0, 2.0 ** attempt))
            if error is not None:
                summary["canvases"][canvas_id] = {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "failed_at": _utc_now(),
                }
                summary_path.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(f"{canvas_id}: failed ({type(error).__name__})", file=sys.stderr)

    summary["finished_at"] = _utc_now()
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
