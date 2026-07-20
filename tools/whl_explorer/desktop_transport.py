"""Stdlib-only bootstrap for the authenticated Electron sidecar transport.

Import this module before Flask or any project module. It consumes the
child-only launch capability immediately so subsequently imported code and any
workers it might create cannot inherit the plaintext value.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import MutableMapping


CAPABILITY_HEADER = "X-WHL-Desktop-Capability"
_CAPABILITY_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True, slots=True)
class DesktopTransportConfig:
    mode: str
    capability_digest: bytes | None
    port: int | None

    @property
    def expected_host(self) -> str | None:
        return f"127.0.0.1:{self.port}" if self.port is not None else None

    @property
    def expected_origin(self) -> str | None:
        host = self.expected_host
        return f"http://{host}" if host else None


def load_desktop_transport_config(
        environ: MutableMapping[str, str], *, packaged: bool) -> DesktopTransportConfig:
    """Consume and validate the desktop launch environment, failing closed."""
    mode = (environ.pop("WHL_DESKTOP_MODE", "") or "").strip().lower()
    capability = environ.pop("WHL_DESKTOP_CAPABILITY", "") or ""
    if mode not in {"", "development", "packaged"}:
        raise RuntimeError("invalid WHL_DESKTOP_MODE")
    if packaged and mode != "packaged":
        raise RuntimeError("packaged sidecar requires desktop transport authentication")
    if not mode:
        if capability:
            raise RuntimeError("desktop capability supplied without desktop mode")
        return DesktopTransportConfig("", None, None)
    if not _CAPABILITY_RE.fullmatch(capability):
        raise RuntimeError("desktop transport capability is missing or malformed")
    raw_port = (environ.get("WHL_PORT") or "").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("desktop transport requires a valid WHL_PORT") from exc
    if port < 1024 or port > 65535 or str(port) != raw_port:
        raise RuntimeError("desktop transport requires a valid WHL_PORT")
    digest = hashlib.sha256(capability.encode("ascii")).digest()
    return DesktopTransportConfig(mode, digest, port)


def capability_matches(candidate: str | None, expected_digest: bytes | None) -> bool:
    """Compare fixed-size hashes so correct and incorrect tokens take one path."""
    if expected_digest is None or not isinstance(candidate, str):
        return False
    supplied = hashlib.sha256(candidate.encode("utf-8", "surrogatepass")).digest()
    return hmac.compare_digest(supplied, expected_digest)


def origin_matches(origin: str, expected: str) -> bool:
    """Accept one serialized HTTP origin, with no paths or normalization gaps."""
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return (parsed.scheme == "http" and not parsed.username and not parsed.password and
            not parsed.path and not parsed.query and not parsed.fragment and
            origin.lower() == expected.lower())


CONFIG = load_desktop_transport_config(
    os.environ, packaged=bool(getattr(sys, "frozen", False)))
