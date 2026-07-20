from __future__ import annotations

import pytest

from whl_image_processor.settings import ConfigurationError, Settings


def test_settings_require_https_and_backend_secret(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_env()

    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    settings = Settings.from_env()
    assert settings.derivative_bucket == "capture-derivatives"


def test_local_http_is_allowed_for_self_hosted_development(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:54321")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "development-secret")
    assert Settings.from_env().supabase_url == "http://127.0.0.1:54321"


def test_optional_admin_token_requires_adequate_entropy_length(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test")
    monkeypatch.setenv("IMAGE_PROCESSOR_ADMIN_TOKEN", "too-short")

    with pytest.raises(ConfigurationError, match="at least 32"):
        Settings.from_env()


def test_publishable_key_is_rejected_for_backend_worker(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_publishable_not-a-backend-key")

    with pytest.raises(ConfigurationError, match="backend secret"):
        Settings.from_env()
