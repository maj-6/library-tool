#!/usr/bin/env python3
"""Repoint volumes.pdf_url onto a CORS-enabled host (the R2 reader fix).

The website reader (website/assets/read.js, pdf.js) streams each scan with
cross-origin HTTP Range requests. Cloudflare's managed `pub-*.r2.dev` URLs send
no `Access-Control-Allow-Origin` and 403 the CORS preflight, so the browser
blocks every scan ("Cannot open ... blocked by cross-origin rules"). The durable
fix is to serve the same R2 objects from a *custom domain* carrying a CORS policy
(see docs/cloud/r2_cors_setup.md), then repoint the stored URLs. Object keys are
identical across hosts, so this only swaps scheme+host and keeps the path.

Config is loaded exactly like tools/cloud_setup.py: SUPABASE_URL / SUPABASE_KEY
env vars, else Settings > Sync (client_state). Listing needs only read access;
--apply writes and therefore requires the service_role key.

    # dry run -- show what would change (safe, read-only, anon key is enough)
    python3 tools/fix_pdf_url_host.py --to https://files.worldherblibrary.org
    # then actually write them (service_role key required)
    python3 tools/fix_pdf_url_host.py --to https://files.worldherblibrary.org --apply
    # restrict to rows still on a specific old host
    python3 tools/fix_pdf_url_host.py --from pub-xxxx.r2.dev --to https://... --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_setup            # noqa: E402
import supabase_sync as sb    # noqa: E402


def rewrite_host(url: str, new_base: str) -> str:
    """Return `url` with its scheme+host replaced by `new_base`; path kept."""
    parts = urlsplit(url)
    base = urlsplit(new_base.rstrip("/"))
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def plan(rows: list[dict], new_base: str, from_host: str) -> list[tuple[str, str, str]]:
    """(slug, old_url, new_url) for every row that needs repointing."""
    to_host = urlsplit(new_base).netloc
    out = []
    for r in rows or []:
        old = str(r.get("pdf_url") or "").strip()
        if not old:
            continue                                  # metadata-only, no scan
        host = urlsplit(old).netloc
        if host == to_host:
            continue                                  # already on the target
        if from_host and host != from_host:
            continue                                  # not the host we were told to move
        new = rewrite_host(old, new_base)
        if new != old:
            out.append((str(r.get("slug") or ""), old, new))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Repoint volumes.pdf_url onto a CORS-enabled host.")
    ap.add_argument("--to", required=True, metavar="BASE",
                    help="new public base, e.g. https://files.worldherblibrary.org")
    ap.add_argument("--from", dest="from_host", default="", metavar="HOST",
                    help="only rewrite rows whose pdf_url host is this "
                         "(default: every row not already on --to's host)")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (requires the service_role key); "
                         "without it this is a dry run")
    args = ap.parse_args(argv)

    if not args.to.startswith(("http://", "https://")):
        ap.error("--to must include the scheme, e.g. https://files.example.org")
    if urlsplit(args.to).netloc.endswith(".r2.dev"):
        print("! --to is an r2.dev host; those cannot serve CORS. Use a custom "
              "domain (see docs/cloud/r2_cors_setup.md).", file=sys.stderr)

    cfg = cloud_setup.config()
    if args.apply and cloud_setup.key_role(cfg["key"]) != "service_role":
        sys.exit("--apply needs the service_role key (Settings > Sync), not the anon key.")

    rows = sb._rest(cfg, "GET", "volumes?select=slug,pdf_url&order=slug")
    changes = plan(rows if isinstance(rows, list) else [], args.to, args.from_host)

    if not changes:
        print(f"Nothing to rewrite -- every published pdf_url is already on "
              f"{urlsplit(args.to).netloc} (or matched no filter).")
        return 0

    print(f"{'APPLY' if args.apply else 'DRY RUN'} -- {len(changes)} volume(s):\n")
    for slug, old, new in changes:
        print(f"  {slug}\n    - {old}\n    + {new}")

    if not args.apply:
        print("\nRe-run with --apply (and the service_role key) to write these.")
        return 0

    print()
    for slug, _old, new in changes:
        sb._rest(cfg, "PATCH", f"volumes?slug=eq.{quote(slug, safe='')}",
                 {"pdf_url": new}, prefer="return=minimal")
        print(f"  updated {slug}")
    print(f"\nDone -- {len(changes)} row(s) repointed to {urlsplit(args.to).netloc}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
