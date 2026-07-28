from __future__ import annotations

import base64
from contextlib import contextmanager

import pytest

from librarytool.engine.correction_transforms import CorrectionTransformCancelled
from tools.whl_explorer import server


class Hooks:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled

    def is_cancelled(self):
        return self.cancelled

    def report_progress(self, _progress):
        return None


@contextmanager
def _execution_config(_service, config):
    yield dict(config)


def test_host_provider_pins_selection_and_normalizes_binary_artifacts(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_client_settings",
        lambda: {"ocrService": "mistral"},
    )
    monkeypatch.setattr(server, "_ocr_execution_cfg", _execution_config)
    monkeypatch.setattr(server, "_ocr_request_cfg", lambda _payload: {})
    calls = []

    def recognize(content, config):
        calls.append((content, config))
        return {
            "text": "Machine text",
            "regions": [{"type": "illustration"}],
            "images": [
                {
                    "id": "figure.jpeg",
                    "data": b"figure bytes",
                    "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
                }
            ],
        }

    monkeypatch.setitem(server._OCR_SERVICES, "mistral", recognize)
    provider = server._EngineCorrectionOcrProvider()

    selection = provider.select_provider()
    result = provider.recognize(selection, b"exact OCR-ready PNG", Hooks())

    assert selection.provider_id == "mistral"
    assert selection.model == "mistral-ocr-latest"
    assert calls == [(b"exact OCR-ready PNG", {})]
    assert result.provider_id == selection.provider_id
    assert result.model == selection.model
    payload = result.as_dict()["payload"]
    assert payload["text"] == "Machine text"
    assert payload["images"][0]["data"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"figure bytes").decode("ascii"),
    }


def test_host_provider_pins_claude_model_into_execution_config(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_client_settings",
        lambda: {
            "ocrService": "claude",
            "ocrClaudeModel": "claude-pinned",
        },
    )
    configs = []

    @contextmanager
    def execution_config(service, config):
        configs.append((service, dict(config)))
        yield dict(config)

    monkeypatch.setattr(server, "_ocr_execution_cfg", execution_config)
    monkeypatch.setattr(server, "_ocr_request_cfg", lambda _payload: {})
    monkeypatch.setitem(
        server._OCR_SERVICES,
        "claude",
        lambda _content, _config: "Transcription",
    )
    provider = server._EngineCorrectionOcrProvider()

    selection = provider.select_provider()
    result = provider.recognize(selection, b"image", Hooks())

    assert selection.model == "claude-pinned"
    assert configs == [
        ("claude", {"claude_model": "claude-pinned"})
    ]
    assert result.as_dict()["payload"] == {"text": "Transcription"}


def test_host_provider_defaults_unknown_selection_to_local_tesseract(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_client_settings",
        lambda: {"ocrService": "unknown"},
    )

    selection = server._EngineCorrectionOcrProvider().select_provider()

    assert selection.provider_id == "tesseract"
    assert selection.model == "local"


def test_host_provider_honors_cancellation_before_external_work(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setitem(
        server._OCR_SERVICES,
        "tesseract",
        lambda content, config: calls.append((content, config)),
    )
    provider = server._EngineCorrectionOcrProvider()

    with pytest.raises(CorrectionTransformCancelled):
        provider.recognize(
            provider.select_provider(),
            b"image",
            Hooks(cancelled=True),
        )

    assert calls == []
