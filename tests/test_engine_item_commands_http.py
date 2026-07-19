"""Production HTTP composition for recoverable catalogue commands."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest


def _bind_engine_session(monkeypatch, server, session) -> None:
    """Keep transitional server globals on one temporary engine session."""

    aliases = {
        "_engine_session": session,
        "_engine_write_set": session.write_set,
        "_job_manager": session.jobs,
        "_translation_provenance": session.provenance,
        "_jobs": session.jobs.records,
        "_jobs_events": session.jobs.cancel_events,
        "_jobs_lock": session.jobs.lock,
        "_library_engine_instance": session.engine,
    }
    for name, value in aliases.items():
        monkeypatch.setattr(server, name, value)


def _item(
    *,
    title: str = "A New Herbal",
    metadata: dict | None = None,
    representations: list | None = None,
) -> dict:
    return {
        "kind": "book",
        "title": title,
        "metadata": (
            {"authors": "Ada Curator", "rights": "public-domain"}
            if metadata is None
            else metadata
        ),
        "representations": [] if representations is None else representations,
    }


def _patch(
    *,
    title: str | None = None,
    metadata_set: dict | None = None,
    metadata_remove: list | None = None,
    representations=None,
) -> dict:
    return {
        "title": title,
        "metadata_set": {} if metadata_set is None else metadata_set,
        "metadata_remove": [] if metadata_remove is None else metadata_remove,
        "representations": representations,
    }


@pytest.fixture()
def command_catalog(monkeypatch, tmp_path: Path):
    import server

    root = tmp_path / "output"
    builds_path = root / "whl_builds.json"
    entries_dir = root / "entries"
    root.mkdir()
    original = {
        "book-one": {
            "id": "book-one",
            "title": "The Old Herbal",
            "authors": "Old Author",
            "year": "1600",
            "rights": "public-domain",
            "status": "draft",
            "created_at": "2026-01-01T00:00:00.000000+00:00",
            "updated_at": "2026-01-02T00:00:00.000000+00:00",
            "pdf_file": r"C:\private\primary.pdf",
            "pdf_sources": [
                {"id": "Scan", "path": r"C:\private\alternate.pdf"},
                {"id": "scan", "path": r"C:\private\alternate-2.pdf"},
            ],
            "images": ["capture/cover.jpg"],
            "extra": {"workspace_path": r"C:\private"},
            "capture_id": "phone-1",
            "relevance": {"score": 0.75},
            "published_slug": "old-herbal",
            "ocr_active": "compiled.txt",
            "ocr_verified": "verified.txt",
            "ocr_quality": "reviewed",
            "title_pages": "1,3",
            "thumbnail_source": "page:1",
            "future.extension": {"nested": [1, True, None]},
        },
        "legacy-no-revision": {
            "id": "legacy-no-revision",
            "title": "Legacy Herbal",
            "authors": "Legacy Author",
            "rights": "",
            "created_at": "2025-12-01T00:00:00+00:00",
        },
    }
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)
    server.lib.save_json(builds_path, original)

    current_session = [server._open_engine_session(root)]
    _bind_engine_session(monkeypatch, server, current_session[0])

    def reopen_session():
        current_session[0].close()
        current_session[0] = server._open_engine_session(root)
        _bind_engine_session(monkeypatch, server, current_session[0])
        return current_session[0]

    try:
        yield server, builds_path, deepcopy(original), reopen_session
    finally:
        current_session[0].close()


def test_create_is_durable_replayable_and_conflict_safe(
    client, command_catalog
):
    server, builds_path, original, reopen_session = command_catalog
    document = {"item": _item(metadata={
        "authors": "Ada Curator",
        "rights": "public-domain",
        "future.catalogue": {"edition_note": "First state"},
    })}

    missing = client.post("/api/v1/items", json=document)
    assert missing.status_code == 428
    assert missing.get_json()["code"] == "idempotency_key_required"
    assert server.lib.load_json(builds_path, {}) == original

    created = client.post(
        "/api/v1/items", json=document,
        headers={"Idempotency-Key": "create-http-1"},
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["schema"] == "librarytool.item-mutation-receipt/1"
    assert body["replayed"] is False
    receipt = body["receipt"]
    item_id = receipt["item_id"]
    assert receipt["action"] == "create"
    assert receipt["before_revision"] == ""
    assert receipt["item"]["representations"] == []
    assert created.headers["X-Record-Revision"] == receipt["after_revision"]
    assert "ETag" not in created.headers

    stored = server.lib.load_json(builds_path, {})[item_id]
    assert stored["id"] == item_id
    assert stored["title"] == "A New Herbal"
    assert stored["authors"] == "Ada Curator"
    assert stored["created_at"] == stored["updated_at"]
    assert stored["status"] == "draft"
    first_bytes = builds_path.read_bytes()

    # Recomposition proves that replay comes from the durable receipt rather
    # than an in-memory request cache.
    reopen_session()
    replay = client.post(
        "/api/v1/items", json=document,
        headers={"Idempotency-Key": "create-http-1"},
    )
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == receipt
    assert builds_path.read_bytes() == first_bytes

    conflict = client.post(
        "/api/v1/items", json={"item": _item(title="Another Book")},
        headers={"Idempotency-Key": "create-http-1"},
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "operation_id_conflict"
    assert builds_path.read_bytes() == first_bytes

    queried = client.get(f"/api/v1/items/{item_id}")
    assert queried.status_code == 200
    view = queried.get_json()["item"]
    assert view["record_revision"] == receipt["after_revision"]
    assert queried.headers["X-Record-Revision"] == receipt["after_revision"]
    assert view["title"] == receipt["item"]["title"]
    assert view["metadata"] == receipt["item"]["metadata"]

    digest = hashlib.sha256(b"create-http-1").hexdigest()
    receipt_path = (
        builds_path.parent
        / f".engine/receipts/item-commands/{digest}.json"
    )
    assert receipt_path.is_file()


def test_update_preserves_raw_managed_state_and_supports_cas_replay(
    client, command_catalog
):
    server, builds_path, original, reopen_session = command_catalog
    detail = client.get("/api/v1/items/book-one")
    before_revision = detail.get_json()["item"]["record_revision"]
    assert detail.headers["ETag"] != f'"{before_revision}"'
    assert detail.headers["X-Record-Revision"] == before_revision
    patch = _patch(
        title="The Revised Herbal",
        metadata_set={
            "authors": "New Author",
            "future.second": {"confidence": 0.9},
        },
        metadata_remove=["year"],
    )
    headers = {
        "Idempotency-Key": "update-http-1",
        "If-Record-Match": f'"{before_revision}"',
    }

    response = client.patch(
        "/api/v1/items/book-one", json={"patch": patch}, headers=headers)
    assert response.status_code == 200
    result = response.get_json()
    receipt = result["receipt"]
    assert result["replayed"] is False
    assert receipt["before_revision"] == before_revision
    assert receipt["after_revision"] != before_revision
    assert response.headers["X-Record-Revision"] == receipt["after_revision"]
    assert "ETag" not in response.headers

    stored = server.lib.load_json(builds_path, {})["book-one"]
    managed = {
        "created_at", "pdf_file", "pdf_sources", "images", "extra",
        "capture_id", "relevance", "published_slug", "ocr_active",
        "ocr_verified", "ocr_quality", "title_pages", "thumbnail_source",
        "status",
    }
    assert {key: stored[key] for key in managed} == {
        key: original["book-one"][key] for key in managed
    }
    assert stored["title"] == "The Revised Herbal"
    assert stored["authors"] == "New Author"
    assert "year" not in stored
    assert stored["future.extension"] == original["book-one"][
        "future.extension"]
    assert stored["future.second"] == {"confidence": 0.9}
    assert stored["updated_at"] > original["book-one"]["updated_at"]
    assert not managed & set(receipt["item"]["metadata"])

    queried = client.get("/api/v1/items/book-one")
    assert queried.get_json()["item"]["record_revision"] == (
        receipt["after_revision"])
    assert queried.headers["X-Record-Revision"] == receipt["after_revision"]
    committed = builds_path.read_bytes()

    reopen_session()
    replay = client.patch(
        "/api/v1/items/book-one", json={"patch": patch}, headers=headers)
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == receipt
    assert builds_path.read_bytes() == committed

    changed_key = client.patch(
        "/api/v1/items/book-one",
        json={"patch": _patch(title="Conflicting retry")},
        headers=headers,
    )
    assert changed_key.status_code == 409
    assert changed_key.get_json()["code"] == "operation_id_conflict"

    stale = client.patch(
        "/api/v1/items/book-one", json={"patch": _patch(title="Stale")},
        headers={
            "Idempotency-Key": "update-http-stale",
            "If-Record-Match": f'"{before_revision}"',
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "item_revision_conflict"
    assert stale.get_json()["details"]["current_revision"] == (
        receipt["after_revision"])
    assert builds_path.read_bytes() == committed


def test_legacy_fallback_revision_is_shared_by_query_and_command_codec(
    client, command_catalog
):
    server, builds_path, original, _reopen_session = command_catalog
    detail = client.get("/api/v1/items/legacy-no-revision")
    revision = detail.get_json()["item"]["record_revision"]
    assert revision.startswith("ir-")
    assert detail.headers["X-Record-Revision"] == revision

    response = client.patch(
        "/api/v1/items/legacy-no-revision",
        json={"patch": _patch(metadata_set={"authors": "Corrected"})},
        headers={
            "Idempotency-Key": "update-legacy-1",
            "If-Record-Match": f'"{revision}"',
        },
    )
    assert response.status_code == 200
    after = response.get_json()["receipt"]["after_revision"]
    assert after != revision
    raw = server.lib.load_json(builds_path, {})["legacy-no-revision"]
    assert raw["created_at"] == original["legacy-no-revision"]["created_at"]
    assert raw["updated_at"] == after
    assert client.get(
        "/api/v1/items/legacy-no-revision").get_json()["item"][
            "record_revision"] == after


def test_strict_documents_managed_fields_and_preconditions_never_publish(
    client, command_catalog
):
    _server, builds_path, _original, _reopen_session = command_catalog
    before = builds_path.read_bytes()
    create_headers = {"Idempotency-Key": "invalid-create"}
    update_headers = {
        "Idempotency-Key": "invalid-update",
        "If-Record-Match": '"2026-01-02T00:00:00.000000+00:00"',
    }
    cases = [
        ("post", "/api/v1/items", {"draft": _item()}, create_headers,
         "invalid_item_mutation_envelope"),
        ("post", "/api/v1/items", {"item": {
            **_item(), "unexpected": True}}, create_headers,
         "invalid_item_draft"),
        ("post", "/api/v1/items", {"item": {
            **_item(), "kind": "manuscript"}}, create_headers,
         "unsupported_item_kind"),
        ("post", "/api/v1/items", {"item": _item(
            representations=[{
                "id": "primary", "role": "primary",
                "media_type": "application/pdf", "locator": "urn:test",
                "label": "Source", "metadata": {},
            }])}, create_headers, "representation_mutation_not_supported"),
        ("post", "/api/v1/items", {"item": _item(title=" Padded ")},
         create_headers, "invalid_item_metadata"),
        ("post", "/api/v1/items", {"item": _item(
            metadata={"year": 1600})}, create_headers,
         "invalid_item_metadata"),
        ("post", "/api/v1/items", {"item": _item(
            metadata={"category_ids": ["unknowncat99"]})}, create_headers,
         "invalid_item_metadata"),
        ("post", "/api/v1/items", {"item": _item(
            metadata={"created_at": "client", "authors": "Ada"})},
         create_headers, "managed_item_fields_not_writable"),
        ("patch", "/api/v1/items/book-one", {"patch": _patch(
            representations=[])}, update_headers,
         "representation_mutation_not_supported"),
        ("patch", "/api/v1/items/book-one", {"patch": _patch(
            metadata_set={"status": "ready"})}, update_headers,
         "managed_item_fields_not_writable"),
        ("patch", "/api/v1/items/book-one", {"patch": _patch(
            metadata_remove=["relevance"])}, update_headers,
         "managed_item_fields_not_writable"),
        ("patch", "/api/v1/items/book-one", {"patch": _patch(
            metadata_set={"authors": " Padded "})}, update_headers,
         "invalid_item_metadata"),
    ]
    for method, path, body, headers, code in cases:
        response = getattr(client, method)(path, json=body, headers=headers)
        assert response.status_code == 400
        assert response.get_json()["code"] == code
        assert builds_path.read_bytes() == before

    for value in (None, 'W/"revision"', '"one", "two"', "revision"):
        headers = {"Idempotency-Key": f"precondition-{value}"}
        if value is not None:
            headers["If-Record-Match"] = value
        response = client.patch(
            "/api/v1/items/book-one",
            json={"patch": _patch(title="No publication")},
            headers=headers,
        )
        assert response.status_code == (428 if value is None else 400)
        assert builds_path.read_bytes() == before

    duplicate = client.post(
        "/api/v1/items",
        data=(
            '{"item":{"kind":"book","kind":"book","title":"Book",'
            '"metadata":{},"representations":[]}}'
        ),
        content_type="application/json",
        headers={"Idempotency-Key": "duplicate-json"},
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["code"] == "invalid_item_mutation_document"
    assert builds_path.read_bytes() == before

    oversized = client.post(
        "/api/v1/items",
        data=b"{" + b"x" * (1024 * 1024),
        content_type="application/json",
        headers={"Idempotency-Key": "oversized-json"},
    )
    assert oversized.status_code == 400
    assert oversized.get_json()["code"] == "item_mutation_too_large"
    assert builds_path.read_bytes() == before
    assert not (builds_path.parent / ".engine/receipts/item-commands").exists()


def test_codec_failures_are_sanitized_and_capabilities_match_composition(
    client, command_catalog, monkeypatch
):
    server, builds_path, _original, reopen_session = command_catalog
    document = client.get("/api/v1/capabilities").get_json()
    capabilities = {
        (row["id"], row["version"]) for row in document["capabilities"]
    }
    assert {
        ("library.items.create", 1),
        ("library.items.update", 1),
    } <= capabilities
    assert server._library_engine().item_commands is not None

    def fail_codec(_item_id, _draft, _previous):
        raise RuntimeError(r"C:\private\catalogue-secret")

    monkeypatch.setattr(server, "_engine_item_command_encode", fail_codec)
    reopen_session()
    before = builds_path.read_bytes()
    response = client.post(
        "/api/v1/items", json={"item": _item()},
        headers={"Idempotency-Key": "codec-failure"},
    )
    assert response.status_code == 500
    body = response.get_json()
    assert body["code"] == "item_record_codec_failed"
    assert body["details"] == {"cause_type": "RuntimeError"}
    assert "private" not in response.get_data(as_text=True).lower()
    assert builds_path.read_bytes() == before
