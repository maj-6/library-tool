#!/usr/bin/env python3
"""Set up and inspect the Library Tool Supabase project.

    python3 tools/cloud_setup.py check      what exists, what is missing
    python3 tools/cloud_setup.py buckets    create the storage buckets
    python3 tools/cloud_setup.py seed       publish local builds as volumes (metadata only)
    python3 tools/cloud_setup.py anon-key   print the website's config snippet

Tables need DDL, and PostgREST cannot run DDL — paste docs/cloud/schema.sql into
the Supabase SQL Editor once. Everything else this script does directly, because
the Storage and REST APIs both accept the service_role key.

Credentials come from the desktop's settings (output/client_state.json), or from
SUPABASE_URL / SUPABASE_KEY in the environment. They are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libcommon as lib          # noqa: E402
import supabase_sync as sb       # noqa: E402

TABLES = ["captures", "books", "volumes", "releases", "profiles", "events"]
BUCKETS = {"captures": False, "volumes": True}       # name -> public?


def config() -> dict:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not (url and key):
        state = lib.load_json(lib.CLIENT_STATE_PATH, {}).get("settings", {})
        url = url or str(state.get("supabaseUrl") or "")
        key = key or str(state.get("supabaseKey") or "")
    if not (url and key):
        sys.exit("No Supabase URL/key. Set them in Settings > Sync, or export "
                 "SUPABASE_URL and SUPABASE_KEY.")
    return {"url": url.rstrip("/"), "key": key}


def key_role(key: str) -> str:
    """service_role or anon, read straight out of the JWT (no verification)."""
    import base64
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "?")
    except Exception:
        return "?"


def existing_tables(cfg: dict) -> set[str]:
    """PostgREST publishes an OpenAPI document naming every table it can see."""
    url, _, headers = sb._cfg(cfg)
    raw = sb._request("GET", f"{url}/rest/v1/", headers)
    spec = json.loads(raw.decode("utf-8", "replace"))
    return set(spec.get("definitions", {}))


def existing_buckets(cfg: dict) -> dict[str, bool]:
    url, _, headers = sb._cfg(cfg)
    raw = sb._request("GET", f"{url}/storage/v1/bucket", headers)
    return {b["name"]: bool(b.get("public")) for b in json.loads(raw.decode())}


def cmd_check(args) -> None:
    cfg = config()
    ref = cfg["url"].split("//")[-1].split(".")[0]
    print(f"project  {ref[:6]}…{ref[-4:]}   key role: {key_role(cfg['key'])}")
    if key_role(cfg["key"]) != "service_role":
        print("  ! the desktop needs the service_role key, not the anon key")

    try:
        tables = existing_tables(cfg)
    except sb.SyncError as exc:
        sys.exit(f"cannot reach PostgREST: {exc}")
    print("\ntables")
    missing = []
    for t in TABLES:
        ok = t in tables
        missing += [] if ok else [t]
        print(f"  {'ok  ' if ok else 'MISS'}  {t}")

    print("\nbuckets")
    try:
        buckets = existing_buckets(cfg)
    except sb.SyncError as exc:
        buckets = {}
        print(f"  cannot list: {exc}")
    for name, public in BUCKETS.items():
        have = buckets.get(name)
        if have is None:
            print(f"  MISS  {name}")
        elif have != public:
            print(f"  WARN  {name}  public={have}, expected {public}")
        else:
            print(f"  ok    {name}  {'public' if public else 'private'}")

    if missing:
        print(f"\n{len(missing)} table(s) missing. Paste docs/cloud/schema.sql into the")
        print("Supabase SQL Editor and run it, then re-run this check.")
    if any(b not in buckets for b in BUCKETS):
        print("\nMissing buckets:  python3 tools/cloud_setup.py buckets")
    if not missing and all(b in buckets for b in BUCKETS):
        n = sb._rest(cfg, "GET", "volumes?select=id")
        print(f"\nEverything is in place. volumes: {len(n or [])}")


def cmd_buckets(args) -> None:
    cfg = config()
    url, _, headers = sb._cfg(cfg)
    have = existing_buckets(cfg)
    for name, public in BUCKETS.items():
        if name in have:
            print(f"  {name}: exists (public={have[name]})")
            continue
        if args.dry_run:
            print(f"  {name}: would create (public={public})")
            continue
        h = dict(headers, **{"Content-Type": "application/json"})
        body = json.dumps({"id": name, "name": name, "public": public}).encode()
        sb._request("POST", f"{url}/storage/v1/bucket", h, body)
        print(f"  {name}: created (public={public})")
    if args.dry_run:
        print("\n(dry run — pass --apply to create them)")


def slugify(title: str, year, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{title} {year or ''}".lower()).strip("-")[:60]
    slug, n = base or "volume", 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def volume_rows(ready_only: bool = False, actor: str = "") -> list[dict]:
    """The local builds, shaped as `volumes` rows. No PDFs, metadata only."""
    builds = lib.load_json(lib.OUTPUT_DIR / "whl_builds.json", {})
    rows, taken = [], set()
    for b in builds.values():
        if not (b.get("title") or "").strip():
            continue
        if ready_only and b.get("status") not in ("ready", "uploaded"):
            continue
        year = str(b.get("year") or "")
        pages = str(b.get("pages") or "")
        rows.append({
            "slug": slugify(b["title"], year, taken),
            "title": b["title"],
            "subtitle": b.get("subtitle") or "",
            "authors": b.get("authors") or "",
            "year": int(year) if year.isdigit() else None,
            "publisher": b.get("publisher") or "",
            "publisher_city": b.get("publisher_city") or "",
            "edition": b.get("edition") or "",
            "language": b.get("language") or "",
            "pages": int(pages) if pages.isdigit() else None,
            "categories": b.get("categories") or "",
            "description": b.get("description") or "",
            "source_url": b.get("source_url") or b.get("pdf_source") or "",
            "uploaded_by_name": actor,
        })
    return rows


def cmd_seed(args) -> None:
    """Publish the local builds as volumes. Metadata only — no PDFs are sent."""
    cfg = config()
    rows = volume_rows(args.ready_only, args.actor)
    if not rows:
        sys.exit("no builds to seed")
    print(f"{len(rows)} volume(s):")
    for r in rows[:20]:
        print(f"  {r['slug']:<44} {r['title'][:40]}")
    if args.dry_run:
        print("\n(dry run — pass --apply to upsert)")
        return
    sb._rest(cfg, "POST", "volumes?on_conflict=slug", rows,
             prefer="resolution=merge-duplicates,return=minimal")
    print(f"\nupserted {len(rows)} volume(s)")


def cmd_fixture(args) -> None:
    """website/fixtures/volumes.json — lets the site be built and reviewed
    before the cloud holds a single row."""
    rows = volume_rows(args.ready_only, "")
    for i, r in enumerate(rows):
        r["created_at"] = f"2026-07-{(i % 28) + 1:02d}T00:00:00+00:00"
    out = Path(__file__).resolve().parent.parent / "website" / "fixtures" / "volumes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(Path.cwd())} ({len(rows)} volumes)")


def cmd_anon_key(_args) -> None:
    cfg = config()
    print("The website needs the ANON key, never the service_role key.")
    print("Supabase dashboard > Project Settings > API > anon public\n")
    print("Then write website/assets/config.js (it is gitignored):\n")
    print("  window.WHL_CONFIG = {")
    print(f'    supabaseUrl: "{cfg["url"]}",')
    print('    supabaseAnonKey: "<the anon key>",')
    print("  };")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="what exists, what is missing").set_defaults(fn=cmd_check)

    b = sub.add_parser("buckets", help="create the storage buckets")
    b.add_argument("--apply", dest="dry_run", action="store_false", default=True)
    b.set_defaults(fn=cmd_buckets)

    s = sub.add_parser("seed", help="publish local builds as volumes (metadata only)")
    s.add_argument("--apply", dest="dry_run", action="store_false", default=True)
    s.add_argument("--ready-only", action="store_true", help="skip drafts")
    s.add_argument("--actor", default="", help="name recorded as the uploader")
    s.set_defaults(fn=cmd_seed)

    f = sub.add_parser("fixture", help="write website/fixtures/volumes.json for offline dev")
    f.add_argument("--ready-only", action="store_true")
    f.set_defaults(fn=cmd_fixture)

    sub.add_parser("anon-key", help="print the website config snippet").set_defaults(fn=cmd_anon_key)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
