"""End-to-end capture import integration for the lib/3 association service."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import threading
import uuid
import zipfile

import capture_lib
import libcommon as lib
import libformat
import pytest
import server
from PIL import Image
from pypdf import PdfWriter


@pytest.fixture(autouse=True)
def _isolate_capture_files(monkeypatch, tmp_path):
    """Keep integration records out of the suite-wide compatibility files."""

    workspace = tmp_path / "output"
    workspace.mkdir()
    monkeypatch.setattr(
        lib,
        "MANUAL_ENTRIES_PATH",
        workspace / "manual_entries.json",
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
    monkeypatch.setattr(
        server,
        "CAPTURE_CLOUD_ASSOCIATION_STATE_PATH",
        tmp_path / "capture_cloud_association_state.json",
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


def _accepted_cloud_association(
        association,
        revision: int,
        *,
        status: str = "imported",
) -> dict:
    return {
        "id": association.capture_id,
        "status": status,
        "lib_association": association.as_dict(),
        "lib_association_revision": revision,
        "lib_association_updated_at": "2026-07-23T12:35:00Z",
    }


def _scoped_cloud_association(
        capture_id: str,
        association=None,
        *,
        revision: int = 0,
        status: str = "imported",
) -> dict:
    return {
        "id": capture_id,
        "status": status,
        "lib_association": (
            association.as_dict() if association is not None else None
        ),
        "lib_association_revision": revision,
        "lib_association_updated_at": (
            "2026-07-23T12:35:00Z" if association is not None else None
        ),
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


def test_cloud_ack_replays_when_local_receipt_save_fails_after_rpc_commit(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a8989898-8989-4898-8989-898989898989"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    publications = []
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *args, **kwargs: publications.append((args, kwargs)) or
        _accepted_cloud_association(current, 1),
    )
    remember = server._remember_cloud_capture_association
    attempts = 0

    def fail_first_receipt(association, accepted):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected local receipt write failure")
        remember(association, accepted)

    monkeypatch.setattr(
        server,
        "_remember_cloud_capture_association",
        fail_first_receipt,
    )

    accepted = server._publish_cloud_capture_acknowledgement(
        {"url": "cloud", "key": "service"},
        {"url": "cloud", "key": "user"},
        {"id": capture_id, "lib_association_revision": 0},
        current,
    )

    assert accepted["lib_association_revision"] == 1
    assert len(publications) == 2
    assert publications[0] == publications[1]
    assert server._capture_cloud_association_state()[
        "shadows"
    ][capture_id] == {
        "association": current.as_dict(),
        "revision": 1,
    }


def test_cloud_current_to_stale_queues_offline_and_flushes_association_only(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a9999999-9999-4999-8999-999999999999"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        revision = kwargs["expected_revision"] + 1
        association = server.CaptureArchiveAssociation.from_dict(args[3])
        return _accepted_cloud_association(
            association,
            revision,
            # Association-only publication must accept and preserve any
            # already-authoritative capture status, not force "imported".
            status="error" if not kwargs["mark_imported"] else "imported",
        )

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )
    server._publish_cloud_capture_acknowledgement(
        owner_cfg,
        capture_cfg,
        {"id": capture_id, "lib_association_revision": 0},
        current,
    )
    publications.clear()

    stale = server._mark_capture_archive_stale(capture_id)

    assert stale is not None
    assert stale.state is server.CaptureArchiveState.STALE
    # The catalogue edit only touched local durable state and never attempted
    # to lease or use either cloud credential.
    assert publications == []
    pending = server._pending_cloud_capture_associations()
    assert pending == [(stale, 1)]

    result = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert result == {"pushed": 1, "pending": 0, "errors": []}
    assert publications == [(
        (
            owner_cfg,
            capture_cfg,
            capture_id,
            stale.as_dict(),
        ),
        {"expected_revision": 1, "mark_imported": False},
    )]
    shadow = server._capture_cloud_association_state()[
        "shadows"
    ][capture_id]
    assert shadow == {
        "association": stale.as_dict(),
        "revision": 2,
    }


def test_queued_stale_still_invalidates_old_shadow_after_local_reseal(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a3434343-3434-4434-8434-343434343434"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        desired = server.CaptureArchiveAssociation.from_dict(args[3])
        return _accepted_cloud_association(
            desired,
            kwargs["expected_revision"] + 1,
        )

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )
    server._publish_cloud_capture_acknowledgement(
        owner_cfg,
        capture_cfg,
        {"id": capture_id, "lib_association_revision": 0},
        current,
    )
    stale = server._mark_capture_archive_stale(capture_id)
    publications.clear()
    resealed_values = current.as_dict()
    resealed_values.pop("schema")
    resealed_values.pop("version")
    resealed_values["source_fingerprint"] = "c" * 64
    resealed = server.CaptureArchiveAssociation(**resealed_values)
    monkeypatch.setattr(
        server,
        "_capture_archive_association",
        lambda _capture_id: resealed,
    )

    result = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert result == {"pushed": 1, "pending": 0, "errors": []}
    assert publications[0][0][3] == stale.as_dict()
    assert publications[0][1] == {
        "expected_revision": 1,
        "mark_imported": False,
    }


def test_queued_stale_cancels_only_for_exact_shadow_current_reversion(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a5656565-5656-4656-8656-565656565656"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    calls = []
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *args, **kwargs: calls.append((args, kwargs)) or
        _accepted_cloud_association(current, 1),
    )
    server._publish_cloud_capture_acknowledgement(
        owner_cfg,
        capture_cfg,
        {"id": capture_id, "lib_association_revision": 0},
        current,
    )
    server._mark_capture_archive_stale(capture_id)
    calls.clear()
    monkeypatch.setattr(
        server,
        "_capture_archive_association",
        lambda _capture_id: current,
    )

    result = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert result == {"pushed": 0, "pending": 0, "errors": []}
    assert calls == []
    assert server._capture_cloud_association_state()[
        "shadows"
    ][capture_id]["association"] == current.as_dict()


def test_exact_reversion_cancel_serializes_with_concurrent_stale_mark(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a7878787-7878-4878-8878-787878787878"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    calls = []
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *args, **kwargs: calls.append((args, kwargs)) or
        _accepted_cloud_association(current, 1),
    )
    server._publish_cloud_capture_acknowledgement(
        owner_cfg,
        capture_cfg,
        {"id": capture_id, "lib_association_revision": 0},
        current,
    )
    stale = server._mark_capture_archive_stale(capture_id)
    calls.clear()
    inspection_entered = threading.Event()
    release_inspection = threading.Event()

    def paused_reversion(_capture_id):
        inspection_entered.set()
        assert release_inspection.wait(2)
        return current

    monkeypatch.setattr(
        server,
        "_capture_archive_association",
        paused_reversion,
    )
    flush_results = []
    flush = threading.Thread(
        target=lambda: flush_results.append(
            server._publish_pending_cloud_capture_associations(
                owner_cfg,
                capture_cfg,
            )
        ),
    )
    flush.start()
    assert inspection_entered.wait(2)
    stale_started = threading.Event()

    def mark_again():
        stale_started.set()
        server._mark_capture_archive_stale(capture_id)

    marker = threading.Thread(target=mark_again)
    marker.start()
    assert stale_started.wait(2)
    # Flush still owns the capture stripe while it decides whether the exact
    # current reversion can cancel the old stale intent.
    assert marker.is_alive()
    release_inspection.set()
    flush.join(2)
    marker.join(2)

    assert not flush.is_alive()
    assert not marker.is_alive()
    assert not calls
    assert stale is not None
    # The waiting stale transition ran after cancellation and requeued the
    # same exact shadow invalidation instead of being silently lost.
    assert server._pending_cloud_capture_associations() == [(stale, 1)]
    assert len(flush_results) == 1


def test_damaged_local_association_does_not_block_other_stale_publications(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_ids = (
        "a6767676-6767-4767-8767-676767676767",
        "a6868686-6868-4868-8868-686868686868",
    )
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        desired = server.CaptureArchiveAssociation.from_dict(args[3])
        return _accepted_cloud_association(
            desired,
            kwargs["expected_revision"] + 1,
        )

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )
    stale_by_id = {}
    for capture_id in capture_ids:
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg(capture_id)],
            "",
            ["photo_1.jpg"],
            transport="cloud",
        )
        current = server._capture_archive_association(capture_id)
        server._publish_cloud_capture_acknowledgement(
            owner_cfg,
            capture_cfg,
            {"id": capture_id, "lib_association_revision": 0},
            current,
        )
        stale_by_id[capture_id] = server._mark_capture_archive_stale(
            capture_id
        )
    publications.clear()
    inspect = server._capture_archive_association

    def inspect_one(capture_id):
        if capture_id == capture_ids[0]:
            raise server.EngineRepositoryError("injected damaged association")
        return inspect(capture_id)

    monkeypatch.setattr(
        server,
        "_capture_archive_association",
        inspect_one,
    )

    result = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert result["pushed"] == 1
    assert result["pending"] == 1
    assert len(result["errors"]) == 1
    assert "local inspection failed" in result["errors"][0]
    assert [call[0][2] for call in publications] == [capture_ids[1]]
    assert server._pending_cloud_capture_associations() == [
        (stale_by_id[capture_ids[0]], 1),
    ]


def test_cloud_stale_outbox_requires_exact_accepted_current_authority(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a1212121-1212-4212-8212-121212121212"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="lan",
    )
    current = server._capture_archive_association(capture_id)
    calls = []
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    # A LAN/local capture has no authenticated cloud receipt, so even its exact
    # stale transition cannot authorize a future cloud write.
    stale = server._mark_capture_archive_stale(capture_id)
    assert stale is not None
    assert server._pending_cloud_capture_associations() == []
    assert calls == []

    with pytest.raises(
        server.sbase.SyncError,
        match="verified stale library archive",
    ):
        server._publish_cloud_capture_association_only(
            {"url": "cloud", "key": "service"},
            {"url": "cloud", "key": "user"},
            {"id": capture_id, "lib_association_revision": 1},
            current,
        )
    assert calls == []


def test_cloud_association_state_migrates_once_out_of_version_one_phone_file(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a1313131-1313-4313-8313-131313131313"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    legacy_shadow = {
        "association": current.as_dict(),
        "revision": 4,
    }
    lib.save_json(server.CAPTURE_PHONE_SYNC_STATE_PATH, {
        "version": 1,
        "cloud_association_shadows": {capture_id: legacy_shadow},
        "pending_cloud_associations": {},
        "published_capture_ids": [],
    })

    migrated = server._capture_cloud_association_state()
    server._update_capture_phone_sync_state(
        lambda state: state["published_capture_ids"].append(capture_id)
    )
    rewritten_phone = lib.load_json(
        server.CAPTURE_PHONE_SYNC_STATE_PATH,
        {},
    )
    rewritten_phone["cloud_association_shadows"] = {
        capture_id: {"association": {"corrupt": True}, "revision": 99},
    }
    lib.save_json(server.CAPTURE_PHONE_SYNC_STATE_PATH, rewritten_phone)

    assert migrated["shadows"][capture_id] == legacy_shadow
    assert "cloud_association_shadows" not in (
        server._capture_phone_sync_state()
    )
    # A downgraded/older phone-state writer cannot overwrite the isolated
    # cursor after its one-time migration marker is durable.
    assert server._capture_cloud_association_state()[
        "shadows"
    ][capture_id] == legacy_shadow


def test_installed_imported_stale_archive_bootstraps_shadow_then_publishes(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a1414141-1414-4414-8414-141414141414"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    stale = server._mark_capture_archive_stale(capture_id)
    assert server._pending_cloud_capture_associations() == []
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda cfg, ids, chunk=40: [
            _scoped_cloud_association(
                capture_id,
                current,
                revision=5,
                status="imported",
            )
        ],
    )
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        desired = server.CaptureArchiveAssociation.from_dict(args[3])
        return _accepted_cloud_association(desired, 6)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )

    reconciled = server._reconcile_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
        capture_ids=[capture_id],
    )
    flushed = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert reconciled == {
        "observed": 1,
        "bootstrapped": 1,
        "published": 0,
        "queued": 1,
        "quarantined": 0,
        "errors": [],
    }
    assert stale is not None
    assert publications == [(
        (
            owner_cfg,
            capture_cfg,
            capture_id,
            stale.as_dict(),
        ),
        {"expected_revision": 5, "mark_imported": False},
    )]
    assert flushed == {"pushed": 1, "pending": 0, "errors": []}
    assert server._capture_cloud_association_state()[
        "shadows"
    ][capture_id] == {
        "association": stale.as_dict(),
        "revision": 6,
    }


def test_imported_null_remote_state_bootstraps_without_changing_status(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a1515151-1515-4515-8515-151515151515"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, _ids, chunk=40: [
            _scoped_cloud_association(
                capture_id,
                status="imported",
            )
        ],
    )
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        return _accepted_cloud_association(current, 1)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )

    result = server._reconcile_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
        capture_ids=[capture_id],
    )

    assert result["published"] == 1
    assert result["errors"] == []
    assert publications[0][1] == {
        "expected_revision": 0,
        "mark_imported": False,
    }

    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, _ids, chunk=40: [{
            **_scoped_cloud_association(capture_id, status="imported"),
            "lib_association_revision": 1,
        }],
    )
    publications.clear()
    malformed = server._reconcile_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
        capture_ids=[capture_id],
    )

    assert malformed["published"] == 0
    assert "null scoped state" in malformed["errors"][0]
    assert publications == []


def test_offline_backfill_is_published_by_next_explicit_cloud_sync(
        monkeypatch):
    """The documented operator workflow crosses the real reconciliation path."""

    capture_id = "a2525252-2525-4525-8525-252525252525"
    capture_directory = server.CAPTURES_DIR / capture_id
    capture_directory.mkdir(parents=True)
    (capture_directory / "orig_1.jpg").write_bytes(
        _jpeg(f"{capture_id}-original", width=7, height=11)
    )
    (capture_directory / "photo_1.jpg").write_bytes(
        _jpeg(f"{capture_id}-display", width=5, height=9)
    )
    lib.save_json(lib.MANUAL_ENTRIES_PATH, {
        "manual-backfill": {
            "id": "manual-backfill",
            "capture_id": capture_id,
            "title": "Offline legacy capture",
            "created_at": "2026-07-30T12:00:00+00:00",
        },
    })

    backfill = capture_lib.run_capture_archive_backfill(
        manual_entries_path=lib.MANUAL_ENTRIES_PATH,
        capture_root=server.CAPTURES_DIR,
        workspace_root=server.BUILDS_PATH.parent,
        format_module=libformat,
        apply=True,
    )
    association = server._capture_archive_association(capture_id)

    assert backfill["ok"] is True
    assert backfill["summary"]["created"] == 1
    assert association is not None

    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, ids, chunk=40: [
            _scoped_cloud_association(capture_id, status="imported")
        ] if capture_id in ids else [],
    )
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        desired = server.CaptureArchiveAssociation.from_dict(args[3])
        return _accepted_cloud_association(desired, 1)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )

    reconciled = server._reconcile_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert reconciled["published"] == 1
    assert reconciled["errors"] == []
    assert publications == [(
        (
            owner_cfg,
            capture_cfg,
            capture_id,
            association.as_dict(),
        ),
        {"expected_revision": 0, "mark_imported": False},
    )]
    assert server._capture_cloud_association_state()["shadows"][
        capture_id
    ]["association"] == association.as_dict()


def test_stale_after_durable_ingest_is_first_acknowledged_as_imported(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a1616161-1616-4616-8616-161616161616"
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    monkeypatch.setattr(
        server.sbase,
        "download_photo",
        lambda _cfg, _path: _jpeg(capture_id),
    )
    ingest = server.ingest_capture

    def ingest_then_edit(*args, **kwargs):
        result = ingest(*args, **kwargs)
        server._mark_capture_archive_stale(capture_id)
        return result

    monkeypatch.setattr(server, "ingest_capture", ingest_then_edit)
    publications = []
    deletions = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        stale = server.CaptureArchiveAssociation.from_dict(args[3])
        return _accepted_cloud_association(stale, 1)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )
    monkeypatch.setattr(
        server.sbase,
        "delete_photos",
        lambda *args: deletions.append(args),
    )

    result = server._import_capture(
        owner_cfg,
        capture_cfg,
        {
            **_capture(capture_id),
            "photos": ["phone/photo_1.jpg"],
            "lib_association_revision": 0,
        },
        "",
        True,
    )

    assert result["status"] == "imported"
    assert result["lib_association"]["state"] == "stale"
    assert publications[0][1] == {
        "expected_revision": 0,
        "mark_imported": True,
    }
    assert deletions == []


def test_terminal_stale_conflicts_converge_rebase_or_quarantine(
        monkeypatch):
    _prepare_capture(monkeypatch)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}

    def seed(capture_id):
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg(capture_id)],
            "",
            ["photo_1.jpg"],
            transport="cloud",
        )
        current = server._capture_archive_association(capture_id)
        server._remember_cloud_capture_association(
            current,
            _accepted_cloud_association(current, 1),
        )
        stale = server._mark_capture_archive_stale(capture_id)
        return current, stale

    applied_id = "a1717171-1717-4717-8717-171717171717"
    applied_current, applied_stale = seed(applied_id)
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            server.sbase.SyncError("HTTP 409 on POST rpc: conflict")
        ),
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, ids, chunk=40: [
            _scoped_cloud_association(
                ids[0],
                applied_stale,
                revision=2,
            )
        ],
    )
    applied = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )
    assert applied == {"pushed": 1, "pending": 0, "errors": []}

    rebase_id = "a1818181-1818-4818-8818-181818181818"
    rebase_current, rebase_stale = seed(rebase_id)
    attempts = []

    def rebase_publish(*args, **kwargs):
        attempts.append(kwargs["expected_revision"])
        if len(attempts) <= 2:
            raise server.sbase.SyncError(
                "HTTP 409 on POST rpc: revision changed"
            )
        return _accepted_cloud_association(rebase_stale, 4)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        rebase_publish,
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, ids, chunk=40: [
            _scoped_cloud_association(
                ids[0],
                rebase_current,
                revision=3,
            )
        ],
    )
    rebased = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )
    assert rebased == {"pushed": 1, "pending": 0, "errors": []}
    assert attempts == [1, 1, 3]

    deleted_id = "a1919191-1919-4919-8919-191919191919"
    _deleted_current, _deleted_stale = seed(deleted_id)
    calls = 0

    def deleted_publish(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise server.sbase.SyncError(
            "HTTP 404 on POST rpc: capture missing"
        )

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        deleted_publish,
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, _ids, chunk=40: [],
    )
    quarantined = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )
    repeated = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert quarantined == {"pushed": 0, "pending": 0, "errors": []}
    assert repeated == {"pushed": 0, "pending": 0, "errors": []}
    assert calls == 2
    state = server._capture_cloud_association_state()
    assert deleted_id not in state["pending"]
    assert deleted_id not in state["shadows"]
    assert state["quarantine"][-1]["capture_id"] == deleted_id
    assert "deleted or no longer authorized" in (
        state["quarantine"][-1]["reason"]
    )


def test_authorization_quarantine_is_not_requeued_until_facts_change(
        monkeypatch):
    _prepare_capture(monkeypatch)
    capture_id = "a2020202-2020-4020-8020-202020202020"
    server.ingest_capture(
        _capture(capture_id),
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    current = server._capture_archive_association(capture_id)
    server._remember_cloud_capture_association(
        current,
        _accepted_cloud_association(current, 1),
    )
    stale = server._mark_capture_archive_stale(capture_id)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}
    calls = 0

    def unauthorized(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise server.sbase.SyncError(
            "HTTP 403 on POST rpc: authorization changed"
        )

    remote = _scoped_cloud_association(
        capture_id,
        current,
        revision=1,
    )
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        unauthorized,
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, _ids, chunk=40: [remote],
    )

    first = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )
    reconciled = server._reconcile_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
        capture_ids=[capture_id],
    )
    repeated = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert stale is not None
    assert first == {"pushed": 0, "pending": 0, "errors": []}
    assert reconciled["observed"] == 1
    assert reconciled["errors"] == []
    assert repeated == {"pushed": 0, "pending": 0, "errors": []}
    assert calls == 2
    state = server._capture_cloud_association_state()
    assert state["pending"] == {}
    assert state["shadows"][capture_id] == {
        "association": current.as_dict(),
        "revision": 1,
    }
    assert state["quarantine"][-1]["local"] == stale.as_dict()


def test_missing_capability_rpc_preserves_visible_predecessor_until_rollout(
        monkeypatch):
    _prepare_capture(monkeypatch)
    owner_cfg = {"url": "cloud", "key": "service"}
    capture_cfg = {"url": "cloud", "key": "user"}

    def seed(capture_id):
        server.ingest_capture(
            _capture(capture_id),
            [_jpeg(capture_id)],
            "",
            ["photo_1.jpg"],
            transport="cloud",
        )
        current = server._capture_archive_association(capture_id)
        server._remember_cloud_capture_association(
            current,
            _accepted_cloud_association(current, 1),
        )
        return current, server._mark_capture_archive_stale(capture_id)

    capture_id = "a2121212-2121-4121-8121-212121212121"
    current, stale = seed(capture_id)
    endpoint_available = False
    publications = []

    def publish(*args, **kwargs):
        publications.append((args, kwargs))
        if not endpoint_available:
            raise server.sbase.SyncError(
                "HTTP 404 on POST rpc/prepare_capture_lib_association: "
                "function is missing"
            )
        return _accepted_cloud_association(stale, 2)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_lib_association",
        publish,
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, ids, chunk=40: [
            _scoped_cloud_association(
                ids[0],
                current,
                revision=1,
            )
        ],
    )

    unavailable = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert unavailable["pushed"] == 0
    assert unavailable["pending"] == 1
    assert len(unavailable["errors"]) == 1
    state = server._capture_cloud_association_state()
    assert state["pending"][capture_id] == {
        "association": stale.as_dict(),
        "expected_revision": 1,
    }
    assert state["quarantine"] == []

    endpoint_available = True
    recovered = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert recovered == {"pushed": 1, "pending": 0, "errors": []}
    assert [call[1]["expected_revision"] for call in publications] == [
        1,
        1,
        1,
    ]

    absent_id = "a2222222-2222-4222-8222-222222222222"
    _absent_current, absent_stale = seed(absent_id)
    endpoint_available = False
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, _ids, chunk=40: [],
    )

    absent = server._publish_pending_cloud_capture_associations(
        owner_cfg,
        capture_cfg,
    )

    assert absent == {"pushed": 0, "pending": 0, "errors": []}
    state = server._capture_cloud_association_state()
    assert absent_id not in state["pending"]
    assert absent_id not in state["shadows"]
    assert state["quarantine"][-1]["capture_id"] == absent_id
    assert state["quarantine"][-1]["local"] == absent_stale.as_dict()


def test_cloud_association_quarantine_is_bounded(monkeypatch):
    _prepare_capture(monkeypatch)
    for index in range(
        server._CAPTURE_CLOUD_ASSOCIATION_QUARANTINE_KEEP + 5
    ):
        server._capture_cloud_quarantine_intent(
            f"capture-{index}",
            f"terminal-{index}",
        )

    quarantine = server._capture_cloud_association_state()["quarantine"]
    assert len(quarantine) == (
        server._CAPTURE_CLOUD_ASSOCIATION_QUARANTINE_KEEP
    )
    assert quarantine[0]["capture_id"] == "capture-5"


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


def _capture_promotion_document(
        capture_id: str,
        source_revision: str,
        *,
        title: str = "",
        metadata: dict | None = None,
        primary_source: str = "",
) -> dict:
    return {
        "promotion": {
            "capture_id": capture_id,
            "source_revision": source_revision,
            "item": {
                "kind": "book",
                "title": title,
                "metadata": metadata or {},
                "representations": [],
            },
            "primary_source": primary_source,
        },
    }


def test_capture_promotion_hydrates_corrected_metadata_and_replays(
        monkeypatch, data_root):
    capture_id = "transactional_promotion_metadata.1"
    entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    with server._manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        source = entries[entry_id]
        source.update({
            "title": "Corrected Herbal",
            "author": "Ada Curator",
            "city": "London",
            "notes": "Hand-corrected capture notes",
        })
        source["extra"]["generated"] = {
            "binding": "calf",
            "confidence": 0.94,
        }
        server._save_manual_entries(entries)
        source_revision = (
            server._MANUAL_ENTRY_ITEM_CODEC.record_revision(
                entry_id,
                entries[entry_id],
            )
        )
    document = _capture_promotion_document(
        capture_id,
        source_revision,
        metadata={"pdf_source": "https://example.test/capture.pdf"},
        primary_source=str(
            data_root / "transactional-promotion-source.pdf"
        ),
    )
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with (
        data_root / "transactional-promotion-source.pdf"
    ).open("wb") as stream:
        writer.write(stream)
    headers = {"Idempotency-Key": "transactional-promotion-1"}

    with server.app.test_client() as client:
        created = client.post(
            "/api/v1/capture-promotions",
            json=document,
            headers=headers,
        )
        replayed = client.post(
            "/api/v1/capture-promotions",
            json=document,
            headers=headers,
        )

    assert created.status_code == 201, created.get_json()
    assert replayed.status_code == 200, replayed.get_json()
    assert replayed.get_json()["replayed"] is True
    builds = lib.load_json(server.BUILDS_PATH, {})
    assert len(builds) == 1
    build = next(iter(builds.values()))
    assert build["capture_id"] == capture_id
    assert build["capture_book_id"] == association.book_id
    assert build["title"] == "Corrected Herbal"
    assert build["authors"] == "Ada Curator"
    assert build["publisher_city"] == "London"
    assert build["notes"] == "Hand-corrected capture notes"
    assert build["extra"] == {
        "generated": {
            "binding": "calf",
            "confidence": 0.94,
        },
    }
    assert build["images"]
    assert build["pdf_file"] == str(
        data_root / "transactional-promotion-source.pdf"
    )
    assert "primary" in build["representation_manifest"]["sources"]

    with server.app.test_client() as client:
        detail = client.get(
            f"/api/v1/corrections/items/{association.book_id}"
        )
        item = detail.get_json()["item"]
        assert item["metadata"]["extra"] == build["extra"]
        edited = client.patch(
            f"/api/v1/corrections/items/{association.book_id}",
            json={
                "patch": {
                    "title": None,
                    "metadata_set": {
                        "extra": {
                            "generated": {
                                "binding": "vellum",
                                "confidence": 0.98,
                            },
                        },
                    },
                    "metadata_remove": [],
                },
            },
            headers={
                "Idempotency-Key": "promoted-extra-edit-1",
                "If-Record-Match": (
                    f'"{item["record_revision"]}"'
                ),
            },
        )
    assert edited.status_code == 200, edited.get_json()
    assert lib.load_json(
        server.BUILDS_PATH, {}
    )[build["id"]]["extra"]["generated"]["binding"] == "vellum"


def test_capture_promotion_stale_seed_conflicts_without_orphan_and_retries(
        monkeypatch):
    capture_id = "transactional_promotion_stale_seed.1"
    entry_id, _association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    source = server._capture_manual_source_snapshot(capture_id)
    assert source is not None
    stale_revision = source.record_revision
    with server._manual_lock:
        entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
        entries[entry_id]["title"] = "Edited after the upload-list seed"
        entries[entry_id]["author"] = "Latest Curator"
        server._save_manual_entries(entries)
        current_revision = (
            server._MANUAL_ENTRY_ITEM_CODEC.record_revision(
                entry_id,
                entries[entry_id],
            )
        )

    with server.app.test_client() as client:
        stale = client.post(
            "/api/v1/capture-promotions",
            json=_capture_promotion_document(
                capture_id,
                stale_revision,
                title="Old upload-list title",
            ),
            headers={"Idempotency-Key": "stale-seed-promotion"},
        )
        assert lib.load_json(server.BUILDS_PATH, {}) == {}
        retried = client.post(
            "/api/v1/capture-promotions",
            json=_capture_promotion_document(
                capture_id,
                current_revision,
            ),
            headers={"Idempotency-Key": "stale-seed-promotion"},
        )

    assert stale.status_code == 409
    assert stale.get_json()["code"] == "capture_source_revision_conflict"
    assert retried.status_code == 201, retried.get_json()
    build = next(iter(lib.load_json(server.BUILDS_PATH, {}).values()))
    assert build["title"] == "Edited after the upload-list seed"
    assert build["authors"] == "Latest Curator"


def test_capture_promotion_source_failure_leaves_no_orphan(monkeypatch):
    capture_id = "transactional_promotion_bad_source.1"
    _entry_id, _association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    source = server._capture_manual_source_snapshot(capture_id)
    assert source is not None

    with server.app.test_client() as client:
        response = client.post(
            "/api/v1/capture-promotions",
            json=_capture_promotion_document(
                capture_id,
                source.record_revision,
                primary_source="missing/capture-source.pdf",
            ),
            headers={"Idempotency-Key": "bad-source-promotion"},
        )

    assert response.status_code == 400
    assert response.get_json()["code"] == "representation_source_not_found"
    assert lib.load_json(server.BUILDS_PATH, {}) == {}


def test_capture_promotion_invalidation_failure_leaves_no_build_and_new_operation_retries(
        monkeypatch):
    capture_id = "transactional_promotion_replay_gate.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    source = server._capture_manual_source_snapshot(capture_id)
    assert source is not None
    real_mark_stale = server._mark_capture_archive_stale
    calls = []

    def fail_once(requested_capture_id):
        calls.append(requested_capture_id)
        if len(calls) == 1:
            raise server.EngineRepositoryError(
                "injected pre-commit invalidation failure",
                code="capture_archive_stale_unavailable",
                retryable=True,
            )
        return real_mark_stale(requested_capture_id)

    monkeypatch.setattr(server, "_mark_capture_archive_stale", fail_once)
    document = _capture_promotion_document(
        capture_id,
        source.record_revision,
        metadata={"source_url": "https://example.test/new-source"},
    )

    with server.app.test_client() as client:
        failed = client.post(
            "/api/v1/capture-promotions",
            json=document,
            headers={"Idempotency-Key": "promotion-failed-gate"},
        )
        assert lib.load_json(server.BUILDS_PATH, {}) == {}
        assert server._capture_archive_association(
            capture_id
        ).state.value == "current"
        retried = client.post(
            "/api/v1/capture-promotions",
            json=document,
            headers={"Idempotency-Key": "promotion-browser-reload"},
        )

    assert failed.status_code == 503
    assert failed.get_json()["code"] == (
        "capture_archive_stale_unavailable"
    )
    assert retried.status_code == 201, retried.get_json()
    assert retried.get_json()["replayed"] is False
    assert len(lib.load_json(server.BUILDS_PATH, {})) == 1
    assert calls == [capture_id, capture_id]
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256


@pytest.mark.parametrize("source_change", ["delete", "edit"])
def test_capture_promotion_replay_marks_archive_stale_when_source_pin_is_lost(
        monkeypatch, source_change):
    capture_id = f"promotion_replay_lost_source_pin.{source_change}"
    entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    source = server._capture_manual_source_snapshot(capture_id)
    assert source is not None
    document = _capture_promotion_document(
        capture_id,
        source.record_revision,
    )
    headers = {"Idempotency-Key": f"lost-source-pin-{source_change}"}

    with server.app.test_client() as client:
        created = client.post(
            "/api/v1/capture-promotions",
            json=document,
            headers=headers,
        )
        assert created.status_code == 201, created.get_json()
        assert server._capture_archive_association(
            capture_id
        ).state.value == "current"

        with server._manual_lock:
            entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
            if source_change == "delete":
                del entries[entry_id]
            else:
                entries[entry_id]["notes"] = (
                    "Changed after the source-pinned promotion"
                )
            server._save_manual_entries(entries)

        replayed = client.post(
            "/api/v1/capture-promotions",
            json=document,
            headers=headers,
        )

    assert replayed.status_code == 200, replayed.get_json()
    assert replayed.get_json()["replayed"] is True
    assert len(lib.load_json(server.BUILDS_PATH, {})) == 1
    stale = server._capture_archive_association(capture_id)
    assert stale is not None
    assert stale.state.value == "stale"
    assert stale.archive_sha256 == association.archive_sha256


def test_concurrent_capture_promotions_publish_only_one_build(monkeypatch):
    capture_id = "transactional_promotion_race.1"
    _entry_id, _association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    source = server._capture_manual_source_snapshot(capture_id)
    assert source is not None
    entered = threading.Event()
    release = threading.Event()
    original = server._capture_promotion_draft

    def paused_draft(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "_capture_promotion_draft", paused_draft)
    statuses = {}

    def promote(name):
        with server.app.test_client() as client:
            response = client.post(
                "/api/v1/capture-promotions",
                json=_capture_promotion_document(
                    capture_id,
                    source.record_revision,
                ),
                headers={
                    "Idempotency-Key": f"promotion-race-{name}",
                },
            )
            statuses[name] = (
                response.status_code,
                response.get_json(),
            )

    first = threading.Thread(target=promote, args=("first",))
    second = threading.Thread(target=promote, args=("second",))
    first.start()
    assert entered.wait(5)
    second.start()
    second.join(0.2)
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert sorted(status for status, _body in statuses.values()) == [
        201,
        409,
    ]
    conflict = next(
        body for status, body in statuses.values() if status == 409
    )
    assert conflict["code"] == "capture_build_conflict"
    assert len(lib.load_json(server.BUILDS_PATH, {})) == 1


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


def test_create_promotion_rejects_a_concurrent_capture_metadata_edit(
        monkeypatch):
    capture_id = "create_promotion_metadata_race.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    entered = threading.Event()
    release = threading.Event()
    original_compare = server._capture_promotion_changes_source

    def paused_compare(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_compare(*args, **kwargs)

    monkeypatch.setattr(
        server,
        "_capture_promotion_changes_source",
        paused_compare,
    )
    promoted = {}

    def run_promotion():
        promoted["result"] = server._create_build({
            "title": "A Capture Herbal",
            "capture_id": capture_id,
        })

    worker = threading.Thread(target=run_promotion)
    worker.start()
    assert entered.wait(5)
    with server.app.test_client() as client:
        before = client.get(
            f"/api/v1/corrections/items/{association.book_id}"
        ).get_json()["item"]
        edited = client.patch(
            f"/api/v1/corrections/items/{association.book_id}",
            json={
                "patch": {
                    "title": "Human correction during promotion",
                    "metadata_set": {"authors": "Current cataloguer"},
                    "metadata_remove": [],
                }
            },
            headers={
                "Idempotency-Key": "create-promotion-metadata-race",
                "If-Record-Match": (
                    f'"{before["record_revision"]}"'
                ),
            },
        )
    assert edited.status_code == 200, edited.get_json()
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    build, error = promoted["result"]
    assert build is None
    assert error == "capture source changed elsewhere"
    assert lib.load_json(server.BUILDS_PATH, {}) == {}
    after = server._capture_manual_source(capture_id)
    assert after is not None
    assert after["title"] == "Human correction during promotion"
    assert after["author"] == "Current cataloguer"


def test_attach_promotion_rejects_a_concurrent_capture_metadata_edit(
        monkeypatch):
    capture_id = "attach_promotion_metadata_race.1"
    _entry_id, association = _ingest_associated_capture(
        monkeypatch,
        capture_id,
    )
    build, error = server._create_build({"title": "A Capture Herbal"})
    assert error == ""
    assert build is not None
    entered = threading.Event()
    release = threading.Event()
    original_compare = server._capture_promotion_changes_source

    def paused_compare(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_compare(*args, **kwargs)

    monkeypatch.setattr(
        server,
        "_capture_promotion_changes_source",
        paused_compare,
    )
    promoted = {}

    def run_promotion():
        with server.app.test_client() as client:
            response = client.patch(
                f"/api/builds/{build['id']}",
                json={
                    "capture_id": capture_id,
                    "expect_updated_at": build["updated_at"],
                },
            )
            promoted["status"] = response.status_code
            promoted["body"] = response.get_json()

    worker = threading.Thread(target=run_promotion)
    worker.start()
    assert entered.wait(5)
    with server.app.test_client() as client:
        before = client.get(
            f"/api/v1/corrections/items/{association.book_id}"
        ).get_json()["item"]
        edited = client.patch(
            f"/api/v1/corrections/items/{association.book_id}",
            json={
                "patch": {
                    "title": "Human correction during attachment",
                    "metadata_set": {"authors": "Current cataloguer"},
                    "metadata_remove": [],
                }
            },
            headers={
                "Idempotency-Key": "attach-promotion-metadata-race",
                "If-Record-Match": (
                    f'"{before["record_revision"]}"'
                ),
            },
        )
    assert edited.status_code == 200, edited.get_json()
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert promoted["status"] == 409
    assert promoted["body"]["code"] == "capture_source_revision_conflict"
    stored = lib.load_json(server.BUILDS_PATH, {})[build["id"]]
    assert not stored.get("capture_id")
    after = server._capture_manual_source(capture_id)
    assert after is not None
    assert after["title"] == "Human correction during attachment"
    assert after["author"] == "Current cataloguer"


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
