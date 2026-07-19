"""Stable HTTP composition for filesystem-backed translation aggregates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def translation_workspace(monkeypatch, tmp_path: Path):
    import server

    root = tmp_path / "output"
    builds_path = root / "whl_builds.json"
    entries_dir = root / "entries"
    alternate = tmp_path / "alternate.pdf"
    alternate.write_bytes(b"%PDF-alternate")
    builds = {
        "book-one": {
            "id": "book-one",
            "title": "Herbal",
            "ocr_active": "compiled.txt",
            "pdf_sources": [{"id": "scan-two", "path": str(alternate)}],
        }
    }
    root.mkdir()
    server.lib.save_json(builds_path, builds)
    entry = entries_dir / "book-one"
    (entry / "ocr").mkdir(parents=True)
    (entry / "translations").mkdir()
    # The preamble is intentionally retained by the legacy OCR merge writer
    # and ignored by the legacy Analyze parser.
    (entry / "ocr" / "compiled.txt").write_text(
        "legacy preamble\n\n"
        "--- page 1 ---\nAlpha source.\n\n"
        "--- page 2 ---\nBeta source.\n",
        encoding="utf-8",
    )
    (entry / "ocr" / "sources.json").write_text(
        json.dumps({"compiled.txt": "scan-two"}), encoding="utf-8")
    (entry / "translations" / "es.txt").write_text(
        "--- page 1 ---\nUno.\n\n--- page 2 ---\nDos.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)
    session = server._open_engine_session(root)
    monkeypatch.setattr(server, "_engine_session", session)
    monkeypatch.setattr(server, "_engine_write_set", session.write_set)
    monkeypatch.setattr(server, "_job_manager", session.jobs)
    monkeypatch.setattr(server, "_translation_provenance", session.provenance)
    monkeypatch.setattr(server, "_jobs", session.jobs.records)
    monkeypatch.setattr(server, "_jobs_events", session.jobs.cancel_events)
    monkeypatch.setattr(server, "_jobs_lock", session.jobs.lock)
    monkeypatch.setattr(server, "_library_engine_instance", session.engine)
    try:
        yield server, entry
    finally:
        session.close()


def test_translation_reads_are_versioned_coherent_and_revalidatable(
    client, translation_workspace
):
    server, _entry = translation_workspace

    collection = client.get("/api/v1/items/book-one/translations")
    assert collection.status_code == 200
    body = collection.get_json()
    assert body["schema"] == "librarytool.translation-summaries/1"
    assert collection.headers["ETag"] == f'"{body["revision"]}"'
    assert len(body["translations"]) == 1
    summary = body["translations"][0]
    assert summary["target_language"] == "es"
    assert summary["source"]["representation_id"] == "scan-two"
    assert summary["status"]["untracked"] == ["page:1", "page:2"]

    unchanged = client.get(
        "/api/v1/items/book-one/translations",
        headers={"If-None-Match": collection.headers["ETag"]},
    )
    assert unchanged.status_code == 304

    detail = client.get(
        f'/api/v1/items/book-one/translations/{summary["id"]}')
    assert detail.status_code == 200
    view = detail.get_json()["translation"]
    assert detail.headers["ETag"] == f'"{view["view_revision"]}"'
    assert detail.headers["X-Document-Revision"] == view["document_revision"]
    assert detail.headers["X-Source-Revision"] == view["source"]["revision"]
    assert [(page["selector"], page["source_text"]) for page in view["pages"]] == [
        ("page:1", "Alpha source."),
        ("page:2", "Beta source."),
    ]
    assert server._translation_document_name(
        view["source"]["layer_id"]) == "compiled.txt"
    assert server._translation_layer_id("compiled.txt") == (
        view["source"]["layer_id"])
    detail_unchanged = client.get(
        f'/api/v1/items/book-one/translations/{summary["id"]}',
        headers={"If-None-Match": detail.headers["ETag"]},
    )
    assert detail_unchanged.status_code == 304
    assert detail_unchanged.get_data() == b""


def test_translation_page_put_requires_both_revisions_and_writes_atomically(
    client, translation_workspace
):
    _server, entry = translation_workspace
    summary = client.get(
        "/api/v1/items/book-one/translations").get_json()["translations"][0]
    url = (
        f'/api/v1/items/book-one/translations/{summary["id"]}/pages/page:1')
    current = client.get(
        f'/api/v1/items/book-one/translations/{summary["id"]}'
    ).get_json()["translation"]

    missing = client.put(url, json={"text": "Nueva."})
    assert missing.status_code == 428
    assert missing.get_json()["code"] == "translation_preconditions_required"
    assert missing.get_json()["details"]["required"] == [
        {"field": "expected_document_revision",
         "header": "If-Document-Match"},
        {"field": "expected_source_revision", "header": "If-Source-Match"},
    ]

    saved = client.put(url, json={"text": "Nueva."}, headers={
        "If-Document-Match": f'"{current["document_revision"]}"',
        "If-Source-Match": f'"{current["source"]["revision"]}"',
    })
    assert saved.status_code == 200
    updated = saved.get_json()["translation"]
    page = next(page for page in updated["pages"]
                if page["selector"] == "page:1")
    assert page["text"] == "Nueva."
    assert page["origin"] == "human"
    assert page["state"] == "current"
    assert saved.headers["ETag"] == f'"{updated["view_revision"]}"'
    assert "--- page 1 ---\nNueva." in (
        entry / "translations" / "es.txt").read_text(encoding="utf-8")
    manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
    producer = manifest["artifacts"]["translations/es.txt"]["produced_by"]
    assert producer["engine"] == "translation-aggregate"
    assert producer["kind"] == "manual-edit"
    assert producer["source_revision"] == updated["source"]["revision"]

    conflict = client.put(url, json={"text": "Otra."}, headers={
        "If-Document-Match": f'"{current["document_revision"]}"',
        "If-Source-Match": f'"{current["source"]["revision"]}"',
    })
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == (
        "stale_translation_document_revision")
    assert conflict.get_json()["details"]["current_document_revision"] == (
        updated["document_revision"])


def test_translation_page_put_detects_authoritative_source_race(
    client, translation_workspace
):
    _server, entry = translation_workspace
    summary = client.get(
        "/api/v1/items/book-one/translations").get_json()["translations"][0]
    detail = client.get(
        f'/api/v1/items/book-one/translations/{summary["id"]}'
    ).get_json()["translation"]
    (entry / "ocr" / "compiled.txt").write_text(
        "--- page 1 ---\nCorrected.\n", encoding="utf-8")

    response = client.put(
        f'/api/v1/items/book-one/translations/{summary["id"]}/pages/page:1',
        json={"text": "Nueva."},
        headers={
            "If-Document-Match": f'"{detail["document_revision"]}"',
            "If-Source-Match": f'"{detail["source"]["revision"]}"',
        },
    )
    assert response.status_code == 409
    body = response.get_json()
    assert body["code"] == "stale_translation_source_revision"
    assert body["details"]["current_source_revision"] != (
        detail["source"]["revision"])


@pytest.mark.parametrize(
    "source_payload",
    [
        b"--- page 1 ---\nFirst\n\n--- page 1 ---\nDuplicate\n",
        b"--- page 01 ---\nNon-canonical\n",
        b"\xff",
    ],
)
def test_malformed_authoritative_ocr_is_a_structured_engine_error(
    client, translation_workspace, source_payload
):
    _server, entry = translation_workspace
    (entry / "ocr" / "compiled.txt").write_bytes(source_payload)

    response = client.get("/api/v1/items/book-one/translations")

    assert response.status_code == 500
    assert response.get_json()["code"] == "invalid_translation_source_snapshot"


def test_invalid_source_binding_and_translation_errors_are_structured(
    client, translation_workspace
):
    _server, entry = translation_workspace
    (entry / "ocr" / "sources.json").write_text(
        '{"compiled.txt":"missing-source"}', encoding="utf-8")
    malformed = client.get("/api/v1/items/book-one/translations")
    assert malformed.status_code == 500
    assert malformed.get_json()["code"] == "invalid_translation_source_snapshot"

    (entry / "ocr" / "sources.json").write_text("{}", encoding="utf-8")
    missing = client.get("/api/v1/items/book-one/translations/not-here")
    invalid = client.put(
        "/api/v1/items/book-one/translations/not-here/pages/not/a/page",
        json={"text": "x", "expected_document_revision": "tr-old",
              "expected_source_revision": "ts-old"},
    )
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "translation_not_found"
    assert invalid.status_code == 404


def test_present_invalid_source_maps_are_not_treated_as_absent(
    client, translation_workspace
):
    _server, entry = translation_workspace
    source_map = entry / "ocr" / "sources.json"
    source_map.write_text(
        '{"Compiled.txt":"scan-two"}', encoding="utf-8")
    selected_alias = client.get("/api/v1/items/book-one/translations")
    assert selected_alias.status_code == 500
    assert selected_alias.get_json()["code"] == (
        "invalid_translation_source_snapshot")

    source_map.write_text(
        '{"Compiled.txt":"primary","compiled.txt":"scan-two"}',
        encoding="utf-8",
    )
    aliased = client.get("/api/v1/items/book-one/translations")
    assert aliased.status_code == 500
    assert aliased.get_json()["code"] == "invalid_translation_source_snapshot"

    source_map.unlink()
    source_map.mkdir()
    non_file = client.get("/api/v1/items/book-one/translations")
    assert non_file.status_code == 500
    assert non_file.get_json()["code"] == "invalid_translation_source_snapshot"


def test_present_non_file_ocr_source_is_not_treated_as_unavailable(
    client, translation_workspace
):
    _server, entry = translation_workspace
    source = entry / "ocr" / "compiled.txt"
    source.unlink()
    source.mkdir()

    response = client.get("/api/v1/items/book-one/translations")

    assert response.status_code == 500
    assert response.get_json()["code"] == "invalid_translation_source_snapshot"


def test_translation_preconditions_reject_weak_or_malformed_headers(
    client, translation_workspace
):
    _server, _entry = translation_workspace
    summary = client.get(
        "/api/v1/items/book-one/translations").get_json()["translations"][0]
    detail = client.get(
        f'/api/v1/items/book-one/translations/{summary["id"]}'
    ).get_json()["translation"]
    url = (
        f'/api/v1/items/book-one/translations/{summary["id"]}/pages/page:1')
    source = detail["source"]["revision"]
    document = detail["document_revision"]

    weak = client.put(url, json={"text": "Nueva."}, headers={
        "If-Document-Match": f'W/"{document}"',
        "If-Source-Match": f'"{source}"',
    })
    malformed = client.put(url, json={"text": "Nueva."}, headers={
        "If-Document-Match": document,
        "If-Source-Match": f'"{source}"',
    })
    assert weak.status_code == malformed.status_code == 400
    assert weak.get_json()["code"] == "invalid_translation_page_update"
    assert malformed.get_json()["details"]["header"] == (
        "If-Document-Match")

    body_fallback = client.put(url, json={
        "text": "Nueva.",
        "expected_document_revision": document,
        "expected_source_revision": source,
    })
    assert body_fallback.status_code == 200


def test_translation_capabilities_are_independently_discoverable(
    client, translation_workspace
):
    capabilities = {
        row["id"] for row in
        client.get("/api/v1/capabilities").get_json()["capabilities"]
    }
    assert {
        "translation.layers.read",
        "translation.layers.status",
        "translation.layers.edit",
    } <= capabilities
