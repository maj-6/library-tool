"""OCR jobs never retain or accept renderer credential material."""

from contextlib import contextmanager

import server


def test_ocr_job_config_ignores_renderer_credentials(monkeypatch):
    monkeypatch.setattr(server, "_client_settings", lambda: {
        "ocrClaudeModel": "claude-model",
        "ocrAwsRegion": "us-west-2",
    })

    cfg = server._ocr_request_cfg({
        "mistral_key": "renderer-mistral",
        "claude_key": "renderer-claude",
        "aws_key": "renderer-aws",
        "aws_secret": "renderer-secret",
    })

    assert cfg == {
        "tesseract": None,
        "claude_model": "claude-model",
        "aws_region": "us-west-2",
    }
    assert not any(key.endswith("_key") or key.endswith("_secret")
                   for key in cfg)


def test_ocr_execution_leases_only_provider_credentials(monkeypatch):
    entered = []
    exited = []

    @contextmanager
    def lease(key):
        entered.append(key)
        try:
            yield "leased-" + key
        finally:
            exited.append(key)

    monkeypatch.setattr(server, "_lease_secret", lease)
    base = {"claude_model": "model", "aws_region": "us-east-1"}

    with server._ocr_execution_cfg("textract", base) as cfg:
        assert cfg["aws_key"] == "leased-ocrAwsKey"
        assert cfg["aws_secret"] == "leased-ocrAwsSecret"
        assert base == {"claude_model": "model", "aws_region": "us-east-1"}

    assert entered == ["ocrAwsKey", "ocrAwsSecret"]
    assert exited == ["ocrAwsSecret", "ocrAwsKey"]
    assert "aws_key" not in cfg and "aws_secret" not in cfg
