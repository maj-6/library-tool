"""HTTP contract for the optional revisioned text-layer aggregate."""

from __future__ import annotations

import json
from contextlib import nullcontext
from io import BytesIO
from pathlib import Path

import pytest
from flask import Flask

from librarytool.adapters.filesystem import (
    FilesystemTextLayerAggregateRepository,
    RecoverableWriteSet,
)
from librarytool.engine.runtime import TEXT_LAYER_AGGREGATE_SERVICE
from librarytool.engine.text_layer_aggregate import (
    CreateTextLayerCommand,
    TextLayerAggregateService,
    TextLayerDraft,
    TextLayerProvenance,
    TextLayerSourcePin,
    TextLayerSourceSnapshot,
    TextLayerUnitDraft,
)
from librarytool_http import create_text_layer_blueprint
from librarytool_http import text_layers as text_layer_http_adapter


ITEM_ID = "book-one"
LAYER_ID = "layer-one"
REPRESENTATION_ID = "scan-main"
SOURCE_REVISION = "source-r1"


def _provenance(**overrides):
    value = {
        "origin": "human",
        "review_state": "reviewed",
        "provider_id": "",
        "model": "",
        "recipe_revision": "",
        "updated_at": "2026-07-19T19:00:00Z",
        "metadata": {"editor": "Ada"},
    }
    value.update(overrides)
    return value


class _Engine:
    def __init__(self, service):
        self.service = service
        self.lookups = []

    def get_service(self, key):
        self.lookups.append(key)
        return self.service if key == TEXT_LAYER_AGGREGATE_SERVICE else None


@pytest.fixture()
def text_layer_http(tmp_path: Path):
    root = tmp_path / "workspace"
    entry = root / "entries" / ITEM_ID
    entry.mkdir(parents=True)
    source_revision = [SOURCE_REVISION]

    def source_snapshot(item_id: str, representation_id: str):
        if item_id != ITEM_ID or representation_id != REPRESENTATION_ID:
            return None
        return TextLayerSourceSnapshot(
            item_id,
            representation_id,
            source_revision[0],
        )

    repository = FilesystemTextLayerAggregateRepository(
        RecoverableWriteSet(root),
        item_exists_for=lambda item_id: item_id == ITEM_ID,
        entry_directory_for=lambda item_id: entry,
        source_snapshot_for=source_snapshot,
        lock_context_for=nullcontext,
        layer_id_factory=lambda: LAYER_ID,
        recover=False,
    )
    service = TextLayerAggregateService(repository)
    service.create(
        CreateTextLayerCommand(
            item_id=ITEM_ID,
            operation_id="seed-layer",
            draft=TextLayerDraft(
                label="Diplomatic transcription",
                kind="transcription",
                language="la",
                source=TextLayerSourcePin(
                    REPRESENTATION_ID,
                    SOURCE_REVISION,
                ),
                preamble="Shelf note",
                units=(
                    TextLayerUnitDraft(
                        selector="canvas-a",
                        order=1,
                        label="Folio A",
                        text="Alpha",
                        provenance=TextLayerProvenance(
                            origin="machine",
                            provider_id="local-ocr",
                            recipe_revision="recipe-r1",
                        ),
                    ),
                    TextLayerUnitDraft(
                        selector="canvas-b",
                        order=2,
                        label="Folio B",
                        text="Beta",
                    ),
                    TextLayerUnitDraft(
                        selector="canvas-c",
                        order=3,
                        label="Folio C",
                        text="Gamma",
                    ),
                ),
            ),
        )
    )
    engine = _Engine(service)
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_text_layer_blueprint(lambda: engine))
    with app.test_client() as client:
        yield client, engine, service, source_revision, root


def _detail(client):
    return client.get(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}"
    )


def _replace_headers(detail, **overrides):
    unit = detail["text_layer"]["document"]["units"][0]
    source = detail["text_layer"]["source"]
    headers = {
        "Idempotency-Key": "replace-unit-one",
        "If-Unit-Match": f'"{unit["unit_revision"]}"',
        "If-Source-Match": f'"{source["pinned_revision"]}"',
    }
    headers.update(overrides)
    return headers


def _replace_body(text="Corrected"):
    return {"replacement": {"text": text, "provenance": _provenance()}}


def _page_headers(detail, **overrides):
    document = detail["text_layer"]["document"]
    headers = {
        "If-Document-Match": f'"{document["document_revision"]}"',
        "If-Source-Match": f'"{document["source"]["revision"]}"',
    }
    headers.update(overrides)
    return headers


def _unit_page(client, detail, *, page=1, limit=2, headers=None):
    query = f"page={page}&limit={limit}"
    return client.get(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units?{query}",
        headers=headers or _page_headers(detail),
    )


def test_reads_are_versioned_coherent_and_revalidatable(text_layer_http):
    client, engine, _service, _source_revision, _root = text_layer_http

    collection = client.get(f"/api/v1/items/{ITEM_ID}/text-layers")
    assert collection.status_code == 200
    body = collection.get_json()
    assert body["schema"] == "librarytool.text-layer-summaries/1"
    assert body["item_id"] == ITEM_ID
    assert body["text_layers"][0]["layer_id"] == LAYER_ID
    assert collection.headers["ETag"] == f'"{body["revision"]}"'
    assert not collection.headers["ETag"].startswith("W/")

    unchanged = client.get(
        f"/api/v1/items/{ITEM_ID}/text-layers",
        headers={"If-None-Match": collection.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""

    detail = _detail(client)
    assert detail.status_code == 200
    view = detail.get_json()["text_layer"]
    document = view["document"]
    assert detail.get_json()["schema"] == "librarytool.text-layer/1"
    assert detail.headers["ETag"] == f'"{view["view_revision"]}"'
    assert detail.headers["X-Document-Revision"] == document[
        "document_revision"
    ]
    assert detail.headers["X-Content-Revision"] == document[
        "content_revision"
    ]
    assert detail.headers["X-Source-Revision"] == SOURCE_REVISION
    detail_unchanged = client.get(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}",
        headers={"If-None-Match": detail.headers["ETag"]},
    )
    assert detail_unchanged.status_code == 304
    assert all(key == TEXT_LAYER_AGGREGATE_SERVICE for key in engine.lookups)


def test_detail_response_has_an_exact_encoded_ceiling(
    text_layer_http,
    monkeypatch,
):
    client, _engine, _service, _source_revision, _root = text_layer_http
    baseline = _detail(client)
    assert baseline.status_code == 200
    exact_size = len(baseline.get_data())

    monkeypatch.setattr(
        text_layer_http_adapter,
        "TEXT_LAYER_DETAIL_MAX_BYTES",
        exact_size,
    )
    at_limit = _detail(client)
    assert at_limit.status_code == 200
    assert at_limit.get_data() == baseline.get_data()

    monkeypatch.setattr(
        text_layer_http_adapter,
        "TEXT_LAYER_DETAIL_MAX_BYTES",
        exact_size - 1,
    )
    over_limit = _detail(client)
    assert over_limit.status_code == 413
    assert over_limit.get_json()["code"] == "text_layer_detail_too_large"
    assert over_limit.get_json()["details"] == {
        "maximum_bytes": exact_size - 1
    }
    assert over_limit.cache_control.no_store is True
    assert over_limit.headers["Pragma"] == "no-cache"


def test_unit_pages_are_pinned_ordered_and_revalidatable(text_layer_http):
    client, _engine, _service, _source_revision, _root = text_layer_http
    detail = _detail(client).get_json()

    first = _unit_page(client, detail, limit=2)
    assert first.status_code == 200
    body = first.get_json()
    page = body["page"]
    assert body["schema"] == "librarytool.text-layer-unit-page/1"
    assert [value["selector"] for value in page["units"]] == [
        "canvas-a",
        "canvas-b",
    ]
    assert page["page"] == 1
    assert page["next_page"] == 2
    assert page["has_more"] is True
    assert page["unit_count"] == 3
    assert page["limit"] == 2
    assert first.headers["ETag"] == f'"{page["page_revision"]}"'
    assert first.headers["X-Page-Revision"] == page["page_revision"]
    assert first.headers["X-Document-Revision"] == page[
        "document_revision"
    ]
    assert first.headers["X-Content-Revision"] == page["content_revision"]
    assert first.headers["X-Source-Revision"] == SOURCE_REVISION
    assert first.cache_control.no_cache is True

    unchanged = _unit_page(
        client,
        detail,
        limit=2,
        headers={
            **_page_headers(detail),
            "If-None-Match": first.headers["ETag"],
        },
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""
    assert unchanged.headers["X-Document-Revision"] == page[
        "document_revision"
    ]

    second = _unit_page(client, detail, page=2, limit=2)
    second_page = second.get_json()["page"]
    assert [value["selector"] for value in second_page["units"]] == [
        "canvas-c"
    ]
    assert second_page["page"] == 2
    assert second_page["next_page"] is None
    assert second_page["has_more"] is False
    assert second_page["document_revision"] == page["document_revision"]
    assert second_page["source_revision"] == page["source_revision"]
    assert second_page["page_revision"] != page["page_revision"]


def test_unit_page_requires_exact_pins_and_strict_bounded_query(
    text_layer_http,
):
    client, _engine, _service, _source_revision, _root = text_layer_http
    detail = _detail(client).get_json()
    url = (
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units"
        "?page=1&limit=2"
    )
    headers = _page_headers(detail)

    for missing, code in (
        ("If-Document-Match", "text_layer_document_revision_required"),
        ("If-Source-Match", "text_layer_source_revision_required"),
    ):
        response = client.get(
            url,
            headers={key: value for key, value in headers.items()
                     if key != missing},
        )
        assert response.status_code == 428
        assert response.get_json()["code"] == code

    for name, value, code in (
        (
            "If-Document-Match",
            'W/"tld-old"',
            "invalid_text_layer_document_revision",
        ),
        (
            "If-Source-Match",
            SOURCE_REVISION,
            "invalid_text_layer_source_revision",
        ),
    ):
        response = client.get(url, headers={**headers, name: value})
        assert response.status_code == 400
        assert response.get_json()["code"] == code

    cases = (
        ("", "text_layer_page_range_required"),
        ("?page=1", "text_layer_page_range_required"),
        ("?limit=2", "text_layer_page_range_required"),
        ("?page=0&limit=2", "invalid_text_layer_page_number"),
        ("?page=100001&limit=2", "invalid_text_layer_page_number"),
        ("?page=01&limit=2", "invalid_text_layer_page_number"),
        ("?page=1&limit=0", "invalid_text_layer_page_limit"),
        ("?page=1&limit=257", "invalid_text_layer_page_limit"),
        ("?page=1&limit=01", "invalid_text_layer_page_limit"),
        ("?page=1&page=2&limit=2", "invalid_text_layer_page_query"),
        ("?page=1&limit=2&limit=3", "invalid_text_layer_page_query"),
        ("?page=1&limit=2&unknown=1", "invalid_text_layer_page_query"),
    )
    base = f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units"
    for query, code in cases:
        response = client.get(base + query, headers=headers)
        assert response.status_code == 400
        assert response.get_json()["code"] == code


def test_unit_page_rejects_stale_document_or_source_pins(text_layer_http):
    client, _engine, _service, _source_revision, _root = text_layer_http
    detail = _detail(client).get_json()

    stale_document = _unit_page(
        client,
        detail,
        headers=_page_headers(
            detail,
            **{"If-Document-Match": '"tld-stale"'},
        ),
    )
    assert stale_document.status_code == 409
    assert stale_document.get_json()["code"] == (
        "text_layer_document_revision_conflict"
    )

    stale_source = _unit_page(
        client,
        detail,
        headers=_page_headers(
            detail,
            **{"If-Source-Match": '"source-stale"'},
        ),
    )
    assert stale_source.status_code == 409
    assert stale_source.get_json()["code"] == (
        "text_layer_source_revision_conflict"
    )

    missing_page = _unit_page(
        client,
        detail,
        page=3,
        limit=2,
    )
    assert missing_page.status_code == 404
    assert missing_page.get_json()["code"] == (
        "text_layer_unit_page_not_found"
    )


def test_paged_units_remain_available_when_coherent_detail_exceeds_its_cap(
    text_layer_http,
    monkeypatch,
):
    client, _engine, _service, _source_revision, _root = text_layer_http
    detail_body = _detail(client).get_json()
    headers = _page_headers(detail_body)
    monkeypatch.setattr(text_layer_http_adapter, "TEXT_LAYER_DETAIL_MAX_BYTES", 1)

    detail = _detail(client)
    assert detail.status_code == 413
    page = client.get(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units?page=1&limit=1",
        headers=headers,
    )
    assert page.status_code == 200
    assert len(page.get_json()["page"]["units"]) == 1


def test_unit_page_response_has_an_exact_encoded_ceiling(
    text_layer_http,
    monkeypatch,
):
    client, _engine, _service, _source_revision, _root = text_layer_http
    detail = _detail(client).get_json()
    baseline = _unit_page(client, detail, limit=1)
    assert baseline.status_code == 200
    exact_size = len(baseline.get_data())

    monkeypatch.setattr(
        text_layer_http_adapter,
        "TEXT_LAYER_UNIT_PAGE_MAX_BYTES",
        exact_size,
    )
    at_limit = _unit_page(client, detail, limit=1)
    assert at_limit.status_code == 200
    assert at_limit.get_data() == baseline.get_data()

    monkeypatch.setattr(
        text_layer_http_adapter,
        "TEXT_LAYER_UNIT_PAGE_MAX_BYTES",
        exact_size - 1,
    )
    over_limit = _unit_page(client, detail, limit=1)
    assert over_limit.status_code == 413
    assert over_limit.get_json()["code"] == (
        "text_layer_unit_page_response_too_large"
    )
    assert over_limit.get_json()["details"] == {
        "maximum_bytes": exact_size - 1
    }
    assert over_limit.cache_control.no_store is True
    assert over_limit.headers["Pragma"] == "no-cache"


def test_replace_unit_is_idempotent_public_and_not_cacheable(text_layer_http):
    client, _engine, _service, _source_revision, root = text_layer_http
    current = _detail(client).get_json()
    headers = _replace_headers(current)

    saved = client.put(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units/canvas-a",
        json=_replace_body(),
        headers=headers,
    )
    assert saved.status_code == 200
    body = saved.get_json()
    assert body["schema"] == "librarytool.text-layer-mutation-receipt/1"
    assert body["replayed"] is False
    assert body["receipt"]["action"] == "replace-unit"
    assert body["receipt"]["operation_id"] == "replace-unit-one"
    assert "command_sha256" not in saved.get_data(as_text=True)
    assert saved.cache_control.no_store is True
    assert saved.headers["Pragma"] == "no-cache"
    assert saved.headers["X-Document-Revision"] == body["receipt"][
        "after_document_revision"
    ]
    assert saved.headers["X-Content-Revision"] == body["receipt"][
        "after_content_revision"
    ]
    assert saved.headers["X-Source-Revision"] == SOURCE_REVISION
    assert _detail(client).get_json()["text_layer"]["document"]["units"][0][
        "text"
    ] == "Corrected"

    committed = tuple(path.read_bytes() for path in sorted(root.rglob("*.json")))
    replay = client.put(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units/canvas-a",
        json=_replace_body(),
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == body["receipt"]
    assert tuple(path.read_bytes() for path in sorted(root.rglob("*.json"))) == (
        committed
    )


def test_mutation_requires_strong_headers_and_exact_json(text_layer_http):
    client, _engine, _service, _source_revision, _root = text_layer_http
    current = _detail(client).get_json()
    headers = _replace_headers(current)
    url = f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units/canvas-a"

    required = [
        ({key: value for key, value in headers.items()
          if key != "Idempotency-Key"}, "idempotency_key_required"),
        ({key: value for key, value in headers.items()
          if key != "If-Unit-Match"}, "text_layer_unit_revision_required"),
        ({key: value for key, value in headers.items()
          if key != "If-Source-Match"}, "text_layer_source_revision_required"),
    ]
    for candidate, code in required:
        response = client.put(url, json=_replace_body(), headers=candidate)
        assert response.status_code == 428
        assert response.get_json()["code"] == code

    for name, value, code in (
        ("If-Unit-Match", 'W/"unit-old"',
         "invalid_text_layer_unit_revision"),
        ("If-Source-Match", "source-r1",
         "invalid_text_layer_source_revision"),
    ):
        response = client.put(
            url,
            json=_replace_body(),
            headers={**headers, name: value},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == code

    wrong_type = client.put(
        url,
        data=json.dumps(_replace_body()),
        content_type="text/plain",
        headers=headers,
    )
    assert wrong_type.status_code == 400
    assert wrong_type.get_json()["code"] == (
        "invalid_text_layer_mutation_document"
    )

    duplicate = client.put(
        url,
        data=(
            '{"replacement":{"text":"one","text":"two",'
            '"provenance":' + json.dumps(_provenance()) + "}}"
        ),
        content_type="application/json",
        headers=headers,
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["code"] == (
        "invalid_text_layer_mutation_document"
    )

    extra = client.put(
        url,
        json={**_replace_body(), "expected_unit_revision": "unit-old"},
        headers=headers,
    )
    assert extra.status_code == 400
    assert extra.get_json()["code"] == "invalid_text_layer_mutation_envelope"

    incomplete = _replace_body()
    del incomplete["replacement"]["provenance"]["metadata"]
    invalid = client.put(url, json=incomplete, headers=headers)
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_text_layer_unit_replacement"


def test_mutation_body_is_bounded_before_publication(
    text_layer_http,
    monkeypatch,
):
    client, _engine, _service, _source_revision, root = text_layer_http
    current = _detail(client).get_json()
    headers = _replace_headers(current)
    monkeypatch.setattr(
        text_layer_http_adapter,
        "TEXT_LAYER_MUTATION_MAX_BYTES",
        128,
    )
    before = tuple(path.read_bytes() for path in sorted(root.rglob("*.json")))
    url = f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units/canvas-a"
    payload = b"{" + b"x" * 128
    known_length = client.put(
        url,
        data=payload,
        content_type="application/json",
        headers=headers,
    )
    assert known_length.status_code == 413
    assert known_length.get_json()["code"] == "text_layer_mutation_too_large"
    assert known_length.cache_control.no_store is True

    unknown_length = client.open(
        url,
        method="PUT",
        input_stream=BytesIO(payload),
        content_type="application/json",
        headers=headers,
        environ_overrides={
            "CONTENT_LENGTH": "",
            "wsgi.input_terminated": True,
        },
    )
    assert unknown_length.status_code == 413
    assert unknown_length.get_json()["code"] == (
        "text_layer_mutation_too_large"
    )
    assert tuple(path.read_bytes() for path in sorted(root.rglob("*.json"))) == (
        before
    )


def test_engine_errors_have_exact_http_classes(text_layer_http):
    client, _engine, _service, _source_revision, _root = text_layer_http
    missing = client.get(
        f"/api/v1/items/{ITEM_ID}/text-layers/not-here"
    )
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "text_layer_not_found"
    assert missing.cache_control.no_store is True
    assert missing.headers["Pragma"] == "no-cache"

    current = _detail(client).get_json()
    stale = client.put(
        f"/api/v1/items/{ITEM_ID}/text-layers/{LAYER_ID}/units/canvas-a",
        json=_replace_body(),
        headers={
            **_replace_headers(current),
            "If-Unit-Match": '"tur-stale"',
            "Idempotency-Key": "stale-unit",
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "text_layer_unit_revision_conflict"
    assert stale.get_json()["conflict"] == (
        "text_layer_unit_revision_conflict"
    )


def test_server_registers_transport_without_binding_production_storage(client):
    response = client.get("/api/v1/items/not-present/text-layers")
    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "error": "the text layer aggregate module is unavailable",
        "code": "text_layer_module_unavailable",
        "retryable": True,
    }
