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
    assert calls == [(
        b"exact OCR-ready PNG",
        {"mistral_model": "mistral-ocr-latest"},
    )]
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
        (
            "claude",
            {"claude_model": "claude-pinned", "max_tokens": 8192},
        )
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


def test_host_provider_executes_only_pinned_nonsecret_options(
    monkeypatch,
) -> None:
    settings = {
        "ocrService": "textract",
        "ocrAwsRegion": "us-west-2",
    }
    monkeypatch.setattr(server, "_client_settings", lambda: dict(settings))
    configs = []

    @contextmanager
    def execution_config(service, config):
        configs.append((service, dict(config)))
        yield dict(config)

    monkeypatch.setattr(server, "_ocr_execution_cfg", execution_config)
    monkeypatch.setitem(
        server._OCR_SERVICES,
        "textract",
        lambda _content, _config: {"text": "Pinned"},
    )
    provider = server._EngineCorrectionOcrProvider()
    selection = provider.select_provider()
    settings["ocrAwsRegion"] = "eu-central-1"

    provider.recognize(selection, b"image", Hooks())

    assert selection.options == {"aws_region": "us-west-2"}
    assert configs == [
        ("textract", {"aws_region": "us-west-2"}),
    ]


def test_host_provider_normalizes_whitespace_settings(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_client_settings",
        lambda: {
            "ocrService": "claude",
            "ocrClaudeModel": "   ",
        },
    )
    claude = server._EngineCorrectionOcrProvider().select_provider()
    assert claude.model == "claude-haiku-4-5-20251001"
    assert claude.options["claude_model"] == claude.model

    monkeypatch.setattr(
        server,
        "_client_settings",
        lambda: {
            "ocrService": "tesseract",
            "ocrTesseract": "   ",
        },
    )
    monkeypatch.setattr(
        server.shutil,
        "which",
        lambda _name: "C:\\pinned\\tesseract.exe",
    )
    monkeypatch.setattr(
        server,
        "_TESSERACT_DEFAULT",
        "C:\\missing\\default-tesseract.exe",
    )
    tesseract = server._EngineCorrectionOcrProvider().select_provider()
    assert tesseract.options["tesseract"] == (
        "C:\\pinned\\tesseract.exe"
    )


def test_host_provider_pins_mistral_model_across_release_drift(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_client_settings",
        lambda: {"ocrService": "mistral"},
    )
    configs = []

    @contextmanager
    def execution_config(service, config):
        configs.append((service, dict(config)))
        yield dict(config)

    monkeypatch.setattr(server, "_ocr_execution_cfg", execution_config)
    monkeypatch.setitem(
        server._OCR_SERVICES,
        "mistral",
        lambda _content, _config: {"text": "Pinned"},
    )
    monkeypatch.setattr(server.capture, "OCR_MODEL", "mistral-pinned")
    provider = server._EngineCorrectionOcrProvider()
    selection = provider.select_provider()
    monkeypatch.setattr(server.capture, "OCR_MODEL", "mistral-new-default")

    provider.recognize(selection, b"image", Hooks())

    assert selection.model == "mistral-pinned"
    assert configs == [
        ("mistral", {"mistral_model": "mistral-pinned"}),
    ]


def test_tesseract_runner_rejects_an_unavailable_pinned_executable(
    monkeypatch,
) -> None:
    import pytesseract

    sentinel = "previous-global-tesseract"
    monkeypatch.setattr(
        pytesseract.pytesseract,
        "tesseract_cmd",
        sentinel,
    )

    with pytest.raises(RuntimeError, match="Pinned Tesseract"):
        server._ocr_tesseract(
            b"not-decoded",
            {"tesseract": "Z:\\missing\\tesseract.exe"},
        )

    assert pytesseract.pytesseract.tesseract_cmd == sentinel


def test_production_engine_bindings_install_the_correction_ocr_provider() -> None:
    bindings = server._engine_host_bindings()

    assert isinstance(
        bindings.corrections.ocr_provider,
        server._EngineCorrectionOcrProvider,
    )


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
