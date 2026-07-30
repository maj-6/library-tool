from __future__ import annotations

import base64
from contextlib import contextmanager
import json

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
    assert bindings.text_layer_aggregate is not None
    assert bindings.text_layer_aggregate.item_exists_for is (
        server._translation_item_exists
    )
    assert bindings.text_layer_aggregate.source_snapshot_for is (
        server._engine_text_layer_source_snapshot
    )


def test_production_text_layer_source_uses_live_representation_revision(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        server,
        "_translation_item_exists",
        lambda item_id: item_id == "book-1",
    )
    monkeypatch.setattr(
        server,
        "_engine_representation_revision",
        lambda item_id, representation_id: (
            "scan-r7"
            if (item_id, representation_id) == ("book-1", "scan")
            else None
        ),
    )

    snapshot = server._engine_text_layer_source_snapshot(
        "book-1",
        "scan",
    )

    assert snapshot.item_id == "book-1"
    assert snapshot.representation_id == "scan"
    assert snapshot.revision == "scan-r7"
    assert server._engine_text_layer_source_snapshot(
        "book-1",
        "missing",
    ) is None


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


def test_host_normalizer_tracks_the_exact_bounded_json_shape() -> None:
    budget = server._CorrectionOcrJsonBudget(
        max_nodes=20,
        max_string_bytes=1024,
        max_encoded_bytes=1024,
    )

    normalized = server._correction_ocr_json_value(
        {"text": "café\n", "image": b"figure"},
        budget=budget,
    )

    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert budget.encoded_bytes == len(encoded)
    assert budget.nodes == 5
    assert normalized["image"] == {
        "encoding": "base64",
        "data": base64.b64encode(b"figure").decode("ascii"),
    }


def test_host_normalizer_rejects_binary_before_base64_copy(
    monkeypatch,
) -> None:
    calls = []

    def unexpected_encode(_value):
        calls.append(True)
        raise AssertionError("base64 encoding must not start")

    monkeypatch.setattr(server.base64, "b64encode", unexpected_encode)
    budget = server._CorrectionOcrJsonBudget(
        max_nodes=20,
        max_string_bytes=1024,
        max_encoded_bytes=64,
    )

    with pytest.raises(ValueError, match="encoded size budget"):
        server._correction_ocr_json_value(
            {"small": b"x", "large": b"too large"},
            budget=budget,
        )

    assert calls == []


def test_host_normalizer_preflights_node_and_string_budgets() -> None:
    node_budget = server._CorrectionOcrJsonBudget(
        max_nodes=2,
        max_string_bytes=1024,
        max_encoded_bytes=1024,
    )
    with pytest.raises(ValueError, match="node budget"):
        server._correction_ocr_json_value([1, 2], budget=node_budget)

    string_budget = server._CorrectionOcrJsonBudget(
        max_nodes=20,
        max_string_bytes=3,
        max_encoded_bytes=1024,
    )
    with pytest.raises(ValueError, match="string budget"):
        server._correction_ocr_json_value("four", budget=string_budget)


def _mistral_page(images) -> dict:
    return {
        "markdown": "page",
        "dimensions": {"width": 100, "height": 200, "dpi": 200},
        "blocks": [],
        "images": images,
    }


def _mistral_image(encoded: object, *, image_id: str = "figure.jpeg") -> dict:
    return {
        "id": image_id,
        "image_base64": encoded,
        "top_left_x": 10,
        "top_left_y": 20,
        "bottom_right_x": 60,
        "bottom_right_y": 80,
    }


def test_mistral_runner_strictly_decodes_raw_and_data_url_images(
    monkeypatch,
) -> None:
    first = b"first figure"
    second = b"second figure"
    monkeypatch.setattr(
        server.capture,
        "mistral_ocr_pages",
        lambda *_args, **_kwargs: [
            _mistral_page([
                _mistral_image(base64.b64encode(first).decode("ascii")),
                _mistral_image(
                    "data:image/png;base64,"
                    + base64.b64encode(second).decode("ascii"),
                    image_id="second.png",
                ),
            ])
        ],
    )

    result = server._ocr_mistral(
        b"source",
        {"mistral_key": "configured"},
    )

    assert [image["data"] for image in result["images"]] == [first, second]
    assert result["images"][0]["bbox"] == {
        "x": 0.1,
        "y": 0.1,
        "w": 0.5,
        "h": 0.3,
    }


def test_mistral_runner_rejects_invalid_base64_without_leaking_values(
    monkeypatch,
) -> None:
    secret = "!!!!"
    monkeypatch.setattr(
        server.capture,
        "mistral_ocr_pages",
        lambda *_args, **_kwargs: [
            _mistral_page([_mistral_image(secret)])
        ],
    )

    with pytest.raises(ValueError) as raised:
        server._ocr_mistral(
            b"source",
            {"mistral_key": "credential-must-stay-private"},
        )

    message = str(raised.value)
    assert "invalid image data" in message
    assert secret not in message
    assert "credential-must-stay-private" not in message


def test_mistral_runner_rejects_per_image_limit_before_decode(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        server,
        "_CORRECTION_OCR_MISTRAL_MAX_IMAGE_BYTES",
        3,
    )
    monkeypatch.setattr(
        server.capture,
        "mistral_ocr_pages",
        lambda *_args, **_kwargs: [
            _mistral_page([
                _mistral_image(base64.b64encode(b"four").decode("ascii"))
            ])
        ],
    )
    monkeypatch.setattr(
        server.base64,
        "b64decode",
        lambda *_args, **_kwargs: calls.append(True),
    )

    with pytest.raises(ValueError, match="image exceeds"):
        server._ocr_mistral(b"source", {"mistral_key": "configured"})

    assert calls == []


def test_mistral_runner_rejects_aggregate_limit_before_any_decode(
    monkeypatch,
) -> None:
    calls = []
    encoded = base64.b64encode(b"abc").decode("ascii")
    monkeypatch.setattr(
        server,
        "_CORRECTION_OCR_MISTRAL_MAX_TOTAL_IMAGE_BYTES",
        5,
    )
    monkeypatch.setattr(
        server.capture,
        "mistral_ocr_pages",
        lambda *_args, **_kwargs: [
            _mistral_page([
                _mistral_image(encoded),
                _mistral_image(encoded, image_id="second.jpeg"),
            ])
        ],
    )
    monkeypatch.setattr(
        server.base64,
        "b64decode",
        lambda *_args, **_kwargs: calls.append(True),
    )

    with pytest.raises(ValueError, match="aggregate size limit"):
        server._ocr_mistral(b"source", {"mistral_key": "configured"})

    assert calls == []


def test_mistral_runner_rejects_image_count_before_any_decode(
    monkeypatch,
) -> None:
    calls = []
    encoded = base64.b64encode(b"a").decode("ascii")
    monkeypatch.setattr(server, "_CORRECTION_OCR_MISTRAL_MAX_IMAGES", 1)
    monkeypatch.setattr(
        server.capture,
        "mistral_ocr_pages",
        lambda *_args, **_kwargs: [
            _mistral_page([
                _mistral_image(encoded),
                _mistral_image(encoded, image_id="second.jpeg"),
            ])
        ],
    )
    monkeypatch.setattr(
        server.base64,
        "b64decode",
        lambda *_args, **_kwargs: calls.append(True),
    )

    with pytest.raises(ValueError, match="too many images"):
        server._ocr_mistral(b"source", {"mistral_key": "configured"})

    assert calls == []


class _HttpResponse:
    def __init__(self, payload: bytes, *, content_length=None) -> None:
        self.payload = payload
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.payload[:size] if size >= 0 else self.payload


def test_mistral_http_response_read_is_bounded(monkeypatch) -> None:
    response = _HttpResponse(b'{"pages":[]}')
    monkeypatch.setattr(
        server.capture.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )

    result = server.capture._mistral_post(
        "https://provider.invalid/ocr",
        {"document": {}},
        "private-key",
        1,
        maximum_response_bytes=32,
    )

    assert result == {"pages": []}
    assert response.read_sizes == [33]


def test_mistral_http_rejects_declared_oversize_without_reading(
    monkeypatch,
) -> None:
    response = _HttpResponse(b"private response", content_length=33)
    monkeypatch.setattr(
        server.capture.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )

    with pytest.raises(RuntimeError) as raised:
        server.capture._mistral_post(
            "https://provider.invalid/ocr",
            {},
            "private-key",
            1,
            maximum_response_bytes=32,
        )

    assert response.read_sizes == []
    assert "private-key" not in str(raised.value)
    assert "private response" not in str(raised.value)


def test_claude_http_response_read_is_bounded_and_sanitized(
    monkeypatch,
) -> None:
    response = _HttpResponse(b"x" * 9)
    monkeypatch.setattr(
        server.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: response,
    )
    monkeypatch.setattr(
        server,
        "_CORRECTION_OCR_CLAUDE_MAX_RESPONSE_BYTES",
        8,
    )

    with pytest.raises(RuntimeError) as raised:
        server._ocr_claude(
            b"image",
            {
                "claude_key": "private-claude-key",
                "claude_model": "pinned",
            },
        )

    assert response.read_sizes == [9]
    assert "private-claude-key" not in str(raised.value)
    assert response.payload.decode("ascii") not in str(raised.value)
