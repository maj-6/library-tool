#!/usr/bin/env python3
"""Backfill volumes.copyright_status from the builds' rights decisions.

Publishing now writes the status with every volumes row; rows published
before that carry ''. For every build with status "uploaded", a
published_slug and a rights decision, update the matching row. Restricted and
search-only decisions first remove any public page text / notes and clear their
asset flags, then publish the decision only after deletion is verified.
Uploaded builds WITHOUT a decision are listed and left alone — this script
never invents one; decide in the desktop Editor (Rights) and re-run.

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
TEXT_WITHHELD = frozenset({"searchable-only", "no-public-text"})
TEXT_ASSET_KEYS = frozenset({"pages", "translations", "notes"})


def _slug_filter(slug: str) -> str:
    """One literal slug in a PostgREST filter (never a pattern)."""
    return urllib.parse.quote(slug, safe="")


def _volume_for_prune(cfg: dict, slug: str) -> dict:
    """The one public row whose assets must converge with a restricted state."""
    rows = sb._rest(
        cfg, "GET",
        f"volumes?slug=eq.{_slug_filter(slug)}&select=slug,assets&limit=2",
    )
    if not isinstance(rows, list) or len(rows) != 1:
        raise sb.SyncError(f"expected one volumes row for {slug!r}, got "
                           f"{len(rows) if isinstance(rows, list) else 'an invalid response'}")
    row = rows[0]
    if not isinstance(row, dict) or "assets" not in row:
        raise sb.SyncError(f"volumes row for {slug!r} has no assets manifest")
    if row["assets"] is not None and not isinstance(row["assets"], dict):
        raise sb.SyncError(f"volumes row for {slug!r} has an invalid assets manifest")
    return row


def _assert_no_public_text(cfg: dict, slug: str) -> None:
    """Verify both anon-readable book-text tables are empty for this slug."""
    quoted = _slug_filter(slug)
    for table in ("volume_pages", "volume_notes"):
        rows = sb._rest(
            cfg, "GET", f"{table}?slug=eq.{quoted}&select=slug&limit=1",
        )
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise sb.SyncError(f"invalid verification response from {table}")
        if rows:
            raise sb.SyncError(f"{table} still contains public text for {slug!r}")


def _prune_public_text(cfg: dict, slug: str, assets: dict | None) -> dict:
    """Delete public book text and return the matching, text-free manifest.

    The deletes and their verification happen before copyright_status changes.
    A failure therefore leaves the public label at its previous value instead
    of claiming text is restricted while rows remain anonymously readable.
    """
    quoted = _slug_filter(slug)
    for table in ("volume_pages", "volume_notes"):
        sb._rest(cfg, "DELETE", f"{table}?slug=eq.{quoted}",
                 prefer="return=minimal")
    _assert_no_public_text(cfg, slug)
    return {key: value for key, value in (assets or {}).items()
            if key not in TEXT_ASSET_KEYS}


def apply_rights(cfg: dict, slug: str, rights: str) -> None:
    """Apply one decision, pruning disallowed artifacts before its public label."""
    if rights not in RIGHTS_PUBLIC:
        raise sb.SyncError(f"unknown rights decision {rights!r}")
    payload = {"copyright_status": RIGHTS_PUBLIC[rights]}
    if rights in TEXT_WITHHELD:
        row = _volume_for_prune(cfg, slug)
        payload["assets"] = _prune_public_text(cfg, slug, row.get("assets"))
    rows = sb._rest(
        cfg, "PATCH", f"volumes?slug=eq.{_slug_filter(slug)}", payload,
        prefer="return=representation",
    )
    if not isinstance(rows, list) or len(rows) != 1:
        raise sb.SyncError(f"rights update matched no unique volume for {slug!r}")


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
            todo.append((slug, rights, str(b.get("title") or "")))
        else:
            undecided.append((slug, str(b.get("title") or "")))

    if todo:
        print(f"{len(todo)} volume(s) to backfill:")
        for slug, rights, title in todo:
            print(f"  {slug:<44} -> {RIGHTS_PUBLIC[rights]:<13} {title[:36]}")
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
    failed = []
    for slug, rights, _title in todo:
        try:
            apply_rights(cfg, slug, rights)
        except sb.SyncError as exc:
            failed.append((slug, str(exc)))
            print(f"  {slug}: FAILED — {exc}")
        else:
            note = "; public page text/notes pruned" if rights in TEXT_WITHHELD else ""
            print(f"  {slug}: ok{note}")
    if failed:
        sys.exit(f"\n{len(failed)} rights backfill(s) failed; failed rows were not "
                 "reported as updated")


if __name__ == "__main__":
    main()
