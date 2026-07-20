"""Explicit environment-only credentials for standalone maintenance tools.

The desktop owns credentials through the protected engine secret repository.
Standalone scripts do not import that boundary or inspect UI state: operators
provide the credentials needed for one process through documented environment
variables instead.
"""
from __future__ import annotations

import os


R2_ENV = {
    "account": "R2_ACCOUNT_ID",
    "bucket": "R2_BUCKET",
    "key_id": "R2_ACCESS_KEY_ID",
    "secret": "R2_SECRET_ACCESS_KEY",
    "public_base": "R2_PUBLIC_BASE_URL",
}
_R2_REQUIRED = ("account", "bucket", "key_id", "secret")


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def supabase_service_config(*, default_url: str = "") -> dict[str, str]:
    """Return the explicit service configuration or fail without secret data."""
    url = _env("SUPABASE_URL") or str(default_url or "").strip()
    key = _env("SUPABASE_KEY")
    missing = []
    if not url:
        missing.append("SUPABASE_URL")
    if not key:
        missing.append("SUPABASE_KEY")
    if missing:
        raise SystemExit(
            "Missing Supabase configuration. Set "
            + " and ".join(missing)
            + " in the environment."
        )
    return {"url": url.rstrip("/"), "key": key}


def r2_config(
    *, required: bool = True, require_public_base: bool = False
) -> dict[str, str]:
    """Return one complete R2 environment configuration or no configuration."""
    cfg = {field: _env(variable) for field, variable in R2_ENV.items()}
    missing = [R2_ENV[field] for field in _R2_REQUIRED if not cfg[field]]
    if require_public_base and not cfg["public_base"]:
        missing.append(R2_ENV["public_base"])
    if required and missing:
        raise SystemExit(
            "Missing R2 configuration. Set "
            + ", ".join(missing)
            + " in the environment."
        )
    if missing:
        return {field: "" for field in R2_ENV}
    return cfg


def mistral_api_key(*, required: bool = True) -> str:
    """Return the Mistral key without accepting process-list-visible argv."""
    key = _env("MISTRAL_API_KEY")
    if required and not key:
        raise SystemExit(
            "Missing Mistral API credential. Set MISTRAL_API_KEY in the "
            "environment."
        )
    return key
