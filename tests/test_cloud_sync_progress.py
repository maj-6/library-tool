"""Detailed, incremental desktop cloud-sync progress."""
from __future__ import annotations

import copy
import contextlib
import hashlib
import io
import threading

import libcommon as lib
import pytest
import server
from PIL import Image


def _jpeg(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    stream = io.BytesIO()
    Image.new("RGB", (2, 1), tuple(digest[:3])).save(stream, format="JPEG")
    return stream.getvalue()


@pytest.fixture(autouse=True)
def _restore_cloudsync_state():
    with server._cloudsync_lock:
        before = copy.deepcopy(server._cloudsync)
    yield
    with server._cloudsync_lock:
        server._cloudsync.clear()
        server._cloudsync.update(before)


def test_import_publishes_durable_book_before_cloud_acknowledgement(
        monkeypatch, tmp_path):
    manual_path = tmp_path / "manual_entries.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(
        server.sbase, "download_photo", lambda _cfg, _path: b"photo")

    def ingest(cap, _photos, _key, _paths, *, transport):
        assert transport == "cloud"
        entry = {
            "id": "book-1", "capture_id": cap["id"],
            "title": "Incremental Herbal",
        }
        lib.save_json(manual_path, {"book-1": entry})
        return "book-1", []

    monkeypatch.setattr(server, "ingest_capture", ingest)
    monkeypatch.setattr(
        server.sbase, "mark_capture",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    seen = []

    result = server._import_capture(
        {"url": "cloud"},
        {"id": "capture-1", "photos": ["one.jpg"]},
        "",
        False,
        on_persisted=lambda item: seen.append((
            item, lib.load_json(manual_path, {}),
        )),
    )

    assert seen[0][0]["book_id"] == "book-1"
    assert seen[0][1]["book-1"]["title"] == "Incremental Herbal"
    assert result["status"] == "imported"
    assert "cloud acknowledgement failed" in result["sync_error"]


def test_zero_photo_cloud_capture_fails_before_ingest_or_acknowledgement(
        monkeypatch, tmp_path):
    manual_path = tmp_path / "manual_entries.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    calls = []
    monkeypatch.setattr(
        server.sbase,
        "download_photo",
        lambda *_args: calls.append("download"),
    )
    monkeypatch.setattr(
        server,
        "ingest_capture",
        lambda *_args, **_kwargs: calls.append("ingest"),
    )
    monkeypatch.setattr(
        server.sbase,
        "mark_capture",
        lambda *_args: calls.append("acknowledge"),
    )

    with pytest.raises(ValueError, match="at least one photo"):
        server._import_capture(
            {"url": "cloud"},
            {"id": "capture-empty", "photos": []},
            "",
            False,
        )

    assert calls == []
    assert lib.load_json(manual_path, {}) in ({}, None)


@pytest.mark.parametrize(
    ("paths", "payloads", "limits", "message", "download_count"),
    [
        (
            ["one.jpg", "two.jpg"],
            [b"a", b"b"],
            {"CAPTURE_MAX_PHOTOS": 1},
            "photo limit",
            0,
        ),
        (
            ["one.jpg"],
            [b""],
            {},
            "photo is empty",
            1,
        ),
        (
            ["one.jpg"],
            [b"abcd"],
            {"CAPTURE_MAX_PHOTO_BYTES": 3},
            "per-photo size limit",
            1,
        ),
        (
            ["one.jpg", "two.jpg"],
            [b"abc", b"def"],
            {
                "CAPTURE_MAX_PHOTO_BYTES": 4,
                "CAPTURE_MAX_TOTAL_PHOTO_BYTES": 5,
            },
            "aggregate size limit",
            2,
        ),
    ],
)
def test_cloud_photo_download_enforces_ingest_envelope(
        monkeypatch, paths, payloads, limits, message, download_count):
    for name, value in limits.items():
        monkeypatch.setattr(server, name, value)
    calls = []

    def download(_cfg, path):
        calls.append(path)
        return payloads[len(calls) - 1]

    monkeypatch.setattr(server.sbase, "download_photo", download)

    with pytest.raises(ValueError, match=message):
        server._download_cloud_capture_photos({"url": "cloud"}, paths)

    assert len(calls) == download_count


def test_supabase_photo_download_reads_only_one_byte_past_bound(monkeypatch):
    reads = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum=None):
            reads.append(maximum)
            return b"x" * int(maximum)

    monkeypatch.setattr(
        server.sbase.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(server.sbase.SyncError, match="download limit"):
        server.sbase.download_photo(
            {"url": "https://example.test", "key": "test"},
            "oversize.jpg",
            maximum_bytes=4,
        )

    assert reads == [5]


def test_supabase_photo_download_caps_http_error_detail(monkeypatch):
    reads = []

    class ErrorBody(io.BytesIO):
        def read(self, maximum=-1):
            reads.append(maximum)
            return super().read(maximum)

    error = server.sbase.urllib.error.HTTPError(
        "https://example.test/storage/photo.jpg",
        413,
        "too large",
        {},
        ErrorBody(b"x" * 1_000),
    )

    def reject(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(server.sbase.urllib.request, "urlopen", reject)

    with pytest.raises(server.sbase.SyncError, match="HTTP 413"):
        server.sbase.download_photo(
            {"url": "https://example.test", "key": "test"},
            "oversize.jpg",
            maximum_bytes=4,
        )

    assert reads == [301]


def test_same_capture_lan_cloud_race_has_one_asset_writer_and_one_row(
        monkeypatch, tmp_path):
    manual_path = tmp_path / "manual_entries.json"
    captures_path = tmp_path / "captures"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures_path)
    monkeypatch.setattr(server, "_entry_checks", lambda _entry: {})
    monkeypatch.setattr(server, "activity", lambda *_args, **_kwargs: None)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def phone_result(_cap, _photos, _paths):
        calls.append(threading.current_thread().name)
        entered.set()
        assert release.wait(2)
        return {
            "photos": [_jpeg("processed")],
            "ocr_text": "",
            "fields": {"title": "Serialized Capture"},
            "extra": {},
            "errors": [],
        }

    monkeypatch.setattr(server, "_phone_result", phone_result)
    cap = {"id": "shared-capture", "photos": ["photo_1.jpg"]}
    results = []
    failures = []

    def ingest(transport):
        try:
            results.append(server.ingest_capture(
                cap, [_jpeg(transport)], "", ["photo_1.jpg"],
                transport=transport,
            ))
        except Exception as exc:  # pragma: no cover - assertion reports detail
            failures.append(exc)

    first = threading.Thread(target=ingest, args=("cloud",), name="cloud")
    first.start()
    assert entered.wait(2)
    second_started = threading.Event()

    def lan_ingest():
        second_started.set()
        ingest("lan")

    second = threading.Thread(target=lan_ingest, name="lan")
    second.start()
    assert second_started.wait(2)
    assert second.is_alive()
    assert calls == ["cloud"]
    release.set()
    first.join(2)
    second.join(2)

    assert not failures
    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    entries = lib.load_json(manual_path, {})
    assert len(entries) == 1
    assert next(iter(entries.values()))["capture_id"] == "shared-capture"
    assert sorted(entry_id is None for entry_id, _errors in results) == [
        False, True,
    ]
    assert (captures_path / "shared-capture" / "photo_1.jpg").read_bytes() == \
        _jpeg("processed")


def test_cloud_run_reports_each_capture_and_compatibility_views(monkeypatch):
    captures = [
        {"id": "cap-one", "title": "One", "photos": ["1.jpg"]},
        {"id": "cap-two", "title": "Two", "photos": []},
        {"id": "cap-three", "title": "Three", "photos": ["3.jpg"]},
    ]
    observed = []
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(server, "_refresh_collection_aliases",
                        lambda *_args: [])
    monkeypatch.setattr(server.sbase, "list_pending_captures",
                        lambda _cfg: captures)
    monkeypatch.setattr(server, "_secret_is_configured", lambda _key: False)

    def import_one(_cfg, cap, _key, _delete, on_persisted=None):
        if cap["id"] == "cap-three":
            raise RuntimeError("bad image")
        result = {
            "status": "imported" if cap["id"] == "cap-one" else "skipped",
            "capture_id": cap["id"],
            "book_id": "book-one" if cap["id"] == "cap-one" else "book-two",
            "title": cap["title"],
            "message": "Book imported" if cap["id"] == "cap-one"
            else "Already present on this desktop",
            "warnings": [],
        }
        if result["status"] == "imported":
            on_persisted(result)
            observed.append(server._cloudsync_snapshot())
        return result

    monkeypatch.setattr(server, "_import_capture", import_one)

    result = server._cloud_sync_run_with_configs(None, {"key": "user"})
    status = server._cloudsync_snapshot()

    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 1
    assert status["stage"] == "failed"
    assert status["progress"] == {
        "phase": "failed",
        "completed": 3,
        "total": 3,
        "unit": "captures",
        "indeterminate": False,
        "capture_completed": 3,
        "capture_total": 3,
        "imported": 1,
        "skipped": 1,
        "failed": 1,
        "current_capture": "",
        "current_book": "",
        "current_index": 0,
        "photo_count": 0,
    }
    assert [event["status"] for event in status["events"]] == [
        "imported", "skipped", "failed",
    ]
    assert status["recent_items"][0]["entry_id"] == "book-one"
    assert status["recent_items"][0]["sequence"] == status["events"][0]["seq"]
    assert observed[0]["events"][0]["book_id"] == "book-one"
    assert observed[0]["current"]["capture_id"] == "cap-one"


def test_owner_phases_become_indeterminate_after_capture_meter(monkeypatch):
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(server, "_refresh_collection_aliases",
                        lambda *_args: [])
    monkeypatch.setattr(
        server.sbase, "list_pending_captures",
        lambda _cfg: [{"id": "cap-one", "title": "One", "photos": []}],
    )
    monkeypatch.setattr(server, "_secret_is_configured", lambda _key: False)

    def import_one(_cfg, cap, _key, _delete, on_persisted=None):
        result = {
            "status": "imported", "capture_id": cap["id"],
            "book_id": "book-one", "title": "One",
            "message": "Book imported", "warnings": [],
        }
        on_persisted(result)
        return result

    monkeypatch.setattr(server, "_import_capture", import_one)
    owner_snapshots = []

    def sync_stores(*_args, **_kwargs):
        owner_snapshots.append(server._cloudsync_snapshot())
        return {"builds": {}}

    monkeypatch.setattr(server.store_sync, "sync_stores", sync_stores)
    monkeypatch.setattr(server, "_books_mirror_rows", lambda: [])
    monkeypatch.setattr(server.sbase, "push_books", lambda *_args: 0)
    monkeypatch.setattr(server, "_sync_capture_reviews",
                        lambda *_args: {"errors": []})
    monkeypatch.setattr(server, "_publish_capture_book_metadata",
                        lambda *_args: 0)
    monkeypatch.setattr(server, "_lease_r2_cfg",
                        lambda: contextlib.nullcontext({}))
    monkeypatch.setattr(server.r2, "configured", lambda _cfg: False)

    result = server._cloud_sync_run_with_configs(
        {"url": "owner"}, {"url": "capture"})

    assert result["ok"] is True
    progress = owner_snapshots[0]["progress"]
    assert progress["phase"] == "owner_stores"
    assert progress["unit"] == "operations"
    assert progress["indeterminate"] is True
    assert progress["completed"] == progress["total"] == 0
    assert progress["capture_completed"] == progress["capture_total"] == 1


def test_manual_entry_get_supports_incremental_merge(client, monkeypatch,
                                                     tmp_path):
    manual_path = tmp_path / "manual_entries.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    lib.save_json(manual_path, {
        "new-book": {"id": "new-book", "title": "Just Arrived"},
    })

    response = client.get("/api/manual/new-book")
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "entry": {"id": "new-book", "title": "Just Arrived"},
    }
    assert client.get("/api/manual/missing").status_code == 404


def test_manual_run_claim_is_atomic_before_thread_spawn(client, monkeypatch):
    started = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(server, "_capture_configured", lambda: True)
    monkeypatch.setattr(server, "_cloud_configured", lambda: False)
    monkeypatch.setattr(server.threading, "Thread", DeferredThread)

    first = client.post("/api/cloudsync/run").get_json()
    second = client.post("/api/cloudsync/run").get_json()

    assert first["started"] is True
    assert second == {
        "ok": True, "started": False, "run_id": first["run_id"],
    }
    assert len(started) == 1
