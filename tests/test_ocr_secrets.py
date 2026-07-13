"""OCR jobs use the server's local-only credential store, not renderer state."""

import server


def test_ocr_config_prefers_server_mistral_secret(monkeypatch):
    monkeypatch.setattr(server, "_client_settings", lambda: {
        "mistralKey": "cloud-synced-key",
    })

    cfg = server._ocr_request_cfg({"mistral_key": "stale-renderer-key"})

    assert cfg["mistral_key"] == "cloud-synced-key"


def test_ocr_config_keeps_request_fallback_when_server_cache_empty(monkeypatch):
    monkeypatch.setattr(server, "_client_settings", lambda: {})

    cfg = server._ocr_request_cfg({"mistral_key": "legacy-renderer-key"})

    assert cfg["mistral_key"] == "legacy-renderer-key"
