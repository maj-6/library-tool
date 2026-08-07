"""Backfill desktop-pinned OCR geometry for already-imported phone captures.

Phone captures record Mistral OCR region polygons against the camera
original.  The desktop import derives its own display JPEG, so those
polygons only project when a remapped copy pinned to the derivative's
content hash exists (``librarytool.processing.capture_geometry``).  Imports
newer than this tool write that record inline; this tool adds it to captures
imported before it existed.

For every ``captures/<id>/photo_assets.json`` with phone geometry and a
desktop import row, the original bytes are re-derived with the same traced
pipeline the import uses.  Only when the re-derived bytes hash-match the
stored derivative is the recovered transform trusted and the remapped
record appended — a hash mismatch (e.g. a detector version drift changed
the warp) is reported and left alone rather than guessed at.

Usage:
  python tools/backfill_capture_geometry.py           # dry run, report only
  python tools/backfill_capture_geometry.py --apply   # write records

Close the desktop app first when applying: writes are atomic per file, but
the sidecar's projection caches only refresh on stat changes it observes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capture_pipeline as capture  # noqa: E402
from libcommon import DATA_ROOT  # noqa: E402


def _asset_import_rows(manifest: dict) -> dict[str, dict]:
    rows = {}
    desktop_import = manifest.get("desktop_import") or {}
    for row in desktop_import.get("assets") or []:
        if isinstance(row, dict) and isinstance(row.get("asset_id"), str):
            rows.setdefault(row["asset_id"], row)
    return rows


def _asset_pinned(asset: dict, derivative_sha: str) -> bool:
    """True when this asset already carries a record for these bytes."""

    return any(
        isinstance(record, dict)
        and record.get("display_sha256") == derivative_sha
        for record in (asset.get("geometry") or [])
    )


def _needs_backfill(asset: dict, derivative_sha: str) -> bool:
    geometry = asset.get("geometry")
    if not isinstance(geometry, list):
        return False
    has_phone_regions = any(
        isinstance(record, dict)
        and not record.get("display_sha256")
        and record.get("regions")
        for record in geometry
    )
    already_pinned = any(
        isinstance(record, dict)
        and record.get("display_sha256") == derivative_sha
        for record in geometry
    )
    return has_phone_regions and not already_pinned


def backfill_capture(directory: Path, *, apply: bool) -> tuple[str, str]:
    """Process one capture directory. Returns (status, detail)."""

    manifest_path = directory / "photo_assets.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "error", f"unreadable manifest ({type(exc).__name__})"
    assets = manifest.get("assets")
    import_rows = _asset_import_rows(manifest)
    if not isinstance(assets, list) or not import_rows:
        return "skipped", "no desktop import"

    changed = 0
    mismatched = 0
    examined = 0
    already = 0
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        row = import_rows.get(str(asset.get("asset_id")))
        if not row:
            continue
        derivative_sha = str(row.get("derivative_checksum") or "")
        raw_ref = str(row.get("raw_ref") or "")
        if not derivative_sha or not raw_ref:
            continue
        if not _needs_backfill(asset, derivative_sha):
            if _asset_pinned(asset, derivative_sha):
                already += 1
            continue
        examined += 1
        try:
            raw = (directory / raw_ref).read_bytes()
        except OSError:
            mismatched += 1
            continue
        if str(row.get("source_checksum") or "") != hashlib.sha256(
                raw).hexdigest():
            mismatched += 1
            continue
        try:
            derived, trace = capture.process_photo_traced(raw)
        except Exception:  # noqa: BLE001 - a bad photo must not stop the sweep
            mismatched += 1
            continue
        if hashlib.sha256(derived).hexdigest() != derivative_sha:
            # The current detector no longer reproduces the stored
            # derivative. Try the no-warp derivation before giving up: the
            # common drift is quad detection, not the standardize encode.
            try:
                rederived = capture.standardize(raw)
            except Exception:  # noqa: BLE001
                mismatched += 1
                continue
            if hashlib.sha256(rederived).hexdigest() != derivative_sha:
                mismatched += 1
                continue
            # The stored bytes are the UNWARPED derivation, so the trace's
            # final_size still describes the warped one it just disproved.
            # Carrying it through would stamp the record with dimensions
            # the display does not have, and projection would reject it as
            # stale — silently losing exactly the geometry this recovers.
            with Image.open(io.BytesIO(rederived)) as image:
                rederived_size = image.size
            trace = {
                **trace,
                "quad": None,
                "warp_size": None,
                "final_size": rederived_size,
            }
        records = capture.remap_phone_geometry_for_import(
            asset.get("geometry"), trace, derivative_sha)
        if not records:
            continue
        asset["geometry"] = list(asset.get("geometry") or []) + records
        changed += 1

    if not changed:
        if mismatched:
            # A capture whose other photos are already pinned is partial,
            # not a mismatch: re-running cannot improve it, but the
            # geometry it does have is live. Reporting both as "mismatch"
            # made a healthy library look broken on the second run.
            if already:
                return "partial", (
                    f"{already} pinned, {mismatched}/{examined} undecidable")
            return "mismatch", f"{mismatched}/{examined} photos undecidable"
        return "current", ""
    if apply:
        serialized = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        temp_path = manifest_path.with_name(manifest_path.name + ".tmp")
        temp_path.write_text(serialized, encoding="utf-8")
        os.replace(temp_path, manifest_path)
    note = f"{changed} asset(s) pinned"
    if mismatched:
        note += f", {mismatched} undecidable"
    return ("updated" if apply else "would-update"), note


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pin phone OCR geometry to desktop display derivatives")
    parser.add_argument("--apply", action="store_true",
                        help="write updated manifests (default: dry run)")
    parser.add_argument("--data-root", default="",
                        help="override the data root (default: resolved "
                             "DATA_ROOT)")
    args = parser.parse_args()

    root = Path(args.data_root).expanduser() if args.data_root else DATA_ROOT
    captures = root / "captures"
    if not captures.is_dir():
        print(f"no captures directory at {captures}")
        return 1

    counts: dict[str, int] = {}
    for directory in sorted(captures.iterdir()):
        if not directory.is_dir():
            continue
        if not (directory / "photo_assets.json").exists():
            counts["no-manifest"] = counts.get("no-manifest", 0) + 1
            continue
        status, detail = backfill_capture(directory, apply=args.apply)
        counts[status] = counts.get(status, 0) + 1
        if status in {"updated", "would-update", "partial",
                      "mismatch", "error"}:
            print(f"{status:>13}  {directory.name}  {detail}")
    print()
    for status in sorted(counts):
        print(f"{counts[status]:>5}  {status}")
    if not args.apply and counts.get("would-update"):
        print("\nDry run — pass --apply to write these records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
