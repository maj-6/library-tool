"""Versioned HTTP adapter for the engine item-query spine."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def item_catalog(monkeypatch, tmp_path: Path):
    import server

    builds_path = tmp_path / "whl_builds.json"
    entries_dir = tmp_path / "entries"
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)
    monkeypatch.setattr(server, "_library_engine_instance", None)

    private = tmp_path / "private" / "scan.pdf"
    alternate = tmp_path / "private" / "alternate.pdf"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"%PDF-private")
    alternate.write_bytes(b"%PDF-alternate")
    builds = {
        "book-one": {
            "id": "book-one",
            "title": "A New Herbal",
            "authors": "A. Author",
            "language": "en",
            "rights": "public-domain",
            "updated_at": "2026-01-02T03:04:05+00:00",
            "pdf_file": str(private),
            "pdf_sources": [{"id": "scan-two", "path": str(alternate)}],
            "images": [str(tmp_path / "private" / "cover.jpg")],
            "extra": {"workspace_path": str(tmp_path / "private")},
        },
        "book-two": {
            "id": "book-two",
            "title": "Zoologia",
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
    }
    server.lib.save_json(builds_path, builds)

    entry = entries_dir / "book-one"
    (entry / "ocr").mkdir(parents=True)
    (entry / "translations").mkdir()
    (entry / "analysis").mkdir()
    (entry / "ocr" / "compiled.txt").write_text("herbal text", encoding="utf-8")
    (entry / "translations" / "fr.txt").write_text(
        "--- page 1 ---\ntexte", encoding="utf-8"
    )
    (entry / "analysis" / "notes.md").write_text("notes", encoding="utf-8")
    (entry / "summary.md").write_text("summary", encoding="utf-8")

    yield server, builds, private
    server._library_engine_instance = None


def test_default_item_collection_is_portable_revisioned_and_revalidatable(
    client, item_catalog
):
    _server, _builds, private = item_catalog

    response = client.get("/api/v1/items")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["schema"] == "librarytool.items/1"
    assert [item["id"] for item in body["items"]] == ["book-one", "book-two"]
    assert response.headers["ETag"] == f'"{body["revision"]}"'

    item = body["items"][0]
    assert item["record_revision"] == "2026-01-02T03:04:05+00:00"
    assert item["metadata"]["authors"] == "A. Author"
    assert "pdf_file" not in item["metadata"]
    assert "pdf_sources" not in item["metadata"]
    assert "images" not in item["metadata"]
    assert "extra" not in item["metadata"]
    assert all(
        row["locator"].startswith("urn:librarytool:item:")
        for row in item["representations"]
    )
    assert str(private) not in response.get_data(as_text=True)
    assert {row["kind"] for row in item["artifacts"]} >= {
        "ocr", "translation", "analysis", "summary",
    }
    assert item["workbench_state"]["readiness"]["text"] == "untracked"
    assert item["workbench_state"]["readiness"]["translation"] == "untracked"

    unchanged = client.get(
        "/api/v1/items", headers={"If-None-Match": response.headers["ETag"]}
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""


def test_legacy_build_projection_is_explicit_and_preserves_current_ui_shape(
    client, item_catalog
):
    _server, builds, private = item_catalog

    default = client.get("/api/v1/items/book-one").get_json()["item"]
    projected = client.get(
        "/api/v1/items/book-one?projection=build-workbench"
    ).get_json()["item"]

    assert "compatibility" not in default
    assert projected["compatibility"]["schema"] == "librarytool.build-record/1"
    assert projected["compatibility"]["build"] == builds["book-one"]
    assert projected["compatibility"]["build"]["pdf_file"] == str(private)


def test_item_child_resources_and_readiness_have_machine_contracts(
    client, item_catalog
):
    representations = client.get(
        "/api/v1/items/book-one/representations"
    )
    artifacts = client.get("/api/v1/items/book-one/artifacts")
    readiness = client.get("/api/v1/items/book-one/readiness")

    assert representations.status_code == artifacts.status_code == 200
    assert representations.get_json()["schema"] == (
        "librarytool.representations/1"
    )
    assert [row["id"] for row in representations.get_json()["representations"]] == [
        "primary", "scan-two",
    ]
    assert artifacts.get_json()["schema"] == "librarytool.artifacts/1"
    assert readiness.get_json()["schema"] == "librarytool.workbench-state/1"
    assert readiness.get_json()["state"]["item_id"] == "book-one"
    assert readiness.headers["ETag"] == (
        f'"{readiness.get_json()["state"]["revision"]}"'
    )


def test_item_http_errors_are_structured(client, item_catalog):
    missing = client.get("/api/v1/items/missing/artifacts")
    invalid = client.get("/api/v1/items?projection=filesystem")

    assert missing.status_code == 404
    assert missing.get_json() == {
        "ok": False,
        "error": "the item does not exist",
        "code": "item_not_found",
        "retryable": False,
        "details": {"item_id": "missing"},
    }
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_item_projection"


def test_item_capability_discovery_keeps_compatibility_and_adds_read_facets(
    client, item_catalog
):
    document = client.get("/api/v1/capabilities").get_json()
    capabilities = {
        (row["id"], row["version"]) for row in document["capabilities"]
    }
    assert {
        ("library.items", 1),
        ("library.items.read", 1),
        ("library.representations", 1),
        ("library.artifacts", 1),
    } <= capabilities


def test_projection_does_not_enable_local_visual_tools_for_remote_only_sources(
    item_catalog,
):
    server, _builds, _private = item_catalog

    remote = server._engine_source_snapshot(
        "remote", "primary", "https://example.test/scan.pdf",
        role="primary", label="Primary source",
    )
    first = server._engine_artifact_id(
        "book", "ocr", "compiled.txt", source_id="primary"
    )
    second = server._engine_artifact_id(
        "book", "ocr", "compiled.txt", source_id="scan-two"
    )

    assert remote["available"] is False
    assert first != second
