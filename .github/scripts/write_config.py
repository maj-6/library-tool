#!/usr/bin/env python3
"""Write the public website's Supabase configuration.

Production is deliberately fail closed: both repository variables must be
present, the URL must be an HTTPS origin, and the key must be either a modern
Supabase publishable key or an unexpired legacy JWT with the ``anon`` role.

Fixture data remains available in two intentionally separate forms:

* local development needs no generated config file; and
* ``--mode fixture-preview`` writes a conspicuous, credential-free marker for
  a downloadable preview artifact that the Pages workflow never deploys.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request
from urllib.parse import SplitResult, urlsplit

OUT = pathlib.Path(__file__).resolve().parents[2] / "website" / "assets" / "config.js"
MODES = ("development", "production", "fixture-preview")
PUBLISHABLE_KEY_RE = re.compile(r"sb_publishable_[A-Za-z0-9_-]+")
BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+")
PROBE_TIMEOUT_SECONDS = 15.0
PROBE_RESPONSE_LIMIT = 64 * 1024
PUBLIC_READ_PROBES = (
    ("volumes", "slug"),
    ("volume_texts", "slug"),
    ("volume_pages", "slug"),
    ("volume_notes", "slug"),
    ("author_pages", "author"),
    ("author_index", "author"),
    ("releases", "platform"),
)


class LiveProbeError(RuntimeError):
    """The configured public API is not ready to serve the website."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROBE_OPENER = urllib.request.build_opener(_RejectRedirects)


def _append_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def announce_mode(mode: str) -> None:
    descriptions = {
        "development": "local auto mode; fixtures are allowed",
        "production": "live Supabase configuration required",
        "fixture-preview": "fixture-only artifact; never a Pages deployment",
    }
    description = descriptions[mode]
    print(f"::notice title=Pages configuration::Selected mode: {mode} ({description}).")
    _append_summary(
        "## Pages configuration\n\n"
        f"- **Selected mode:** `{mode}` — {description}.\n"
    )


def notice(kind: str, msg: str) -> None:
    """Add an Actions annotation and a matching workflow-summary line."""
    print(f"::{kind}::{msg}")
    _append_summary(f"- **{kind}:** {msg}\n")


def die(msg: str) -> None:
    notice("error", msg)
    raise SystemExit(1)


def _jwt_part(name: str, encoded: str) -> dict:
    if not encoded or not BASE64URL_RE.fullmatch(encoded):
        die(f"SUPABASE_ANON_KEY has an invalid JWT {name}.")
    body = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(body, altchars=b"-_", validate=True)
        value = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        die(f"SUPABASE_ANON_KEY JWT {name} is not readable JSON: {exc}")
    if not isinstance(value, dict):
        die(f"SUPABASE_ANON_KEY JWT {name} is not a JSON object.")
    return value


def claims(jwt: str) -> dict:
    parts = jwt.split(".")
    if len(parts) != 3 or not all(parts):
        die("SUPABASE_ANON_KEY is not a JWT (expected three non-empty parts).")

    header = _jwt_part("header", parts[0])
    payload = _jwt_part("payload", parts[1])
    if not BASE64URL_RE.fullmatch(parts[2]):
        die("SUPABASE_ANON_KEY has an invalid JWT signature.")

    algorithm = header.get("alg")
    if (
        not isinstance(algorithm, str)
        or not algorithm.strip()
        or algorithm.casefold() == "none"
    ):
        die("SUPABASE_ANON_KEY JWT must name a signing algorithm.")
    return payload


def validate_url(url: str) -> SplitResult:
    if any(char.isspace() for char in url):
        die("SUPABASE_URL must not contain whitespace.")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or not parts.hostname:
        die(f"SUPABASE_URL must be an https:// origin; got {url!r}.")
    if parts.username is not None or parts.password is not None:
        die("SUPABASE_URL must not contain user credentials.")
    try:
        parts.port
    except ValueError as exc:
        die(f"SUPABASE_URL has an invalid port: {exc}")
    if parts.path.strip("/") or parts.query or parts.fragment:
        die(f"SUPABASE_URL must be a bare origin with no path; got {url!r}.")
    return parts


def validate_public_key(key: str) -> str:
    if any(char.isspace() for char in key):
        die("SUPABASE_ANON_KEY must not contain whitespace.")
    if key.startswith("sb_secret_"):
        die(
            "refusing to publish a Supabase secret key. This site is public: "
            "use a publishable key or a legacy JWT whose role is 'anon'."
        )
    if key.startswith("sb_publishable_"):
        if not PUBLISHABLE_KEY_RE.fullmatch(key):
            die("SUPABASE_ANON_KEY is not a valid Supabase publishable key.")
        return "publishable"

    payload = claims(key)
    role = payload.get("role")
    if role != "anon":
        die(
            f"refusing to publish a key whose role is {role!r}. This site is public: "
            "only the 'anon' role may be deployed. A 'service_role' key bypasses "
            "row-level security for anyone who views source."
        )

    expiration = payload.get("exp")
    if (
        isinstance(expiration, bool)
        or not isinstance(expiration, (int, float))
        or not expiration
    ):
        die("SUPABASE_ANON_KEY JWT must contain a numeric expiration.")
    if expiration <= time.time():
        die("SUPABASE_ANON_KEY has expired; refusing to deploy a broken library.")
    return "legacy anon JWT"


def _open_probe(request: urllib.request.Request, timeout: float):
    return _PROBE_OPENER.open(request, timeout=timeout)


def probe_live_configuration(
    url: str,
    key: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> None:
    """Prove that every public website resource is reachable with this key."""
    started = time.monotonic()
    for table, column in PUBLIC_READ_PROBES:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise LiveProbeError(
                f"timed out before checking public resource {table!r}."
            )

        endpoint = (
            f"{url.rstrip('/')}/rest/v1/{table}"
            f"?select={column}&limit=1"
        )
        request = urllib.request.Request(
            endpoint,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "apikey": key,
            },
            method="GET",
        )
        response = None
        try:
            response = _open_probe(request, min(5.0, remaining))
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if not 200 <= status < 300:
                raise LiveProbeError(
                    f"public resource {table!r} returned HTTP {status}."
                )
            final_url = response.geturl()
            if final_url != endpoint:
                raise LiveProbeError(
                    f"public resource {table!r} redirected away from its "
                    "configured endpoint."
                )
            body = response.read(PROBE_RESPONSE_LIMIT + 1)
            if len(body) > PROBE_RESPONSE_LIMIT:
                raise LiveProbeError(
                    f"public resource {table!r} returned an oversized response."
                )
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LiveProbeError(
                    f"public resource {table!r} did not return valid JSON."
                ) from exc
            if not isinstance(payload, list):
                raise LiveProbeError(
                    f"public resource {table!r} did not return a JSON array."
                )
        except urllib.error.HTTPError as exc:
            raise LiveProbeError(
                f"public resource {table!r} returned HTTP {exc.code}."
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise LiveProbeError(
                f"public resource {table!r} was unreachable: {reason}."
            ) from exc
        finally:
            if response is not None:
                response.close()


def _write(output: pathlib.Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def write_live_config(
    output: pathlib.Path,
    *,
    url: str,
    key: str,
    mode: str,
    probed: bool = False,
) -> None:
    validation = (
        "live endpoint and public reads probed"
        if probed
        else "configuration syntax validated; connectivity not probed"
    )
    _write(
        output,
        "// Written by .github/scripts/write_config.py. Not committed.\n"
        f"// Deployment mode: {mode}; {validation}.\n"
        "window.WHL_CONFIG = {\n"
        f"  deploymentMode: {json.dumps(mode)},\n"
        f"  supabaseUrl: {json.dumps(url)},\n"
        f"  supabaseAnonKey: {json.dumps(key)},\n"
        "};\n",
    )


def write_fixture_preview(output: pathlib.Path) -> None:
    _write(
        output,
        "// FIXTURE PREVIEW — NOT A PRODUCTION DEPLOYMENT.\n"
        "// This artifact contains no cloud credential and cannot read live data.\n"
        "window.WHL_CONFIG = {\n"
        '  deploymentMode: "fixture-preview",\n'
        "  fixturePreview: true,\n"
        "};\n",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="development",
        help="production fails closed; fixture-preview always omits cloud credentials",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=OUT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--probe-live",
        action="store_true",
        help="verify every public website resource before writing production config",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    announce_mode(args.mode)

    if args.probe_live and args.mode != "production":
        die("--probe-live is valid only in production mode.")
    if args.mode == "production":
        # A failed rerun must not leave a formerly valid deployment config behind.
        args.output.unlink(missing_ok=True)

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()

    if args.mode == "fixture-preview":
        write_fixture_preview(args.output)
        if url or key:
            notice(
                "warning",
                "cloud variables were ignored; fixture previews never contain credentials.",
            )
        notice(
            "warning",
            "fixture preview built for artifact review only; it must not be deployed.",
        )
        return

    if not url and not key:
        if args.mode == "production":
            die(
                "production Pages requires non-empty SUPABASE_URL and "
                "SUPABASE_ANON_KEY repository variables."
            )
        args.output.unlink(missing_ok=True)
        notice(
            "notice",
            "no cloud configuration selected; local development will read "
            "website/fixtures/.",
        )
        return

    if not url or not key:
        missing = "SUPABASE_URL" if not url else "SUPABASE_ANON_KEY"
        die(f"{missing} is empty while the other cloud variable is set.")

    parts = validate_url(url)
    key_kind = validate_public_key(key)
    if args.probe_live:
        try:
            probe_live_configuration(url, key)
        except LiveProbeError as exc:
            die(f"live Supabase probe failed: {exc}")
    write_live_config(
        args.output,
        url=url,
        key=key,
        mode=args.mode,
        probed=args.probe_live,
    )
    live_status = (
        " and all public website reads succeeded" if args.probe_live else ""
    )
    notice(
        "notice",
        f"config.js written for {parts.netloc} with a validated {key_kind}"
        f"{live_status}.",
    )


if __name__ == "__main__":
    main()
