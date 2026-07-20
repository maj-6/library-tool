#!/usr/bin/env python3
"""Validate or publish Android's remotely refreshable in-app UI catalog.

Examples:

    python tools/android_ui_catalog.py check
    python tools/android_ui_catalog.py push
    python tools/android_ui_catalog.py push path/to/catalog.json

`push` uses the same signed-in Supabase user session as the desktop app. It
never reads the service-role credential. The database admits only users listed
in `android_ui_publishers`; the shipped project enrols its approved maintainer.

The source JSON stores icon file paths, not base64 blobs. Publishing reads each
PNG, adds a SHA-256 digest, and sends the bounded wire format the Android app
validates again before caching. Android's installed launcher icon is not part of
this catalog because the OS only changes it when a newly signed APK is installed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_defaults  # noqa: E402
import libcommon as lib  # noqa: E402
import supabase_auth as sauth  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = ROOT / "android" / "BookCapture" / "remote-ui" / "catalog.json"
SCHEMA = 1
MAX_RESPONSE_BYTES = 768 * 1024
MAX_ICON_BYTES = 128 * 1024
MAX_STRINGS = 500
MAX_ICONS = 100
KEY_RE = re.compile(r"[a-z][A-Za-z0-9_]{0,95}\Z")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class CatalogError(Exception):
    pass


def _object(value, label: str) -> dict:
    if not isinstance(value, dict):
        raise CatalogError(f"{label} must be a JSON object")
    return value


def _key(value, label: str) -> str:
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise CatalogError(f"invalid {label} key: {value!r}")
    return value


def build_wire_catalog(source_path: Path) -> tuple[int, dict]:
    """Return `(revision, wire catalog)` after validating every local asset."""
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"could not read {source_path}: {exc}") from exc
    source = _object(source, "catalog")
    if source.get("schema") != SCHEMA:
        raise CatalogError(f"catalog schema must be {SCHEMA}")
    revision = source.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise CatalogError("catalog revision must be a positive integer")

    raw_strings = _object(source.get("strings", {}), "strings")
    if len(raw_strings) > MAX_STRINGS:
        raise CatalogError(f"catalog has more than {MAX_STRINGS} strings")
    strings: dict[str, str] = {}
    for raw_name, value in raw_strings.items():
        name = _key(raw_name, "string")
        if not isinstance(value, str):
            raise CatalogError(f"string {name!r} must contain text")
        if len(value) > 4096:
            raise CatalogError(f"string {name!r} exceeds 4096 characters")
        strings[name] = value

    raw_icons = _object(source.get("icons", {}), "icons")
    if len(raw_icons) > MAX_ICONS:
        raise CatalogError(f"catalog has more than {MAX_ICONS} icons")
    icons: dict[str, dict] = {}
    for raw_name, definition in raw_icons.items():
        name = _key(raw_name, "icon")
        if isinstance(definition, str):
            icon_path = definition
            mime = "image/png"
        else:
            definition = _object(definition, f"icon {name!r}")
            icon_path = definition.get("path")
            mime = str(definition.get("mime") or "image/png").lower()
        if mime != "image/png":
            raise CatalogError(f"icon {name!r} must use image/png")
        if not isinstance(icon_path, str) or not icon_path.strip():
            raise CatalogError(f"icon {name!r} needs a path")
        path = Path(icon_path)
        if not path.is_absolute():
            path = source_path.parent / path
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CatalogError(f"could not read icon {name!r}: {exc}") from exc
        if not data.startswith(PNG_MAGIC):
            raise CatalogError(f"icon {name!r} is not a PNG")
        if not 0 < len(data) <= MAX_ICON_BYTES:
            raise CatalogError(
                f"icon {name!r} must be between 1 and {MAX_ICON_BYTES} bytes",
            )
        icons[name] = {
            "mime": mime,
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        }

    catalog = {"schema": SCHEMA, "strings": strings, "icons": icons}
    # Include the PostgREST row envelope in the same budget Android enforces.
    envelope = json.dumps(
        [{"revision": revision, "catalog": catalog}],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(envelope) > MAX_RESPONSE_BYTES:
        raise CatalogError(
            f"published catalog would be {len(envelope)} bytes; limit is "
            f"{MAX_RESPONSE_BYTES}",
        )
    return revision, catalog


def _data_root(override: str = "") -> Path:
    return Path(override).expanduser().resolve() if override else lib.DATA_ROOT


def public_config(data_root: Path) -> dict:
    url = (os.environ.get("WHL_SUPABASE_URL")
           or os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("WHL_SUPABASE_ANON_KEY")
           or os.environ.get("SUPABASE_ANON_KEY") or "").strip()
    state = lib.load_json(data_root / "output" / "client_state.json", {}) or {}
    settings = state.get("settings") if isinstance(state, dict) else {}
    settings = settings if isinstance(settings, dict) else {}
    secrets = lib.load_json(data_root / "output" / "secrets.json", {}) or {}
    secrets = secrets if isinstance(secrets, dict) else {}
    url = url or str(settings.get("supabaseUrl") or "").strip()
    key = key or str(secrets.get("supabaseAnonKey") or "").strip()
    url = url or cloud_defaults.SUPABASE_URL
    if not key and url.rstrip("/") == cloud_defaults.SUPABASE_URL.rstrip("/"):
        key = cloud_defaults.SUPABASE_ANON_KEY
    if not url or not key:
        raise CatalogError(
            "Supabase public URL/key are not configured for this desktop profile",
        )
    return {"url": url.rstrip("/"), "key": key}


def live_session(cfg: dict, data_root: Path, now: float | None = None) -> dict:
    """Load and, when necessary, rotate the desktop user's auth session."""
    path = data_root / "output" / "auth_session.json"
    doc = lib.load_json(path, {}) or {}
    session = doc.get("session") if isinstance(doc, dict) else None
    if not isinstance(session, dict) or not session.get("refresh_token"):
        raise CatalogError(
            "No desktop account session. Sign in to Library Tool first "
            "(or point WHL_DATA_ROOT at its data directory).",
        )
    now = time.time() if now is None else now
    if now >= float(session.get("expires_at") or 0) - 90:
        try:
            fresh = sauth.refresh(cfg, str(session["refresh_token"]))
        except sauth.AuthError as exc:
            raise CatalogError(f"could not refresh the desktop session: {exc}") from exc
        fresh["display_name"] = session.get("display_name") or fresh.get("display_name") or ""
        doc["session"] = fresh
        lib.save_json(path, doc)
        session = fresh
    if not session.get("access_token") or not session.get("user_id"):
        raise CatalogError("the desktop account session is incomplete")
    return session


def publish(
    cfg: dict,
    session: dict,
    revision: int,
    catalog: dict,
    *,
    force: bool = False,
) -> dict:
    token = str(session["access_token"])
    rows = sauth.rest(
        cfg,
        token,
        "GET",
        "android_ui_catalog?id=eq.current&select=revision&limit=1",
    ) or []
    current = int(rows[0].get("revision") or 0) if rows else 0
    if revision <= current and not force:
        raise CatalogError(
            f"catalog revision {revision} is not newer than cloud revision "
            f"{current}; increment it (or pass --force intentionally)",
        )
    payload = {
        "revision": revision,
        "catalog": catalog,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": str(session["user_id"]),
    }
    if rows:
        written = sauth.rest(
            cfg,
            token,
            "PATCH",
            "android_ui_catalog?id=eq.current",
            payload,
            prefer="return=representation",
        ) or []
    else:
        written = sauth.rest(
            cfg,
            token,
            "POST",
            "android_ui_catalog",
            [{"id": "current", **payload}],
            prefer="return=representation",
        ) or []
    if not written:
        raise CatalogError(
            "the catalog was not written; confirm this account is enrolled in "
            "android_ui_publishers",
        )
    return written[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("check", "push"))
    parser.add_argument("catalog", nargs="?", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--data-root", default="",
                        help="desktop data directory containing output/auth_session.json")
    parser.add_argument("--force", action="store_true",
                        help="allow replacing the same/newer cloud revision")
    args = parser.parse_args(argv)
    try:
        revision, catalog = build_wire_catalog(args.catalog.resolve())
        wire_bytes = len(json.dumps(catalog, ensure_ascii=False).encode("utf-8"))
        if args.action == "check":
            print(
                f"catalog revision {revision}: {len(catalog['strings'])} strings, "
                f"{len(catalog['icons'])} icons, {wire_bytes} wire bytes",
            )
            return 0
        root = _data_root(args.data_root)
        cfg = public_config(root)
        session = live_session(cfg, root)
        row = publish(cfg, session, revision, catalog, force=args.force)
        print(f"published Android UI catalog revision {row.get('revision', revision)}")
        return 0
    except CatalogError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
