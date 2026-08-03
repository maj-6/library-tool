#!/usr/bin/env python3
"""Apply a staged capture review to the desktop catalogue.

The review (see ``staged_review.json``) is produced offline against the cloud
``captures`` rows: every proposed change is grounded in that capture's own OCR
text and was then adversarially re-checked against the same text.  This script
is the only step that touches live data, and it deliberately does nothing until
asked twice: ``--apply`` is required, and a timestamped backup is written first.

Why it edits ``manual_entries.json`` rather than pushing to the cloud directly:
that is the supported correction path.  ``_save_manual_entries`` stamps
``updated_at``, which is the ``manual_updated_at`` component of a capture's
projection vector clock; ``_capture_book_metadata_rows`` then projects the row
into ``capture_book_metadata`` and the phone picks it up.  Writing the cloud row
by hand would leave the desktop and phone disagreeing at the next projection.

**Only title, author and year reach the phone.**  ``_capture_bibliography``
publishes a deliberately bounded three-field snapshot, and Android's
``DesktopBookMetadata`` parses exactly those three.  Corrections to ``subtitle``
and ``publisher`` are applied to the desktop catalogue and are real, but they
stop at the desktop until that contract is widened on both sides.

Usage:
    python tools/apply_capture_review.py --review staged_review.json          # dry run
    python tools/apply_capture_review.py --review staged_review.json --apply
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Fields the review is scoped to. `volume`/`edition` are deliberately excluded:
# the review flags conflicts there rather than resolving them.
REVIEW_FIELDS = ("title", "subtitle", "author", "publisher", "year")
# Of those, the ones that actually reach the handset.
PHONE_VISIBLE = ("title", "author", "year")
# manual entries spell the author field `author`; builds use `authors`.
MANUAL_FIELD = {f: f for f in REVIEW_FIELDS}


def build_updated_at(previous: str = "") -> str:
    """A revision token strictly newer than ``previous``.

    Mirrors ``_build_updated_at`` in tools/whl_explorer/server.py. Without a
    strictly-advancing token the projection scores the row "equal projection
    source" and the correction silently never reaches the phone.
    """
    now = datetime.now(timezone.utc)
    try:
        prior = datetime.fromisoformat(str(previous or "").replace("Z", "+00:00"))
        prior = prior.replace(tzinfo=timezone.utc) if prior.tzinfo is None \
            else prior.astimezone(timezone.utc)
        if now <= prior:
            now = prior + timedelta(microseconds=1)
    except (TypeError, ValueError):
        pass
    return now.isoformat(timespec="microseconds")


def data_root() -> Path:
    env = os.environ.get("WHL_DATA_ROOT") or os.environ.get("DATA_ROOT")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ["APPDATA"]) / "Library Tool" / "output"
    return Path.home() / ".local" / "share" / "library-tool" / "output"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--review", required=True, help="staged_review.json")
    ap.add_argument("--data-root", default=None,
                    help="override DATA_ROOT (use a throwaway root to rehearse)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--flags", action="store_true",
                    help="also write review flags into each entry's `attention` field")
    args = ap.parse_args()

    review = json.loads(Path(args.review).read_text(encoding="utf-8"))
    root = Path(args.data_root) if args.data_root else data_root()
    entries_path = root / "manual_entries.json"
    if not entries_path.is_file():
        print(f"error: no manual_entries.json under {root}", file=sys.stderr)
        return 2

    entries = json.loads(entries_path.read_text(encoding="utf-8"))
    by_capture: dict[str, str] = {}
    for entry_id, entry in entries.items():
        capture_id = str((entry or {}).get("capture_id") or "").strip()
        if capture_id:
            by_capture[capture_id] = entry_id

    applied: list[tuple[str, str, str, str]] = []
    unmapped: list[str] = []
    flagged_written = 0
    phone_visible_changes = 0

    for capture in review.get("captures", []):
        capture_id = capture.get("capture_id")
        corrections = capture.get("corrections") or {}
        needs_flag = capture.get("needs_review") or not capture.get("is_book", True)
        if not corrections and not (needs_flag and args.flags):
            continue
        entry_id = by_capture.get(capture_id)
        if not entry_id:
            if corrections:
                unmapped.append(capture_id)
            continue
        entry = entries[entry_id]
        changed = False
        for field, change in corrections.items():
            if field not in REVIEW_FIELDS:
                continue
            key = MANUAL_FIELD[field]
            before = str(entry.get(key) or "")
            after = str(change.get("to") or "")
            if before == after:
                continue
            # Guard: only move a field the review actually looked at.
            if before != str(change.get("from") or ""):
                print(f"  SKIP {capture_id[:8]} {field}: live value "
                      f"{before[:40]!r} no longer matches reviewed "
                      f"{str(change.get('from'))[:40]!r}")
                continue
            entry[key] = after
            changed = True
            applied.append((capture_id, field, before, after))
            if field in PHONE_VISIBLE:
                phone_visible_changes += 1
        if needs_flag and args.flags:
            reason = str(capture.get("review_reason") or "Flagged by capture review")
            note = ("Review: not a book. " if not capture.get("is_book", True) else "Review: ")
            existing = str(entry.get("attention") or "")
            merged = (note + reason)[:1000]
            if existing != merged:
                entry["attention"] = merged
                changed = True
                flagged_written += 1
        if changed:
            entry["updated_at"] = build_updated_at(str(entry.get("updated_at") or ""))

    print(f"data root      : {root}")
    print(f"entries        : {len(entries)} ({len(by_capture)} carry a capture_id)")
    print(f"field changes  : {len(applied)}")
    print(f"  reaching the phone (title/author/year): {phone_visible_changes}")
    print(f"  desktop-only (subtitle/publisher)     : {len(applied) - phone_visible_changes}")
    if args.flags:
        print(f"attention flags: {flagged_written}")
    if unmapped:
        print(f"unmapped       : {len(unmapped)} captures with corrections are not "
              f"imported to the desktop yet; re-run after importing them")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
        for capture_id, field, before, after in applied[:20]:
            print(f"  {capture_id[:8]} {field}: {before[:44]!r} -> {after[:44]!r}")
        if len(applied) > 20:
            print(f"  ... and {len(applied) - 20} more")
        return 0

    backup = entries_path.with_suffix(
        f".bak.{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json")
    shutil.copy2(entries_path, backup)
    tmp = entries_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(entries_path)
    print(f"\nbackup  -> {backup}")
    print(f"written -> {entries_path}")
    print("Next: open Library Tool and let it push capture_book_metadata, "
          "or run the cloud sync, to carry title/author/year to the phone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
