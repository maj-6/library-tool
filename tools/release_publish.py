#!/usr/bin/env python3
"""Put one app build on the website's Downloads page.

    python3 tools/release_publish.py BookCapture-0.2.0.apk --platform android --version 0.2.0
    python3 tools/release_publish.py --url https://github.com/.../LibraryTool-Setup-0.4.0.exe \
            --platform windows --version 0.4.0 [--sha256 <hex> --bytes <n>]

The Downloads page renders the `releases` table newest-first, one card per
platform, so inserting a row IS publishing — the static site never redeploys
for a release.

Where the file itself lives is a separate question, hence two modes:

* a local file is uploaded to the public `releases` storage bucket (created on
  first use) and registered under its public URL. Fine for the APK; Supabase's
  free tier caps one object around 50 MB, which the desktop installer exceeds.
* --url registers a file hosted elsewhere — the release workflow passes GitHub
  Release asset URLs. With a local copy of the file also present, sha256 and
  size are computed; otherwise pass --sha256/--bytes or the row goes up without
  them (the page shows blanks, nothing breaks).

Credentials: set SUPABASE_URL / SUPABASE_KEY in the environment. Writing
`releases` needs the service_role key — RLS leaves anon read-only — and the
role is read out of the JWT up front so a wrong key fails with a sentence
instead of a PostgREST 401.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cli_credentials          # noqa: E402
import supabase_sync as sb       # noqa: E402

BUCKET = "releases"
PLATFORMS = ["windows", "macos", "linux", "android"]   # the table's check constraint
CONTENT_TYPES = {
    ".apk": "application/vnd.android.package-archive",
    ".msi": "application/x-msi",
    ".exe": "application/x-msdownload",
    ".dmg": "application/x-apple-diskimage",
}


def config() -> dict:
    return cli_credentials.supabase_service_config()


def key_role(key: str) -> str:
    """service_role or anon, read straight out of the JWT (no verification)."""
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role", "?")
    except Exception:
        return "?"


def ensure_bucket(cfg: dict) -> None:
    """The `releases` bucket is public: its objects ARE the download links."""
    url, _, headers = sb._cfg(cfg)
    raw = sb._request("GET", f"{url}/storage/v1/bucket", headers)
    have = {b["name"]: bool(b.get("public")) for b in json.loads(raw.decode())}
    if BUCKET not in have:
        headers = dict(headers, **{"Content-Type": "application/json"})
        sb._request("POST", f"{url}/storage/v1/bucket", headers,
                    json.dumps({"id": BUCKET, "name": BUCKET, "public": True}).encode())
        print(f"created public bucket `{BUCKET}`")
    elif not have[BUCKET]:
        sys.exit(f"bucket `{BUCKET}` exists but is private; its objects must be "
                 "publicly downloadable. Fix it in the Supabase dashboard.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("artifact", nargs="?", help="file to upload and/or hash")
    ap.add_argument("--platform", required=True, choices=PLATFORMS)
    ap.add_argument("--version", required=True)
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--notes", default="")
    ap.add_argument("--url", default="",
                    help="register this URL instead of uploading to the bucket")
    ap.add_argument("--sha256", default="", help="with --url and no local file")
    ap.add_argument("--bytes", type=int, default=0, help="with --url and no local file")
    args = ap.parse_args()

    if not args.artifact and not args.url:
        ap.error("nothing to publish: give a file, a --url, or both")

    sha256, size = args.sha256, args.bytes
    data = b""
    if args.artifact:
        path = Path(args.artifact)
        if not path.is_file():
            sys.exit(f"not a file: {path}")
        data = path.read_bytes()
        sha256, size = hashlib.sha256(data).hexdigest(), len(data)

    cfg = config()
    role = key_role(cfg["key"])
    if role != "service_role":
        sys.exit(f"the key's role is {role!r}; writing `releases` needs the "
                 "service_role key (anon is read-only by RLS design).")

    url = args.url
    if not url:
        ensure_bucket(cfg)
        name = Path(args.artifact).name
        ctype = CONTENT_TYPES.get(Path(name).suffix.lower()) \
            or mimetypes.guess_type(name)[0] or "application/octet-stream"
        object_path = f"{args.platform}/{args.version}/{name}"
        sb.upload_object(cfg, BUCKET, object_path, data, content_type=ctype)
        url = sb.public_url(cfg, BUCKET, object_path)
        print(f"uploaded {name} ({size / 1048576:.1f} MB) -> {url}")

    row = {
        "platform": args.platform,
        "version": args.version,
        "channel": args.channel,
        "url": url,
        "sha256": sha256,
        "notes": args.notes,
    }
    if size:
        row["bytes"] = size
    # Re-publishing the same platform/version/channel replaces the row, so a
    # re-run workflow or a corrected artifact never 409s.
    sb._rest(cfg, "POST", "releases?on_conflict=platform,version,channel", [row],
             prefer="resolution=merge-duplicates,return=minimal")
    print(f"registered {args.platform} {args.version} ({args.channel}) on the "
          "Downloads page")


if __name__ == "__main__":
    main()
