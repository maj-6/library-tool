"""Environment-only configuration; server credentials never enter a client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from . import __version__


class ConfigurationError(RuntimeError):
    """Raised when required deployment configuration is absent or unsafe."""


def _positive_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise ConfigurationError(f"{name} must be between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_secret_key: str
    source_bucket: str = "captures"
    derivative_bucket: str = "capture-derivatives"
    admin_token: str = ""
    max_source_bytes: int = 32 * 1024 * 1024
    max_attempts: int = 4
    lease_seconds: int = 2100
    curvature_backend: str = "page-dewarp"
    processor_version: str = __version__

    def __post_init__(self) -> None:
        if self.supabase_secret_key.startswith("sb_publishable_"):
            raise ConfigurationError("SUPABASE_SECRET_KEY must be a backend secret key")
        if self.admin_token and len(self.admin_token) < 32:
            raise ConfigurationError(
                "IMAGE_PROCESSOR_ADMIN_TOKEN must be at least 32 characters when enabled"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        secret = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        if not url:
            raise ConfigurationError("SUPABASE_URL is required")
        parsed = urlparse(url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parsed.hostname or parsed.scheme not in ({"http", "https"} if local else {"https"}):
            raise ConfigurationError("SUPABASE_URL must be HTTPS (HTTP is allowed only locally)")
        if not secret:
            raise ConfigurationError("SUPABASE_SECRET_KEY is required")

        source_bucket = os.environ.get("SOURCE_BUCKET", "captures").strip()
        derivative_bucket = os.environ.get(
            "DERIVATIVE_BUCKET", "capture-derivatives"
        ).strip()
        if not source_bucket or not derivative_bucket:
            raise ConfigurationError("Storage bucket names cannot be empty")
        backend = os.environ.get("CURVATURE_BACKEND", "page-dewarp").strip().lower()
        if backend not in {"page-dewarp", "off"}:
            raise ConfigurationError("CURVATURE_BACKEND must be page-dewarp or off")

        admin_token = os.environ.get("IMAGE_PROCESSOR_ADMIN_TOKEN", "").strip()
        return cls(
            supabase_url=url,
            supabase_secret_key=secret,
            source_bucket=source_bucket,
            derivative_bucket=derivative_bucket,
            admin_token=admin_token,
            max_source_bytes=_positive_int(
                "MAX_SOURCE_BYTES", 32 * 1024 * 1024, maximum=128 * 1024 * 1024
            ),
            max_attempts=_positive_int("MAX_ATTEMPTS", 4, maximum=20),
            lease_seconds=_positive_int("LEASE_SECONDS", 2100, maximum=86_400),
            curvature_backend=backend,
            processor_version=os.environ.get("PROCESSOR_VERSION", __version__).strip()
            or __version__,
        )
