"""End-to-end capture import integration for the lib/3 association service."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
import uuid
import zipfile

import libcommon as lib
import libformat
import pytest
import server
from PIL import Image


@pytest.fixture(autouse=True)
def _isolate_capture_files(monkeypatch, tmp_path):
    """Keep integration records out of the suite-wide compatibility files."""

    workspace = tmp_path / "output"
    workspace.mkdir()
    monkeypatch.setattr(
        lib,
        "MANUAL_ENTRIES_PATH",
        tmp_path / "manual_entries.json",
    )
    monkeypatch.setattr(
        server, "BUILDS_PATH", workspace / "whl_builds.json"
    )
    monkeypatch.setattr(server, "ENTRIES_DIR", workspace / "entries")
    monkeypatch.setattr(server, "CAPTURES_DIR", workspace / "captures")
    monkeypatch.setattr(
        server,
        "CAPTURE_PHONE_SYNC_STATE_PATH",
        tmp_path / "capture_phone_sync_state.json",
    )
    session = server._open_engine_session(workspace)
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
    try:
        yield
    finally:
        session.close()


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


def _jpeg(seed: str, *, width: int = 3, height: int = 2) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    stream = io.BytesIO()
    Image.new("RGB", (width, height), tuple(digest[:3])).save(
        stream,
        format="JPEG",
        quality=92,
    )
    return stream.getvalue()


def _lib2_archive(book_id: str) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "book.json",
            json.dumps({
                "format_version": "2.0",
                "book_id": book_id,
                "source": "primary",
                "pages": [1],
            }),
        )
        archive.writestr(
            "pages/1.json",
            json.dumps({
                "page": 1,
                "doc": "compiled.txt",
                "items": [{
                    "role": "body",
                    "order": 0,
                    "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
                    "text": "Imported text",
                }],
            }),
        )
    return stream.getvalue()


def _prepare_capture(monkeypatch) -> None:
    monkeypatch.setattr(
        server.capture,
        "process_photo",
        lambda raw: raw,
    )
    monkeypatch.setattr(server, "_entry_checks", lambda _entry: {})
    monkeypatch.setattr(server, "activity", lambda *_args, **_kwargs: None)


@pytest.mark.parametrize(
    "capture_id",
    [
        "---",
        "abc/def",
        "abc?def",
        "abc def",
        " abcdef",
        "a" * 65,
        "ABC",
        "abc.",
        "con",
        "nul.capture",
    ],
)
def test_nonportable_capture_id_is_rejected_before_archive_work(
        monkeypatch, capture_id):
    archive_calls = []
    monkeypatch.setattr(
        server,
        "_capture_archive_service",
        lambda: archive_calls.append(True),
    )

    assert server.ingest_capture(
        {"id": capture_id}, [b"photo"], ""
    ) == (None, None)
    assert server._capture_archive_association(capture_id) is None
    assert archive_calls == []


def test_distinct_legacy_capture_ids_keep_underscore_and_period(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_ids = ("legacy_capture_1", "legacy.capture.1")

    results = [
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg(capture_id)],
            "",
            ["photo_1.jpg"],
            transport="lan",
        )
        for capture_id in capture_ids
    ]

    assert all(entry_id and errors == [] for entry_id, errors in results)
    associations = [
        server._capture_archive_association(capture_id)
        for capture_id in capture_ids
    ]
    assert [association.capture_id for association in associations] == list(
        capture_ids
    )
    assert len({association.book_id for association in associations}) == 2


def test_promotion_rejects_nonportable_capture_id_without_aliasing(
        monkeypatch):
    _prepare_capture(monkeypatch)

    refused, error = server._create_build({
        "title": "Unsafe promotion",
        "capture_id": "abc/def",
    })
    assert refused is None
    assert error == "capture_id is not a portable identity"
    assert lib.load_json(server.BUILDS_PATH, {}) == {}

    entry_id, errors = server.ingest_capture(
        _capture("abc_def.v2"),
        [_jpeg("abc_def.v2")],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )
    assert entry_id
    assert errors == []
    build, error = server._create_build({
        "title": "Opaque promotion",
        "capture_id": "abc_def.v2",
    })
    assert error == ""
    assert build is not None
    assert build["capture_id"] == "abc_def.v2"

    with server.app.test_client() as client:
        response = client.patch(
            f"/api/builds/{build['id']}",
            json={"capture_id": "abc/def"},
        )
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_capture_identity"
    stored = lib.load_json(server.BUILDS_PATH, {})[build["id"]]
    assert stored["capture_id"] == "abc_def.v2"


def test_existing_capture_build_uuid5_is_preserved_when_archive_is_sealed(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "legacy-capture-build-id"
    build_id = "legacy-build-123"
    lib.save_json(server.BUILDS_PATH, {
        build_id: {
            "id": build_id,
            "title": "Previously exported capture",
            "capture_id": capture_id,
        },
    })
    historical = "b-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://librarytool.local/items/{build_id}",
    ).hex

    assert server._lib_book_id(build_id) == historical
    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )

    assert entry_id
    assert errors == []
    association = server._capture_archive_association(capture_id)
    assert association is not None
    assert association.book_id == historical
    assert server._lib_book_id(build_id) == historical


def test_existing_capture_build_lib_id_is_preserved_when_archive_is_sealed(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "legacy-capture-persisted-id"
    build_id = "legacy-build-456"
    persisted = "b-0123456789abcdef0123456789abcdef"
    lib.save_json(server.BUILDS_PATH, {
        build_id: {
            "id": build_id,
            "title": "Previously imported capture",
            "capture_id": capture_id,
        },
    })
    lib.save_json(server._lib_id_path(build_id), {"book_id": persisted})

    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )

    assert entry_id
    assert errors == []
    association = server._capture_archive_association(capture_id)
    assert association is not None
    assert association.book_id == persisted
    assert server._lib_book_id(build_id) == persisted


def test_promoted_build_reads_legacy_identity_from_capture_association(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "legacy-capture-associated-id"
    build_id = "promoted-after-backfill"
    preserved = "b-fedcba9876543210fedcba9876543210"
    capture_directory = server.CAPTURES_DIR / capture_id
    capture_directory.mkdir(parents=True)
    (capture_directory / "orig_1.jpg").write_bytes(_jpeg(capture_id))
    (capture_directory / "photo_1.jpg").write_bytes(_jpeg(capture_id))
    association = server._ensure_capture_archive(
        capture_id,
        {
            "id": "manual-associated",
            "capture_id": capture_id,
            "title": "Backfilled Capture",
            "book_id": preserved,
        },
    )
    lib.save_json(server.BUILDS_PATH, {
        build_id: {
            "id": build_id,
            "title": "Promoted after backfill",
            "capture_id": capture_id,
        },
    })

    assert association.book_id == preserved
    assert not server._lib_id_path(build_id).exists()
    assert server._lib_book_id(build_id) == preserved


def test_ingest_seals_complete_legacy_capture_and_promotion_keeps_identity(
        monkeypatch, data_root):
    _prepare_capture(monkeypatch)
    capture_id = "a1111111-1111-4111-8111-111111111111"

    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
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
            [_jpeg(capture_id)],
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
        [_jpeg(capture_id)],
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


def test_manual_capture_metadata_noop_keeps_snapshot_current(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a5555555-5555-4555-8555-555555555555"
    entry_id, _errors = server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
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
            # This is an exact no-op against the committed compatibility row.
            json={"title": "A Capture Herbal"},
        )

    assert response.status_code == 200
    unchanged = server._capture_archive_association(capture_id)
    assert unchanged is not None
    assert unchanged.state.value == "current"
    assert unchanged.book_id == current.book_id
    assert unchanged.archive_sha256 == current.archive_sha256


def test_cloud_acknowledgement_waits_for_archive_commit(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a3333333-3333-4333-8333-333333333333"
    write_set = server._ensure_engine_session().write_set
    publications = []
    owner_cfg = {"url": "owner-cloud", "key": "service"}
    capture_cfg = {"url": "capture-cloud", "key": "user"}
    monkeypatch.setattr(
        server.sbase,
        "download_photo",
        lambda _cfg, _path: _jpeg(capture_id),
    )
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *args, **kwargs: publications.append((args, kwargs)) or {
            "lib_association_revision": 8,
        },
    )

    def fail_receipt(index, _path):
        if index == 2:
            raise RuntimeError("injected receipt publication failure")

    monkeypatch.setattr(write_set, "_publish_hook", fail_receipt)
    capture = {
        **_capture(capture_id),
        "photos": ["phone/photo_1.jpg"],
        "lib_association_revision": 7,
    }
    with pytest.raises(server.EngineRepositoryError):
        server._import_capture(
            owner_cfg,
            capture_cfg,
            capture,
            "",
            False,
        )
    assert publications == []
    assert server._capture_archive_association(capture_id) is None

    monkeypatch.setattr(write_set, "_publish_hook", None)
    result = server._import_capture(
        owner_cfg,
        capture_cfg,
        capture,
        "",
        False,
    )

    assert result["status"] == "skipped"
    association = server._capture_archive_association(capture_id)
    assert result["lib_association"] == association.as_dict()
    assert publications == [(
        (
            owner_cfg,
            capture_cfg,
            capture_id,
            association.as_dict(),
        ),
        {"expected_revision": 7, "mark_imported": True},
    )]


def test_cloud_import_without_owner_keeps_remote_pending_and_photos(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a6666666-6666-4666-8666-666666666666"
    capture_cfg = {"url": "capture-cloud", "key": "user"}
    publications = []
    deletions = []
    monkeypatch.setattr(
        server.sbase,
        "download_photo",
        lambda _cfg, _path: _jpeg(capture_id),
    )
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *args, **kwargs: publications.append((args, kwargs)),
    )
    monkeypatch.setattr(
        server.sbase,
        "delete_photos",
        lambda *args: deletions.append(args),
    )

    result = server._import_capture(
        None,
        capture_cfg,
        {
            **_capture(capture_id),
            "photos": ["phone/photo_1.jpg"],
        },
        "",
        True,
    )

    assert result["status"] == "imported"
    assert "owner service credential is required" in result["sync_error"]
    assert result["lib_association"] == (
        server._capture_archive_association(capture_id).as_dict()
    )
    assert publications == []
    assert deletions == []


def test_cloud_acknowledgement_exact_retry_precedes_photo_cleanup(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a7777777-7777-4777-8777-777777777777"
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    events = []
    monkeypatch.setattr(
        server.sbase,
        "download_photo",
        lambda _cfg, _path: _jpeg(capture_id),
    )

    def publish(*args, **kwargs):
        events.append(("publish", args, kwargs))
        if sum(event[0] == "publish" for event in events) == 1:
            raise server.sbase.SyncError("response lost after commit")
        return {"lib_association_revision": 1}

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )
    monkeypatch.setattr(
        server.sbase,
        "delete_photos",
        lambda cfg, paths: events.append(("delete", cfg, paths)),
    )
    capture = {
        **_capture(capture_id),
        "photos": ["phone/photo_1.jpg"],
    }

    result = server._import_capture(
        owner_cfg,
        capture_cfg,
        capture,
        "",
        True,
    )

    association = server._capture_archive_association(capture_id)
    assert result["status"] == "imported"
    assert [event[0] for event in events] == [
        "publish",
        "publish",
        "delete",
    ]
    first = events[0]
    second = events[1]
    assert first[1:] == second[1:]
    assert first[1] == (
        owner_cfg,
        capture_cfg,
        capture_id,
        association.as_dict(),
    )
    assert first[2] == {
        "expected_revision": 0,
        "mark_imported": True,
    }
    assert events[2] == (
        "delete",
        capture_cfg,
        ["phone/photo_1.jpg"],
    )


def test_lan_import_duplicate_and_stale_return_monotonic_confirmation(
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
                "photo": (io.BytesIO(_jpeg(capture_id)), "photo_1.jpg"),
            },
            content_type="multipart/form-data",
        )

    first = send()
    second = send()
    server._mark_capture_archive_stale(capture_id)
    stale_response = send()

    assert first.status_code == second.status_code == \
        stale_response.status_code == 200
    first_body = first.get_json()
    second_body = second.get_json()
    stale_body = stale_response.get_json()
    assert first_body["status"] == "imported"
    assert second_body["status"] == "duplicate"
    assert second_body["lib_association"] == first_body["lib_association"]
    assert second_body["lib_confirmation"] == first_body["lib_confirmation"]
    assert first_body["lib_association"]["capture_id"] == capture_id
    assert first_body["lib_association"]["book_id"] == (
        server.capture_book_id(capture_id)
    )
    first_confirmation = first_body["lib_confirmation"]
    stale_confirmation = stale_body["lib_confirmation"]
    assert set(first_confirmation) == {
        "schema",
        "version",
        "capture_id",
        "stream_id",
        "revision",
        "updated_at",
        "association",
    }
    assert first_confirmation["revision"] == 1
    assert stale_confirmation["stream_id"] == first_confirmation["stream_id"]
    assert stale_confirmation["revision"] == 2
    assert stale_confirmation["updated_at"] > first_confirmation["updated_at"]
    assert stale_confirmation["association"]["state"] == "stale"
    assert stale_body["lib_association"] == stale_confirmation["association"]
    metadata = server._lan_metadata_exchange({
        "capture_ids": [capture_id],
        "reviews": [],
    })
    assert metadata["associations"] == [stale_confirmation]
    snapshot = server._capture_phone_sync_state()[
        "lan_confirmations"
    ][capture_id]
    assert set(snapshot) == {
        "fingerprint",
        "revision",
        "updated_at",
        "last_seen_at",
    }
    assert "association" not in snapshot
    portable = json.dumps(first_body, sort_keys=True)
    assert str(data_root) not in portable
    assert ".engine" not in portable
    assert "archive_path" not in portable
    assert "local_path" not in portable


def test_lost_lan_ack_after_edit_returns_verified_stale_association(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "lost_ack_after_edit.1"
    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(
        server,
        "_lease_secret",
        lambda _key: contextlib.nullcontext(""),
    )
    client = server.lan_app.test_client()

    def send():
        return client.post(
            "/lan/capture",
            headers={"X-WHL-Token": "paired-secret"},
            data={
                "meta": json.dumps(_capture(capture_id)),
                "photo": (io.BytesIO(_jpeg(capture_id)), "photo_1.jpg"),
            },
            content_type="multipart/form-data",
        )

    imported = send()
    entry_id = imported.get_json()["entry_id"]
    with server.app.test_client() as explorer:
        changed = explorer.patch(
            f"/api/manual/{entry_id}",
            json={"title": "Edited after the lost acknowledgement"},
        )
    assert changed.status_code == 200
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    # A later correction may legitimately remove/change the mutable capture
    # store. The already sealed archive remains the acknowledgement authority.
    (server.CAPTURES_DIR / capture_id / "photo_1.jpg").unlink()

    duplicate = send()

    assert duplicate.status_code == 200
    assert duplicate.get_json()["status"] == "duplicate"
    assert duplicate.get_json()["lib_association"] == stale.as_dict()
    assert duplicate.get_json()["lib_association"]["state"] == "stale"


def test_retry_replaces_precommit_asset_attempt_as_one_generation(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "atomic_asset_retry.1"
    checks = 0

    def fail_first_check(_entry):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise RuntimeError("injected failure before the manual row commit")
        return {}

    monkeypatch.setattr(server, "_entry_checks", fail_first_check)
    with pytest.raises(RuntimeError):
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg("first-1"), _jpeg("first-2")],
            "",
            ["photo_1.jpg", "photo_2.jpg"],
            transport="lan",
        )
    capture_directory = server.CAPTURES_DIR / capture_id
    assert (capture_directory / "orig_2.jpg").is_file()
    assert (capture_directory / "photo_2.jpg").is_file()
    assert not any(
        isinstance(entry, dict) and entry.get("capture_id") == capture_id
        for entry in (
            lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
        ).values()
    )

    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [_jpeg("second-only")],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )

    assert entry_id
    assert errors == []
    assert not (capture_directory / "orig_2.jpg").exists()
    assert not (capture_directory / "photo_2.jpg").exists()
    association = server._capture_archive_association(capture_id)
    archive_path = (
        server._ensure_engine_session().write_set.root
        / ".engine"
        / "capture-lib"
        / "objects"
        / f"{association.archive_sha256}.lib"
    )
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "representations/capture-original-2.jpg" not in names
    assert "representations/capture-display-2.jpg" not in names
    assert not any(
        child.name.startswith((".capture-attempt-", ".capture-orphan-"))
        for child in server.CAPTURES_DIR.iterdir()
    )


def test_corrupt_source_fails_before_row_and_valid_retry_repairs(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "invalid_source_retry.1"
    cap = _capture(capture_id)

    with pytest.raises(ValueError, match="JPEG"):
        server.ingest_capture(
            cap,
            [b"not a jpeg"],
            "",
            ["photo_1.jpg"],
            transport="cloud",
        )

    assert not any(
        isinstance(entry, dict) and entry.get("capture_id") == capture_id
        for entry in (
            lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
        ).values()
    )
    assert server._capture_archive_association(capture_id) is None
    assert not (server.CAPTURES_DIR / capture_id).exists()

    valid = _jpeg("valid retry")
    entry_id, errors = server.ingest_capture(
        cap,
        [valid],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )

    assert entry_id
    assert errors == []
    assert (
        server.CAPTURES_DIR / capture_id / "photo_1.jpg"
    ).read_bytes() == valid
    association = server._capture_archive_association(capture_id)
    assert association is not None
    assert association.state.value == "current"


def test_failed_asset_write_cleans_attempt_and_next_ingest_scavenges_crash(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "asset_attempt_cleanup.1"
    server.CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    abandoned = server.CAPTURES_DIR / (
        ".capture-attempt-" + "a" * 32
    )
    abandoned.mkdir()
    (abandoned / "orig_99.jpg").write_bytes(b"abandoned private bytes")
    write_asset = server._write_capture_asset
    calls = 0

    def fail_second_write(directory, name, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected attempt write failure")
        return write_asset(directory, name, payload)

    monkeypatch.setattr(server, "_write_capture_asset", fail_second_write)
    with pytest.raises(ValueError, match="injected attempt"):
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg(capture_id)],
            "",
            ["photo_1.jpg"],
            transport="lan",
        )

    assert not abandoned.exists()
    assert not (server.CAPTURES_DIR / capture_id).exists()
    assert not any(
        child.name.startswith((".capture-attempt-", ".capture-orphan-"))
        for child in server.CAPTURES_DIR.iterdir()
    )


def test_existing_redirecting_capture_member_is_rejected_without_write(
        monkeypatch, tmp_path):
    _prepare_capture(monkeypatch)
    capture_id = "redirecting_capture_member.1"
    directory = server.CAPTURES_DIR / capture_id
    directory.mkdir(parents=True)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside remains unchanged")
    link = directory / "photo_1.jpg"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="redirecting or invalid"):
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg(capture_id)],
            "",
            ["photo_1.jpg"],
            transport="lan",
        )

    assert outside.read_bytes() == b"outside remains unchanged"
    assert link.is_symlink()
    assert not (directory / "orig_1.jpg").exists()


def test_concurrent_different_captures_ignore_parent_sibling_churn(monkeypatch):
    _prepare_capture(monkeypatch)
    first_id = "concurrent_capture_a.1"
    second_id = ""
    for index in range(1, 100):
        candidate = f"concurrent_capture_b.{index}"
        if (
            server._capture_ingest_lock(candidate)
            is not server._capture_ingest_lock(first_id)
        ):
            second_id = candidate
            break
    assert second_id
    original_read = server.capture_lib_compat._read_regular
    first_read = threading.Event()
    release_first = threading.Event()

    def paused_read(path, *, maximum, artifact):
        if (
            threading.current_thread().name == "first-capture"
            and artifact == "capture original 1"
            and not first_read.is_set()
        ):
            first_read.set()
            assert release_first.wait(5)
        return original_read(path, maximum=maximum, artifact=artifact)

    monkeypatch.setattr(
        server.capture_lib_compat,
        "_read_regular",
        paused_read,
    )
    results = {}
    failures = []

    def ingest(name, capture_id):
        try:
            results[name] = server.ingest_capture(
                _capture(capture_id),
                [_jpeg(capture_id)],
                "",
                ["photo_1.jpg"],
                transport="lan",
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    first = threading.Thread(
        target=ingest,
        args=("first", first_id),
        name="first-capture",
    )
    second = threading.Thread(
        target=ingest,
        args=("second", second_id),
        name="second-capture",
    )
    first.start()
    assert first_read.wait(5)
    second.start()
    second.join(5)
    assert not second.is_alive()
    release_first.set()
    first.join(5)

    assert not first.is_alive()
    assert failures == []
    assert results["first"][0]
    assert results["second"][0]
    assert server._capture_archive_association(first_id) is not None
    assert server._capture_archive_association(second_id) is not None


def _ingest_associated_capture(monkeypatch, capture_id: str):
    _prepare_capture(monkeypatch)
    entry_id, errors = server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )
    assert entry_id
    assert errors == []
    association = server._capture_archive_association(capture_id)
    assert association is not None
    assert association.state.value == "current"
    return entry_id, association


def _canonical_patch(*, title=None, metadata_set=None):
    return {
        "patch": {
            "title": title,
            "metadata_set": metadata_set or {},
            "metadata_remove": [],
            "representations": None,
        },
    }


def test_canonical_item_title_and_metadata_edit_marks_capture_stale(
        monkeypatch):
    capture_id = "canonical_item_edit.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch, capture_id
    )
    build, error = server._create_build({
        "title": "A Capture Herbal",
        "capture_id": capture_id,
    })
    assert error == ""
    assert build is not None

    with server.app.test_client() as client:
        detail = client.get(f"/api/v1/items/{build['id']}")
        revision = detail.get_json()["item"]["record_revision"]
        response = client.patch(
            f"/api/v1/items/{build['id']}",
            json=_canonical_patch(
                title="A Corrected Capture Herbal",
                metadata_set={"notes": "Catalogued after inspection"},
            ),
            headers={
                "Idempotency-Key": "canonical-capture-edit-1",
                "If-Record-Match": f'"{revision}"',
            },
        )

    assert response.status_code == 200
    stored = lib.load_json(server.BUILDS_PATH, {})[build["id"]]
    assert stored["title"] == "A Corrected Capture Herbal"
    assert stored["notes"] == "Catalogued after inspection"
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256


def test_failed_stale_transition_blocks_item_commit_and_same_retry_repairs(
        monkeypatch):
    capture_id = "canonical_stale_retry.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch, capture_id
    )
    build, error = server._create_build({
        "title": "A Capture Herbal",
        "capture_id": capture_id,
    })
    assert error == ""
    assert build is not None
    before_bytes = server.BUILDS_PATH.read_bytes()

    with server.app.test_client() as client:
        detail = client.get(f"/api/v1/items/{build['id']}")
        revision = detail.get_json()["item"]["record_revision"]
        headers = {
            "Idempotency-Key": "canonical-capture-stale-retry-1",
            "If-Record-Match": f'"{revision}"',
        }
        document = _canonical_patch(title="Retry-safe title")
        original_mark_stale = server._mark_capture_archive_stale
        calls = []

        def fail_once(requested_capture_id):
            calls.append(requested_capture_id)
            if len(calls) == 1:
                raise server.EngineRepositoryError(
                    "injected stale transition failure",
                    code="capture_archive_stale_unavailable",
                    retryable=True,
                )
            return original_mark_stale(requested_capture_id)

        monkeypatch.setattr(
            server, "_mark_capture_archive_stale", fail_once
        )
        failed = client.patch(
            f"/api/v1/items/{build['id']}",
            json=document,
            headers=headers,
        )

        assert failed.status_code == 503
        assert failed.get_json()["code"] == (
            "capture_archive_stale_unavailable"
        )
        assert server.BUILDS_PATH.read_bytes() == before_bytes
        assert server._capture_archive_association(
            capture_id
        ) == association

        retried = client.patch(
            f"/api/v1/items/{build['id']}",
            json=document,
            headers=headers,
        )

    assert retried.status_code == 200
    assert retried.get_json()["replayed"] is False
    assert calls == [capture_id, capture_id]
    assert lib.load_json(
        server.BUILDS_PATH, {}
    )[build["id"]]["title"] == "Retry-safe title"
    assert server._capture_archive_association(
        capture_id
    ).state.value == "stale"


def test_matching_promotion_attachment_keeps_verified_archive_current(
        monkeypatch):
    capture_id = "matching_promotion.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch, capture_id
    )
    build, error = server._create_build({"title": "A Capture Herbal"})
    assert error == ""
    assert build is not None

    with server.app.test_client() as client:
        response = client.patch(
            f"/api/builds/{build['id']}",
            json={
                "capture_id": capture_id,
                "expect_updated_at": build["updated_at"],
            },
        )

    assert response.status_code == 200
    promoted = response.get_json()["build"]
    assert promoted["capture_id"] == capture_id
    assert server._lib_book_id(build["id"]) == association.book_id
    assert server._capture_archive_association(
        capture_id
    ).state.value == "current"


@pytest.mark.parametrize("mode", ["create", "attach"])
def test_explicit_empty_promotion_field_marks_capture_archive_stale(
        monkeypatch, mode):
    capture_id = f"explicit_clear_promotion.{mode}"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )

    if mode == "create":
        promoted, error = server._create_build({
            "title": "A Capture Herbal",
            "capture_id": capture_id,
            "images": [],
        })
        assert error == ""
        assert promoted is not None
    else:
        build, error = server._create_build({"title": "A Capture Herbal"})
        assert error == ""
        with server.app.test_client() as client:
            response = client.patch(
                f"/api/builds/{build['id']}",
                json={
                    "capture_id": capture_id,
                    "images": [],
                    "expect_updated_at": build["updated_at"],
                },
            )
        assert response.status_code == 200

    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256


def test_capture_reassignment_and_stored_book_identity_conflict_are_rejected(
        monkeypatch):
    first_id = "capture_reassignment.1"
    second_id = "capture_reassignment.2"
    _entry_id, first = _ingest_associated_capture(monkeypatch, first_id)
    _entry_id, second = _ingest_associated_capture(monkeypatch, second_id)
    linked, error = server._create_build({
        "title": "A Capture Herbal",
        "capture_id": first_id,
    })
    assert error == ""
    assert linked is not None
    unlinked, error = server._create_build({"title": "A Capture Herbal"})
    assert error == ""
    assert unlinked is not None
    conflicting_book_id = "b-" + "f" * 32
    assert conflicting_book_id != second.book_id
    server._lib_store_book_id(unlinked["id"], conflicting_book_id)

    with server.app.test_client() as client:
        reassigned = client.patch(
            f"/api/builds/{linked['id']}",
            json={"capture_id": second_id},
        )
        identity_conflict = client.patch(
            f"/api/builds/{unlinked['id']}",
            json={"capture_id": second_id},
        )

    assert reassigned.status_code == 409
    assert reassigned.get_json()["code"] == "capture_identity_conflict"
    assert identity_conflict.status_code == 409
    assert identity_conflict.get_json()["code"] == (
        "capture_book_identity_conflict"
    )
    builds = lib.load_json(server.BUILDS_PATH, {})
    assert builds[linked["id"]]["capture_id"] == first_id
    assert not builds[unlinked["id"]]["capture_id"]
    assert server._capture_archive_association(first_id) == first
    assert server._capture_archive_association(second_id) == second


def test_native_import_routes_reject_archive_identity_for_promoted_capture(
        monkeypatch):
    capture_id = "native_import_identity.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    build, error = server._create_build({
        "title": "A Capture Herbal",
        "capture_id": capture_id,
    })
    assert error == ""
    foreign_book_id = "b-" + "f" * 32
    assert foreign_book_id != association.book_id
    archive = _lib2_archive(foreign_book_id)

    with server.app.test_client() as client:
        compatibility = client.post(
            f"/api/builds/{build['id']}/replica-import",
            data={"lib": (io.BytesIO(archive), "foreign.lib")},
            content_type="multipart/form-data",
        )
        stable = client.post(
            (
                f"/api/v1/items/{build['id']}/replica/"
                "lib-imports?source_id=primary"
            ),
            headers={"Idempotency-Key": "native-identity-conflict-1"},
            data={"lib": (io.BytesIO(archive), "foreign.lib")},
            content_type="multipart/form-data",
        )

    assert compatibility.status_code == 409
    assert compatibility.get_json()["code"] == "book_identity_mismatch"
    assert stable.status_code == 409
    assert stable.get_json()["code"] == "book_identity_mismatch"
    assert server._lib_stored_book_id(build["id"]) == ""


def test_import_and_capture_promotion_serialize_one_item_identity(monkeypatch):
    capture_id = "import_promotion_race.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    build, error = server._create_build({"title": "A Capture Herbal"})
    assert error == ""
    foreign_book_id = "b-" + "e" * 32
    assert foreign_book_id != association.book_id
    entered_import = threading.Event()
    release_import = threading.Event()
    import_failures = []

    class PausedInterchange:
        def import_lib(self, _command):
            entered_import.set()
            assert release_import.wait(5)
            server._lib_store_book_id(build["id"], foreign_book_id)
            return object()

    monkeypatch.setattr(
        server,
        "_interchange_engine",
        lambda: PausedInterchange(),
    )

    def run_import():
        try:
            server._import_lib_for_item(server.ImportLibCommand(
                item_id=build["id"],
                source_id="primary",
                archive=_lib2_archive(foreign_book_id),
                overwrite=False,
                operation_id="import-promotion-race-1",
            ))
        except Exception as exc:  # pragma: no cover - asserted below
            import_failures.append(exc)

    promotion = {}

    def run_promotion():
        with server.app.test_client() as client:
            response = client.patch(
                f"/api/builds/{build['id']}",
                json={"capture_id": capture_id},
            )
            promotion["status"] = response.status_code
            promotion["body"] = response.get_json()

    importer = threading.Thread(target=run_import, name="identity-import")
    promoter = threading.Thread(target=run_promotion, name="identity-promotion")
    importer.start()
    assert entered_import.wait(5)
    promoter.start()
    promoter.join(0.2)
    assert promoter.is_alive()
    release_import.set()
    importer.join(5)
    promoter.join(5)

    assert import_failures == []
    assert not importer.is_alive() and not promoter.is_alive()
    assert promotion["status"] == 409
    assert promotion["body"]["code"] == "capture_book_identity_conflict"
    stored = lib.load_json(server.BUILDS_PATH, {})[build["id"]]
    assert not stored.get("capture_id")
    assert server._lib_stored_book_id(build["id"]) == foreign_book_id


def test_export_snapshot_serializes_before_capture_promotion(monkeypatch):
    capture_id = "export_promotion_race.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    build, error = server._create_build({"title": "A Capture Herbal"})
    assert error == ""
    layout_path = server._entry_dir(build["id"]) / "ocr" / "layout.json"
    layout_path.parent.mkdir(parents=True, exist_ok=True)
    lib.save_json(layout_path, {
        "regions": {
            "primary": {
                "1": {
                    "doc": "compiled.txt",
                    "dims": {},
                    "state": "",
                    "items": [{
                        "role": "body",
                        "order": 0,
                        "box": {
                            "x": 0.1,
                            "y": 0.1,
                            "w": 0.8,
                            "h": 0.8,
                        },
                        "text": "Exported before promotion",
                    }],
                }
            }
        }
    })
    pre_promotion_book_id = server._lib_book_id(build["id"])
    real_book_id = server._lib_book_id
    export_entered = threading.Event()
    release_export = threading.Event()

    def paused_book_id(item_id):
        if item_id == build["id"] and not export_entered.is_set():
            export_entered.set()
            assert release_export.wait(5)
        return real_book_id(item_id)

    monkeypatch.setattr(server, "_lib_book_id", paused_book_id)
    exported = {}
    promoted = {}

    def run_export():
        with server.app.test_client() as client:
            response = client.get(
                f"/api/builds/{build['id']}/replica-export"
            )
            exported["status"] = response.status_code
            exported["data"] = response.data

    def run_promotion():
        with server.app.test_client() as client:
            response = client.patch(
                f"/api/builds/{build['id']}",
                json={"capture_id": capture_id},
            )
            promoted["status"] = response.status_code

    exporter = threading.Thread(target=run_export, name="identity-export")
    promoter = threading.Thread(target=run_promotion, name="export-promotion")
    exporter.start()
    assert export_entered.wait(5)
    promoter.start()
    promoter.join(0.2)
    assert promoter.is_alive()
    release_export.set()
    exporter.join(5)
    promoter.join(5)

    assert not exporter.is_alive() and not promoter.is_alive()
    assert exported["status"] == 200
    assert promoted["status"] == 200
    with zipfile.ZipFile(io.BytesIO(exported["data"])) as archive:
        exported_book = json.loads(archive.read("book.json"))
    assert exported_book["book_id"] == pre_promotion_book_id
    assert exported_book["book_id"] != association.book_id
    assert server._lib_book_id(build["id"]) == association.book_id


def test_ensure_capture_archive_rejects_changed_durable_source(monkeypatch):
    capture_id = "changed_capture_source.1"
    entry_id, association = _ingest_associated_capture(
        monkeypatch, capture_id
    )
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]
    (server.CAPTURES_DIR / capture_id / "ocr.txt").write_text(
        "OCR changed after the archive was sealed.",
        encoding="utf-8",
    )

    with pytest.raises(server.EngineConflictError) as caught:
        server._ensure_capture_archive(capture_id, entry)

    assert caught.value.code == "capture_archive_reseal_required"
    assert server._capture_archive_association(capture_id) == association


def test_category_remap_marks_linked_capture_archive_stale(monkeypatch):
    capture_id = "category_remap_capture.1"
    entry_id, association = _ingest_associated_capture(
        monkeypatch, capture_id
    )
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    entries[entry_id]["category_ids"] = ["old-category"]
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)

    changed = server._remap_category_ids(
        lambda category_ids: [
            "new-category" if value == "old-category" else value
            for value in category_ids
        ]
    )

    assert changed == 1
    assert lib.load_json(
        lib.MANUAL_ENTRIES_PATH, {}
    )[entry_id]["category_ids"] == ["new-category"]
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256


def test_collection_alias_repoint_marks_linked_capture_archive_stale(
        monkeypatch):
    capture_id = "collection_alias_capture.1"
    entry_id, association = _ingest_associated_capture(
        monkeypatch, capture_id
    )
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    entries[entry_id]["extra"] = {
        **(entries[entry_id].get("extra") or {}),
        "scan_collection_id": "collection-old",
    }
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)

    changed = server._repoint_collection_aliases({
        "collection-old": "collection-new",
    })

    assert changed == 1
    assert lib.load_json(
        lib.MANUAL_ENTRIES_PATH, {}
    )[entry_id]["extra"]["scan_collection_id"] == "collection-new"
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256


def test_lan_rejects_nonportable_id_instead_of_returning_aliased_receipt(
        monkeypatch):
    _prepare_capture(monkeypatch)
    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    client = server.lan_app.test_client()

    response = client.post(
        "/lan/capture",
        headers={"X-WHL-Token": "paired-secret"},
        data={
            "meta": json.dumps(_capture("abc/def")),
            "photo": (io.BytesIO(b"immutable-original"), "photo_1.jpg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "capture id is not a portable identity"
    }
    assert server._capture_archive_association("abcdef") is None
    assert lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) == {}


def test_lan_confirmation_commit_failure_returns_500_and_retry_repairs(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a9999999-9999-4999-8999-999999999999"
    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(
        server,
        "_lease_secret",
        lambda _key: contextlib.nullcontext(""),
    )
    original_save_json = lib.save_json

    def fail_confirmation_state(path, value):
        if path == server.CAPTURE_PHONE_SYNC_STATE_PATH:
            raise OSError("injected confirmation ledger failure")
        return original_save_json(path, value)

    monkeypatch.setattr(lib, "save_json", fail_confirmation_state)
    client = server.lan_app.test_client()

    def send():
        return client.post(
            "/lan/capture",
            headers={"X-WHL-Token": "paired-secret"},
            data={
                "meta": json.dumps(_capture(capture_id)),
                "photo": (
                    io.BytesIO(_jpeg(capture_id)),
                    "photo_1.jpg",
                ),
            },
            content_type="multipart/form-data",
        )

    failed = send()
    assert failed.status_code == 500
    assert server._capture_archive_association(capture_id) is not None
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
    assert sum(
        isinstance(entry, dict) and entry.get("capture_id") == capture_id
        for entry in entries.values()
    ) == 1

    monkeypatch.setattr(lib, "save_json", original_save_json)
    repaired = send()
    assert repaired.status_code == 200
    body = repaired.get_json()
    assert body["status"] == "duplicate"
    assert body["lib_confirmation"]["revision"] == 1


def test_malformed_or_lost_lan_confirmation_ledger_rotates_stream(monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "abbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(
        server,
        "_lease_secret",
        lambda _key: contextlib.nullcontext(""),
    )
    client = server.lan_app.test_client()

    def send():
        return client.post(
            "/lan/capture",
            headers={"X-WHL-Token": "paired-secret"},
            data={
                "meta": json.dumps(_capture(capture_id)),
                "photo": (
                    io.BytesIO(_jpeg(capture_id)),
                    "photo_1.jpg",
                ),
            },
            content_type="multipart/form-data",
        )

    first = send()
    assert first.status_code == 200
    first_confirmation = first.get_json()["lib_confirmation"]
    state = lib.load_json(server.CAPTURE_PHONE_SYNC_STATE_PATH, {})
    state["lan_confirmations"][capture_id]["revision"] = True
    lib.save_json(server.CAPTURE_PHONE_SYNC_STATE_PATH, state)

    replay = send()

    assert replay.status_code == 200
    confirmation = replay.get_json()["lib_confirmation"]
    assert confirmation["stream_id"] != first_confirmation["stream_id"]
    assert confirmation["revision"] == 1
    assert confirmation["association"] == first_confirmation["association"]

    server.CAPTURE_PHONE_SYNC_STATE_PATH.unlink()
    after_loss = send()
    assert after_loss.status_code == 200
    recovered = after_loss.get_json()["lib_confirmation"]
    assert recovered["stream_id"] != confirmation["stream_id"]
    assert recovered["revision"] == 1
