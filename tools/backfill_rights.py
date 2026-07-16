#!/usr/bin/env python3
"""Backfill volumes.copyright_status from the builds' rights decisions.

Publishing now writes the status with every volumes row; rows published
before that carry ''. For every build with status "uploaded", a
published_slug and a rights decision, PATCH the matching row. Uploaded
builds WITHOUT a decision are listed and left alone — this script never
invents one; decide in the desktop Editor (Rights) and re-run.

    python3 tools/backfill_rights.py            # dry run
    python3 tools/backfill_rights.py --apply

Credentials come from the desktop's settings (output/client_state.json), or
from SUPABASE_URL / SUPABASE_KEY in the environment. They are never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_defaults            # noqa: E402
import libcommon as lib          # noqa: E402
import supabase_sync as sb       # noqa: E402

# Mirrors server.py's _RIGHTS_PUBLIC: how each decision reads on the site.
RIGHTS_PUBLIC = {"public-domain": "Public domain", "cleared": "Cleared",
                 "searchable-only": "Search only", "no-public-text": "Restricted"}


def config() -> dict:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        state = lib.load_json(lib.CLIENT_STATE_PATH, {}).get("settings", {})
        url = url or str(state.get("supabaseUrl") or "")
        key = key or str(state.get("supabaseKey") or "")
    url = url or cloud_defaults.SUPABASE_URL   # the key is a secret; the URL isn't
    if not key:
        sys.exit("No Supabase service key. Set it in Settings > Sync, or export "
                 "SUPABASE_URL and SUPABASE_KEY.")
    return {"url": url.rstrip("/"), "key": key}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="print what would change (the default)")
    ap.add_argument("--apply", dest="dry_run", action="store_false",
                    help="write the statuses")
    args = ap.parse_args()

    builds = lib.load_json(lib.OUTPUT_DIR / "whl_builds.json", {})
    todo, undecided = [], []
    for b in builds.values():
        slug = str(b.get("published_slug") or "").strip()
        if b.get("status") != "uploaded" or not slug:
            continue
        rights = str(b.get("rights") or "").strip()
        if rights in RIGHTS_PUBLIC:
            todo.append((slug, RIGHTS_PUBLIC[rights], str(b.get("title") or "")))
        else:
            undecided.append((slug, str(b.get("title") or "")))

    if todo:
        print(f"{len(todo)} volume(s) to backfill:")
        for slug, status, title in todo:
            print(f"  {slug:<44} -> {status:<13} {title[:36]}")
    if undecided:
        print(f"\n{len(undecided)} uploaded build(s) have no rights decision "
              "— untouched:")
        for slug, title in undecided:
            print(f"  {slug:<44} {title[:36]}")
    if not todo:
        sys.exit("\nnothing to backfill")
    if args.dry_run:
        print("\n(dry run — pass --apply to patch the rows)")
        return

    cfg = config()
    for slug, status, _title in todo:
        rows = sb._rest(cfg, "PATCH",
                        f"volumes?slug=eq.{urllib.parse.quote(slug)}",
                        {"copyright_status": status},
                        prefer="return=representation") or []
        print(f"  {slug}: "
              f"{'ok' if len(rows) == 1 else 'NO ROW — was it ever published?'}")


if __name__ == "__main__":
    main()
