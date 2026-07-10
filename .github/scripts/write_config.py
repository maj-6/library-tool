#!/usr/bin/env python3
"""Write website/assets/config.js from the repository variables, or don't.

The file is gitignored, so the deployed copy is the only one that ever exists
and CI is where it gets written. Two rules earn this script its keep.

1. Missing variables are not a failure. The site falls back to
   fixtures/volumes.json and says so on the page, which beats a red X and no
   site at all.

2. A service_role key here would hand every visitor a superuser credential: it
   bypasses row-level security entirely. The anon key is public by design; the
   service_role key is a skeleton key that happens to look exactly like it --
   both are JWTs, both are three dot-separated parts, and they differ in one
   claim. So read the claim, and refuse to publish anything that is not `anon`.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import time
from urllib.parse import urlsplit

OUT = pathlib.Path(__file__).resolve().parents[2] / "website" / "assets" / "config.js"


def notice(kind: str, msg: str) -> None:
    """An annotation on the run, and a line in its summary."""
    print(f"::{kind}::{msg}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"- **{kind}:** {msg}\n")


def die(msg: str) -> None:
    notice("error", msg)
    raise SystemExit(1)


def claims(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3:
        die("SUPABASE_ANON_KEY is not a JWT (expected three dot-separated parts).")
    body = parts[1] + "=" * (-len(parts[1]) % 4)  # base64url, padding stripped
    try:
        payload = json.loads(base64.urlsafe_b64decode(body))
    except Exception as exc:
        die(f"SUPABASE_ANON_KEY payload is not readable JSON: {exc}")
    if not isinstance(payload, dict):
        die("SUPABASE_ANON_KEY payload is not a JSON object.")
    return payload


def main() -> None:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()

    if not url and not key:
        notice(
            "warning",
            "SUPABASE_URL / SUPABASE_ANON_KEY are not set, so the site will serve "
            "fixtures/volumes.json rather than the live library. Set them under "
            "Settings -> Secrets and variables -> Actions -> Variables.",
        )
        return

    # Half-configured is always a mistake -- most likely one variable is misspelled.
    # Falling back to the fixture here would hide it behind a site that merely looks
    # a bit empty.
    if not url or not key:
        missing = "SUPABASE_URL" if not url else "SUPABASE_ANON_KEY"
        die(f"{missing} is empty while the other is set. Set both, or neither.")

    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        die(f"SUPABASE_URL must be an https:// origin; got {url!r}.")
    if parts.path.strip("/") or parts.query or parts.fragment:
        die(f"SUPABASE_URL must be a bare origin with no path; got {url!r}.")

    payload = claims(key)
    role = payload.get("role")
    if role != "anon":
        die(
            f"refusing to publish a key whose role is {role!r}. This site is public: "
            "only the 'anon' key may be deployed. A 'service_role' key bypasses "
            "row-level security for anyone who views source."
        )

    exp = payload.get("exp")
    if isinstance(exp, (int, float)) and exp < time.time():
        notice("warning", "the anon key has expired; the library will fail to load.")

    # json.dumps emits a valid JS string literal -- quotes, backslashes and every
    # non-ASCII character escaped -- so neither value can terminate the string
    # early. config.js is an external script, so `</script>` is not a concern.
    OUT.write_text(
        "// Written by .github/workflows/pages.yml at deploy time. Not committed.\n"
        "window.WHL_CONFIG = {\n"
        f"  supabaseUrl: {json.dumps(url)},\n"
        f"  supabaseAnonKey: {json.dumps(key)},\n"
        "};\n",
        encoding="utf-8",
    )
    notice("notice", f"config.js written; the library reads from {parts.netloc}.")


if __name__ == "__main__":
    main()
