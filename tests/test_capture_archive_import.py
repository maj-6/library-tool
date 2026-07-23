"""End-to-end capture import integration for the lib/3 association service."""

from __future__ import annotations

import contextlib
import io
import json
import zipfile

import libcommon as lib
import libformat
import pytest
import server


@pytest.fixture(autouse=True)
def _isolate_capture_files(monkeypatch, tmp_path):
    """Keep integration records out of the suite-wide compatibility files."""

    monkeypatch.setattr(
        lib,
        "MANUAL_ENTRIES_PATH",
        tmp_path / "manual_entries.json",
    )
    monkeypatch.setattr(server, "BUILDS_PATH", tmp_path / "builds.json")
    monkeypatch.setattr(server, "CAPTURES_DIR", tmp_path / "captures")


def _capture(capture_id: str) -> dict:
    return {
        "id": capture_id,
        "ocr": {"photo_1.jpg": "Garden sage and rosemary."},
        "meta": {
            "title": "A Capture Herbal",
            "scan_collection": "Green crate",
            "scan_from": "Archive room",
        },
    }


def _prepare_capture(monkeypatch) -> None:
    monkeypatch.setattr(
        server.capture,
        "process_photo",
        lambda raw: b"display-" + raw,
    )
    monkeypatch.setattr(server, "_entry_checks", lambda _entry: {})
    monkeypatch.setattr(server, "activity", lambda *_args, **_kwargs: None)


def test_ingest_seals_complete_legacy_capture_and_promotion_keeps_identity(
        monkeypatch, data_root):
    _prepare_capture(monkeypatch)
    capture_id = "a1111111-1111-4111-8111-111111111111"

    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [b"immutable-original"],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )

    assert entry_id
    assert errors == []
    association = server._capture_archive_association(capture_id)
    assert association is not None
    archive_path = (
        server._ensure_engine_session().write_set.root
        / ".engine"
        / "capture-lib"
        / "objects"
        / f"{association.archive_sha256}.lib"
    )
    assert archive_path.is_file()
    assert archive_path.stat().st_size == association.archive_bytes

    opened = libformat.read_lib(archive_path)
    assert [
        issue.as_dict()
        for issue in libformat.validate(opened)
        if issue.level == "error"
    ] == []
    assert opened.book_id == association.book_id
    assert {record.role for record in opened.representations} == {
        "capture-original",
        "capture-display",
    }
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert {
            "representations/capture-original-1.jpg",
            "representations/capture-display-1.jpg",
            "artifacts/photo-assets.json",
            "artifacts/generated-metadata.json",
            "artifacts/geometry.json",
            "artifacts/capture-notes.json",
            "artifacts/capture-provenance.json",
            "artifacts/ocr.txt",
        } <= names
        photo_assets = json.loads(
            archive.read("artifacts/photo-assets.json")
        )
        assert photo_assets["legacy_fallback"] is True
        assert photo_assets["capture_id"] == capture_id

    build, error = server._create_build({
        "title": "Promoted Capture Herbal",
        "capture_id": capture_id,
    })
    assert error == ""
    assert build is not None
    assert server._lib_book_id(build["id"]) == association.book_id
    with server.app.test_client() as client:
        edited = client.patch(
            f"/api/builds/{build['id']}",
            json={"title": "Corrected Promoted Herbal"},
        )
    assert edited.status_code == 200
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256
    assert server._lib_book_id(build["id"]) == association.book_id

    portable = json.dumps(association.as_dict(), sort_keys=True)
    assert str(data_root) not in portable
    assert ".engine" not in portable
    assert "archive_path" not in portable
    assert "local_path" not in portable


def test_archive_publication_failure_leaves_no_success_and_retry_heals(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a2222222-2222-4222-8222-222222222222"
    write_set = server._ensure_engine_session().write_set
    object_root = write_set.root / ".engine" / "capture-lib" / "objects"
    objects_before = set(object_root.glob("*.lib"))

    def fail_association(index, _path):
        if index == 1:
            raise RuntimeError("injected archive publication failure")

    monkeypatch.setattr(write_set, "_publish_hook", fail_association)
    with pytest.raises(server.EngineRepositoryError):
        server.ingest_capture(
            _capture(capture_id),
            [b"immutable-original"],
            "",
            ["photo_1.jpg"],
            transport="cloud",
        )

    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
    matching = [
        entry
        for entry in entries.values()
        if isinstance(entry, dict) and entry.get("capture_id") == capture_id
    ]
    assert len(matching) == 1
    assert server._capture_archive_association(capture_id) is None
    assert set(object_root.glob("*.lib")) == objects_before

    monkeypatch.setattr(write_set, "_publish_hook", None)
    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [b"immutable-original"],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )

    assert entry_id is None
    assert errors is None
    association = server._capture_archive_association(capture_id)
    assert association is not None
    assert association.book_id == server.capture_book_id(capture_id)
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
    assert sum(
        isinstance(entry, dict) and entry.get("capture_id") == capture_id
        for entry in entries.values()
    ) == 1


def test_manual_capture_metadata_retry_marks_snapshot_stale(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a5555555-5555-4555-8555-555555555555"
    entry_id, _errors = server.ingest_capture(
        _capture(capture_id),
        [b"immutable-original"],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )
    current = server._capture_archive_association(capture_id)
    assert current is not None
    assert current.state.value == "current"

    with server.app.test_client() as client:
        response = client.patch(
            f"/api/manual/{entry_id}",
            # The compatibility row already contains this value. Replaying the
            # canonical update must still repair a prior stale-publication
            # failure rather than treating the request as an irrelevant no-op.
            json={"title": "A Capture Herbal"},
        )

    assert response.status_code == 200
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.book_id == current.book_id
    assert stale.archive_sha256 == current.archive_sha256


def test_cloud_acknowledgement_waits_for_archive_commit(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a3333333-3333-4333-8333-333333333333"
    write_set = server._ensure_engine_session().write_set
    marks = []
    monkeypatch.setattr(
        server.sbase,
        "download_photo",
        lambda _cfg, _path: b"immutable-original",
    )
    monkeypatch.setattr(
        server.sbase,
        "mark_capture",
        lambda _cfg, remote_id, status: marks.append((remote_id, status)),
    )

    def fail_receipt(index, _path):
        if index == 2:
            raise RuntimeError("injected receipt publication failure")

    monkeypatch.setattr(write_set, "_publish_hook", fail_receipt)
    capture = {
        **_capture(capture_id),
        "photos": ["phone/photo_1.jpg"],
    }
    with pytest.raises(server.EngineRepositoryError):
        server._import_capture(
            {"url": "capture-cloud"},
            capture,
            "",
            False,
        )
    assert marks == []
    assert server._capture_archive_association(capture_id) is None

    monkeypatch.setattr(write_set, "_publish_hook", None)
    result = server._import_capture(
        {"url": "capture-cloud"},
        capture,
        "",
        False,
    )

    assert result["status"] == "skipped"
    assert marks == [(capture_id, "imported")]
    assert result["lib_association"] == (
        server._capture_archive_association(capture_id).as_dict()
    )


def test_lan_import_and_duplicate_return_same_portable_association(
        monkeypatch, data_root):
    _prepare_capture(monkeypatch)
    capture_id = "a4444444-4444-4444-8444-444444444444"
    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(
        server,
        "_lease_secret",
        lambda key: (
            contextlib.nullcontext("")
            if key == "mistralKey"
            else contextlib.nullcontext("")
        ),
    )
    client = server.lan_app.test_client()

    def send():
        return client.post(
            "/lan/capture",
            headers={"X-WHL-Token": "paired-secret"},
            data={
                "meta": json.dumps(_capture(capture_id)),
                "photo": (io.BytesIO(b"immutable-original"), "photo_1.jpg"),
            },
            content_type="multipart/form-data",
        )

    first = send()
    second = send()

    assert first.status_code == second.status_code == 200
    first_body = first.get_json()
    second_body = second.get_json()
    assert first_body["status"] == "imported"
    assert second_body["status"] == "duplicate"
    assert second_body["lib_association"] == first_body["lib_association"]
    assert first_body["lib_association"]["capture_id"] == capture_id
    assert first_body["lib_association"]["book_id"] == (
        server.capture_book_id(capture_id)
    )
    portable = json.dumps(first_body, sort_keys=True)
    assert str(data_root) not in portable
    assert ".engine" not in portable
    assert "archive_path" not in portable
    assert "local_path" not in portable
