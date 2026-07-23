#!/usr/bin/env python3
"""Set up and inspect the Library Tool Supabase project.

    python3 tools/cloud_setup.py check      what exists, what is missing
    python3 tools/cloud_setup.py buckets    create/repair the storage buckets
    python3 tools/cloud_setup.py seed       publish local builds as volumes (metadata only)
    python3 tools/cloud_setup.py anon-key   print the website's config snippet

Tables need DDL, and PostgREST cannot run DDL — paste the files under
docs/cloud/migrations/ into the Supabase SQL Editor, in order; `check` names
the pending ones. Everything else this script does directly, because the
Storage and REST APIs both accept the service_role key.

Set SUPABASE_KEY in the environment for owner operations. SUPABASE_URL is
optional for the built-in project and required when targeting another one.
Credentials are never read from desktop state or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_credentials          # noqa: E402
import cloud_defaults            # noqa: E402
import libcommon as lib          # noqa: E402
import supabase_sync as sb       # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "docs" / "cloud" / "migrations"
# The expected tables and their columns come out of the migrations' DDL
# (expected_schema below) — one source of truth. Views are few enough to
# name by hand: view -> the columns its consumers select.
VIEWS = {"author_index": ["author", "work_count"]}
BUCKETS = {
    "captures": False,
    "capture-derivatives": False,
    "volumes": True,
}                                                       # name -> public?
BUCKET_OPTIONS = {
    "captures": {
        "file_size_limit": 32 * 1024 * 1024,
        "allowed_mime_types": ["image/jpeg"],
    },
    "capture-derivatives": {
        "file_size_limit": 32 * 1024 * 1024,
        "allowed_mime_types": ["image/jpeg", "application/json"],
    },
}
# Role smoke tests: the website reads ANON_CAN with the anon key; ANON_CANNOT
# holds user data behind revoked grants — an anon read succeeding on any of
# them means the revoke/RLS blocks were not applied. passages is RPC-only by
# design (docs/search-design.md D6): anon reaches it through search_passages,
# never a table read.
ANON_CAN = [
    "volumes", "volume_pages", "releases", "index_versions",
    "android_ui_catalog",
]
ANON_CANNOT = [
    "profiles", "events", "captures", "photo_processing_jobs", "passages",
    "collections", "android_ui_publishers", "capture_book_metadata",
    "capture_reviews",
]


def config() -> dict:
    return cli_credentials.supabase_service_config(
        default_url=cloud_defaults.SUPABASE_URL
    )


def key_role(key: str) -> str:
    """Identify modern opaque keys or read a legacy JWT role without verification."""
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "publishable"
    import base64
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "?")
    except Exception:
        return "?"


def migration_files() -> list[Path]:
    """docs/cloud/migrations/*.sql, in apply order (NNN_ prefixes sort)."""
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


_CREATE_RE = re.compile(
    r"^\s*create table if not exists (?:public\.)?(\w+)\s*\("
)
_ADD_RE = re.compile(
    r"^\s*alter table (?:public\.)?(\w+) "
    r"add column (?:if not exists )?(\w+)"
)
_DROP_RE = re.compile(
    r"^\s*alter table (?:public\.)?(\w+) "
    r"drop column (?:if exists )?(\w+)"
)
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*")
_CONSTRAINTS = {"primary", "unique", "check", "foreign", "constraint", "like"}


def expected_schema(sql: str) -> dict[str, set[str]]:
    """table -> columns, read straight out of the migrations' DDL.

    Understands the schema conventions only: `create table if not exists`
    blocks with one column per line, plus single-line
    `alter table X add column [if not exists] C` / `drop column [if exists] C`
    heads (the definition may continue on later lines).
    """
    tables: dict[str, set[str]] = {}
    current: set[str] | None = None
    depth = 0
    for raw in sql.splitlines():
        line = raw.split("--")[0].rstrip()
        if current is None:
            m = _CREATE_RE.match(line)
            if m:
                current = tables.setdefault(m.group(1), set())
                depth = line.count("(") - line.count(")")
                continue
            m = _ADD_RE.match(line)
            if m:
                tables.setdefault(m.group(1), set()).add(m.group(2))
                continue
            m = _DROP_RE.match(line)
            if m:
                tables.setdefault(m.group(1), set()).discard(m.group(2))
            continue
        stripped = line.strip()
        if stripped and depth == 1:
            m = _IDENT_RE.match(stripped)
            if m and m.group(0) not in _CONSTRAINTS:
                current.add(m.group(0))
        depth += line.count("(") - line.count(")")
        if depth <= 0:
            current = None
    return tables


def pending_migrations(local: list[str], applied: set[str]) -> list[str]:
    """The migration ids not yet recorded on the project, in apply order."""
    return [m for m in local if m not in applied]


def openapi_definitions(cfg: dict) -> dict[str, set[str]]:
    """PostgREST's OpenAPI document names every table and view it can see,
    each with its columns under `properties`."""
    url, _, headers = sb._cfg(cfg)
    raw = sb._request("GET", f"{url}/rest/v1/", headers)
    spec = json.loads(raw.decode("utf-8", "replace"))
    return {name: set(d.get("properties") or {})
            for name, d in (spec.get("definitions") or {}).items()}


def applied_migrations(cfg: dict) -> set[str] | None:
    """Ids recorded in schema_migrations; None when the ledger is missing."""
    try:
        rows = sb._rest(cfg, "GET", "schema_migrations?select=id")
    except sb.SyncError:
        return None
    return {r["id"] for r in rows or []}


def anon_selects(cfg: dict, table: str) -> bool:
    """Whether this (anon) credential can read the table at all."""
    try:
        sb._rest(cfg, "GET", f"{table}?select=*&limit=1")
        return True
    except sb.SyncError:
        return False


def anon_config(cfg: dict) -> dict | None:
    """The anon-key twin of config(), for the role smoke tests."""
    key = str(os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    if not key and cfg["url"] == cloud_defaults.SUPABASE_URL:
        key = cloud_defaults.SUPABASE_ANON_KEY
    return {"url": cfg["url"], "key": key} if key else None


def existing_buckets(cfg: dict) -> dict[str, bool]:
    url, _, headers = sb._cfg(cfg)
    raw = sb._request("GET", f"{url}/storage/v1/bucket", headers)
    return {b["name"]: bool(b.get("public")) for b in json.loads(raw.decode())}


def cmd_check(args) -> None:
    cfg = config()
    ref = cfg["url"].split("//")[-1].split(".")[0]
    print(f"project  {ref[:6]}…{ref[-4:]}   key role: {key_role(cfg['key'])}")
    if key_role(cfg["key"]) not in {"service_role", "secret"}:
        print("  ! owner setup needs a secret key, not a public/anon key")

    bad: list[str] = []
    try:
        live = openapi_definitions(cfg)
    except sb.SyncError as exc:
        sys.exit(f"cannot reach PostgREST: {exc}")
    expected = expected_schema("\n".join(
        p.read_text(encoding="utf-8") for p in migration_files()))

    print("\ntables")
    for t in sorted(expected):
        if t not in live:
            bad.append(f"table {t} missing")
            print(f"  MISS  {t}")
        elif expected[t] - live[t]:
            cols = ", ".join(sorted(expected[t] - live[t]))
            bad.append(f"{t} lacks column(s): {cols}")
            print(f"  COLS  {t}  missing: {cols}")
        else:
            print(f"  ok    {t}")

    print("\nviews")
    for v, want in VIEWS.items():
        if v not in live:
            bad.append(f"view {v} missing")
            print(f"  MISS  {v}")
        elif set(want) - live[v]:
            cols = ", ".join(sorted(set(want) - live[v]))
            bad.append(f"{v} lacks column(s): {cols}")
            print(f"  COLS  {v}  missing: {cols}")
        else:
            print(f"  ok    {v}")

    print("\nmigrations")
    local = [p.stem for p in migration_files()]
    applied = applied_migrations(cfg)
    if applied is None:
        pending = local
        print("  no schema_migrations table — every migration is pending")
    else:
        pending = pending_migrations(local, applied)
        for m in local:
            print(f"  {'PEND' if m in pending else 'ok  '}  {m}")
        unknown = sorted(applied - set(local))
        if unknown:
            print(f"  note: applied but not local: {', '.join(unknown)}")
    if pending:
        bad.append(f"{len(pending)} pending migration(s): {', '.join(pending)}")

    print("\nbuckets")
    try:
        buckets = existing_buckets(cfg)
    except sb.SyncError as exc:
        buckets = {}
        bad.append(f"cannot list buckets: {exc}")
        print(f"  cannot list: {exc}")
    for name, public in BUCKETS.items():
        have = buckets.get(name)
        if have is None:
            bad.append(f"bucket {name} missing")
            print(f"  MISS  {name}")
        elif have != public:
            bad.append(f"bucket {name} public={have}, expected {public}")
            print(f"  FAIL  {name}  public={have}, expected {public}")
        else:
            print(f"  ok    {name}  {'public' if public else 'private'}")

    print("\nanon role")
    anon = anon_config(cfg)
    if anon is None:
        print("  skipped — no anon key (set SUPABASE_ANON_KEY)")
    else:
        for t in ANON_CAN:
            ok = anon_selects(anon, t)
            bad += [] if ok else [f"anon cannot select {t}"]
            print(f"  {'PASS' if ok else 'FAIL'}  anon can select {t}")
        for t in ANON_CANNOT:
            ok = not anon_selects(anon, t)
            bad += [] if ok else [f"anon can select {t}"]
            print(f"  {'PASS' if ok else 'FAIL'}  anon cannot select {t}")

    if bad:
        print(f"\n{len(bad)} problem(s)")
        for b in bad:
            print(f"  - {b}")
        if pending:
            print("\nPaste the pending docs/cloud/migrations/ files into the")
            print("Supabase SQL Editor, in order, then re-run this check.")
        if any(b.startswith("bucket ") for b in bad):
            print("Missing buckets:  python3 tools/cloud_setup.py buckets --apply")
        sys.exit(1)
    n = sb._rest(cfg, "GET", "volumes?select=id")
    print(f"\nEverything is in place. volumes: {len(n or [])}")


def cmd_buckets(args) -> None:
    cfg = config()
    url, _, headers = sb._cfg(cfg)
    have = existing_buckets(cfg)
    for name, public in BUCKETS.items():
        options = BUCKET_OPTIONS.get(name, {})
        body_value = {"public": public, **options}
        if name in have:
            if args.dry_run:
                if options or have[name] != public:
                    print(f"  {name}: would enforce private/type/size settings")
                else:
                    print(f"  {name}: exists (public={have[name]})")
                continue
            if options or have[name] != public:
                h = dict(headers, **{"Content-Type": "application/json"})
                body = json.dumps(body_value).encode()
                sb._request("PUT", f"{url}/storage/v1/bucket/{name}", h, body)
                print(f"  {name}: updated (public={public}, restrictions enforced)")
            else:
                print(f"  {name}: exists (public={have[name]})")
            continue
        if args.dry_run:
            print(f"  {name}: would create (public={public})")
            continue
        h = dict(headers, **{"Content-Type": "application/json"})
        body = json.dumps({"id": name, "name": name, **body_value}).encode()
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


def volume_rows(ready_only: bool = False, actor: str = "") -> list[tuple[str, dict]]:
    """The local builds as (build_id, `volumes` row). No PDFs, metadata only.

    A build's slug is sticky: once seeded or published it is written back onto
    the build. The publish path then fills THIS row in, rather than deduping to
    `-2` beside it, and two books sharing a title and a year never collide.
    """
    builds = lib.load_json(lib.OUTPUT_DIR / "whl_builds.json", {})
    nodes = lib.load_taxonomy()["nodes"]
    rows: list[tuple[str, dict]] = []
    taken = {str(b.get("published_slug") or "") for b in builds.values()} - {""}
    for bid, b in builds.items():
        if not (b.get("title") or "").strip():
            continue
        if ready_only and b.get("status") not in ("ready", "uploaded"):
            continue
        year = str(b.get("year") or "")
        pages = str(b.get("pages") or "")
        slug = str(b.get("published_slug") or "") or slugify(b["title"], year, taken)
        # taxonomy paths when assigned, the deprecated free text otherwise
        paths = lib.category_paths(nodes, b.get("category_ids"))
        cats = lib.categories_text(paths) if paths else (b.get("categories") or "")
        rows.append((bid, {
            "slug": slug,
            "title": b["title"],
            "subtitle": b.get("subtitle") or "",
            "authors": b.get("authors") or "",
            "year": int(year) if year.isdigit() else None,
            "publisher": b.get("publisher") or "",
            "publisher_city": b.get("publisher_city") or "",
            "edition": b.get("edition") or "",
            "volume": b.get("volume") or "",
            "group_id": b.get("group_id") or "",
            "language": b.get("language") or "",
            "pages": int(pages) if pages.isdigit() else None,
            "categories": cats,
            "category_paths": paths,
            "description": b.get("description") or "",
            "source_url": b.get("source_url") or b.get("pdf_source") or "",
            "uploaded_by_name": actor,
        }))
    return rows


def cmd_seed(args) -> None:
    """Publish the local builds as volumes. Metadata only, no PDFs are sent."""
    cfg = config()
    pairs = volume_rows(args.ready_only, args.actor)
    if not pairs:
        sys.exit("no builds to seed")
    print(f"{len(pairs)} volume(s):")
    for _bid, r in pairs[:20]:
        print(f"  {r['slug']:<44} {r['title'][:40]}")
    if args.dry_run:
        print("\n(dry run, pass --apply to upsert)")
        return
    try:
        sb._rest(cfg, "POST", "volumes?on_conflict=slug", [r for _b, r in pairs],
                 prefer="resolution=merge-duplicates,return=minimal")
    except sb.SyncError as exc:
        optional = ("category_paths", "volume", "group_id")
        if not any(k in str(exc) for k in optional):
            raise
        # Older projects can publish while optional metadata awaits schema sync.
        print("note: optional volumes metadata is missing on the cloud project;"
              " apply the pending docs/cloud/migrations (see `check`)")
        rows = [{k: v for k, v in r.items() if k not in optional}
                for _b, r in pairs]
        sb._rest(cfg, "POST", "volumes?on_conflict=slug", rows,
                 prefer="resolution=merge-duplicates,return=minimal")

    # Remember which slug each build owns, so publishing later updates that very
    # row instead of creating a second one beside it.
    path = lib.OUTPUT_DIR / "whl_builds.json"
    builds = lib.load_json(path, {})
    changed = 0
    for bid, r in pairs:
        if bid in builds and builds[bid].get("published_slug") != r["slug"]:
            builds[bid]["published_slug"] = r["slug"]
            changed += 1
    if changed:
        lib.save_json(path, builds)
    print(f"\nupserted {len(pairs)} volume(s); recorded {changed} slug(s) on the builds")


def cmd_fixture(args) -> None:
    """website/fixtures/volumes.json — lets the site be built and reviewed
    before the cloud holds a single row."""
    rows = [r for _bid, r in volume_rows(args.ready_only, "")]
    for i, r in enumerate(rows):
        r["created_at"] = f"2026-07-{(i % 28) + 1:02d}T00:00:00+00:00"
    out = Path(__file__).resolve().parent.parent / "website" / "fixtures" / "volumes.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out.relative_to(Path.cwd())} ({len(rows)} volumes)")


def cmd_r2(args) -> None:
    """Prove the R2 credentials work, end to end, before a 129 MB upload does not.

    Lists buckets, uploads a probe, HEADs it, fetches it through the PUBLIC url
    with no credentials at all, and deletes it. Note the browser User-Agent: the
    r2.dev domain sits behind Cloudflare's bot check, which answers a bare
    `Python-urllib` with 403 error-code-1010 and looks exactly like the bucket
    not being public.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import r2_store as r2
    import tempfile
    import urllib.request
    import urllib.error

    cfg = cli_credentials.r2_config(require_public_base=True)

    print("buckets:", r2.list_buckets(cfg))
    if cfg["bucket"] not in r2.list_buckets(cfg):
        print(f"  ! configured bucket {cfg['bucket']!r} is not among them")

    probe = Path(tempfile.gettempdir()) / "whl_r2_probe.txt"
    probe.write_bytes(b"library-tool probe\n")
    key = "volumes/_probe.txt"
    url = r2.put_file(cfg, key, probe, "text/plain")
    print("upload  ok ->", url)
    print("head    ok ->", r2.head(cfg, key))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chrome/120.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"public  ok -> HTTP {r.status}, {len(r.read())} bytes")
    except urllib.error.HTTPError as exc:
        print(f"public  FAILED -> HTTP {exc.code}: enable the bucket's public "
              f"development URL, or set a custom domain")
    r2.delete(cfg, key)
    probe.unlink(missing_ok=True)
    print("delete  ok")


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

    b = sub.add_parser("buckets", help="create or repair the storage buckets")
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

    sub.add_parser("r2", help="prove the R2 credentials work, end to end").set_defaults(fn=cmd_r2)
    sub.add_parser("anon-key", help="print the website config snippet").set_defaults(fn=cmd_anon_key)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
