from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs

import pytest

import server
import supabase_sync


OWNER_ID = "11111111-2222-3333-4444-555555555555"
CAPTURE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
QUEUE_ID = "12345678-1234-4234-8234-1234567890ab"
SCAN_COLLECTION_ID = "99999999-8888-4777-8666-555555555555"
SOURCE_COLLECTION_ID = "77777777-6666-4555-8444-333333333333"
STAMP = "2026-08-21T12:00:00+00:00"


def _jwt(subject: str = OWNER_ID, role: str = "authenticated") -> str:
    def encoded(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encoded({'alg': 'none'})}.{encoded({'sub': subject, 'role': role})}.sig"


def _user_cfg() -> dict:
    return {
        "url": "https://project.test",
        "key": "sb_publishable_test",
        "access_token": _jwt(),
    }


def _scan_state_row(*, active=True, revision=1) -> dict:
    return {
        "capture_id": CAPTURE_ID,
        "owner_id": OWNER_ID,
        "scan_collection_id": SCAN_COLLECTION_ID,
        "source_collection_id": SOURCE_COLLECTION_ID,
        "active": active,
        "revision": revision,
        "marked_at": STAMP,
        "updated_at": STAMP,
    }


def _queue_row(*, status="pending", matched_capture_id=None, revision=1) -> dict:
    return {
        "id": QUEUE_ID,
        "owner_id": OWNER_ID,
        "scan_collection_id": SCAN_COLLECTION_ID,
        "photo_role": "cover",
        "ocr_text": "A captured herbal",
        "status": status,
        "matched_capture_id": matched_capture_id,
        "revision": revision,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def test_capture_scan_state_owner_read_is_bounded_and_scoped(monkeypatch):
    calls = []

    def rest(_cfg, method, path, *_args, **_kwargs):
        calls.append((method, path))
        query = parse_qs(path.partition("?")[2])
        return [_scan_state_row()] if query.get("offset") == ["0"] else []

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    rows = supabase_sync.list_capture_scan_state(
        _user_cfg(), page_size=1, maximum_rows=2,
    )

    assert rows == [_scan_state_row()]
    assert len(calls) == 2
    assert all(f"owner_id=eq.{OWNER_ID}" in path for _method, path in calls)
    assert all("order=capture_id.asc" in path for _method, path in calls)


def test_capture_scan_state_service_read_requires_capture_scope(monkeypatch):
    with pytest.raises(supabase_sync.SyncError, match="capture ids"):
        supabase_sync.list_capture_scan_state({
            "url": "https://project.test", "key": "sb_secret_test",
        })

    paths = []
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda _cfg, _method, path, *_args, **_kwargs:
            paths.append(path) or [_scan_state_row()],
    )
    rows = supabase_sync.list_capture_scan_state(
        {"url": "https://project.test", "key": "sb_secret_test"},
        [CAPTURE_ID, "not-a-uuid"],
    )
    assert rows[0]["capture_id"] == CAPTURE_ID
    assert f"capture_id=in.({CAPTURE_ID})" in paths[0]


def test_scan_queue_user_reads_and_rpc_writes_are_exact(monkeypatch):
    calls = []

    def rest(_cfg, method, path, payload=None, **_kwargs):
        calls.append((method, path, payload))
        if method == "GET":
            query = parse_qs(path.partition("?")[2])
            return [_queue_row()] if query.get("offset") == ["0"] else []
        if path == "rpc/enqueue_scan_search":
            return [_queue_row()]
        if path == "rpc/complete_scan_search":
            return [_queue_row(
                status="matched", matched_capture_id=CAPTURE_ID, revision=2,
            )]
        raise AssertionError(path)

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    cfg = _user_cfg()

    assert supabase_sync.list_scan_search_queue(
        cfg, page_size=1, maximum_rows=2,
    ) == [_queue_row()]
    assert supabase_sync.enqueue_scan_search(
        cfg, QUEUE_ID, SCAN_COLLECTION_ID, "cover", "A captured herbal",
    )["status"] == "pending"
    assert supabase_sync.complete_scan_search(
        cfg, QUEUE_ID, CAPTURE_ID,
    )["matched_capture_id"] == CAPTURE_ID

    assert f"owner_id=eq.{OWNER_ID}" in calls[0][1]
    assert "status=in.(pending)" in calls[0][1]
    assert calls[-2] == ("POST", "rpc/enqueue_scan_search", {
        "p_id": QUEUE_ID,
        "p_scan_collection_id": SCAN_COLLECTION_ID,
        "p_photo_role": "cover",
        "p_ocr_text": "A captured herbal",
    })
    assert calls[-1] == ("POST", "rpc/complete_scan_search", {
        "p_id": QUEUE_ID, "p_capture_id": CAPTURE_ID,
    })


def test_scan_queue_rejects_service_credentials_before_request(monkeypatch):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: pytest.fail("service key must not reach queue RPC"),
    )
    with pytest.raises(supabase_sync.SyncError, match="signed-in user"):
        supabase_sync.enqueue_scan_search(
            {"url": "https://project.test", "key": "sb_secret_test"},
            QUEUE_ID, SCAN_COLLECTION_ID, "cover", "Text",
        )


def _server_signed_in(monkeypatch):
    monkeypatch.setattr(server, "_auth_cfg", lambda: {
        "url": "https://project.test", "key": "sb_publishable_test",
    })
    monkeypatch.setattr(server, "_auth_session", lambda: {
        "access_token": _jwt(), "user_id": OWNER_ID,
    })


def test_desktop_scan_queue_routes_use_user_scope(client, monkeypatch):
    _server_signed_in(monkeypatch)
    configs = []
    monkeypatch.setattr(
        server.sbase,
        "list_scan_search_queue",
        lambda cfg, **_kwargs: configs.append(dict(cfg)) or [_queue_row()],
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_scan_state",
        lambda cfg: configs.append(dict(cfg)) or [_scan_state_row()],
    )
    enqueues = []
    monkeypatch.setattr(
        server.sbase,
        "enqueue_scan_search",
        lambda cfg, queue_id, collection_id, photo_role, ocr_text:
            enqueues.append((dict(cfg), queue_id, collection_id, photo_role, ocr_text))
            or _queue_row(),
    )
    completions = []
    monkeypatch.setattr(
        server.sbase,
        "complete_scan_search",
        lambda cfg, queue_id, capture_id:
            completions.append((dict(cfg), queue_id, capture_id)) or
            _queue_row(status="matched", matched_capture_id=CAPTURE_ID, revision=2),
    )

    queue = client.get("/api/scan/search-queue").get_json()
    states = client.get("/api/scan/state").get_json()
    enqueued = client.post("/api/scan/search-queue", json={
        "id": QUEUE_ID,
        "scan_collection_id": SCAN_COLLECTION_ID,
        "photo_role": "cover",
        "ocr_text": "A captured herbal",
    })
    completed = client.post(
        f"/api/scan/search-queue/{QUEUE_ID}/complete",
        json={"capture_id": CAPTURE_ID},
    )

    assert queue["queue"][0]["id"] == QUEUE_ID
    assert states["states"][0]["active"] is True
    assert enqueued.status_code == 200
    assert enqueues[0][1:] == (
        QUEUE_ID, SCAN_COLLECTION_ID, "cover", "A captured herbal",
    )
    assert completed.status_code == 200
    assert completions[0][1:] == (QUEUE_ID, CAPTURE_ID)
    assert all(cfg["access_token"] == _jwt() for cfg in configs)
    assert completions[0][0]["access_token"] == _jwt()
    assert enqueues[0][0]["access_token"] == _jwt()


def test_collection_type_is_created_returned_and_immutable(client, monkeypatch):
    _server_signed_in(monkeypatch)
    calls = []

    def rest(_cfg, _token, method, path, body=None, **_kwargs):
        calls.append((method, path, body))
        if method == "POST":
            return [{
                **body[0], "updated_at": STAMP, "merged_into": None,
            }]
        raise AssertionError("immutable type PATCH must not reach cloud")

    monkeypatch.setattr(server.sauth, "rest", rest)
    created = client.post("/api/collections", json={
        "name": "Digitization cart",
        "from": "Archive room",
        "collection_type": "scan",
    })
    row = created.get_json()["collection"]
    assert created.status_code == 200
    assert row["collection_type"] == "scan"
    assert calls[0][2][0]["collection_type"] == "scan"

    rejected = client.patch(f"/api/collections/{SCAN_COLLECTION_ID}", json={
        "collection_type": "capture", "expected_updated_at": STAMP,
    })
    assert rejected.status_code == 400
    assert "cannot be changed" in rejected.get_json()["error"]
    assert len(calls) == 1


def test_active_scan_state_projects_candidate_and_keeps_priority():
    rows = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "updated_at": "2026-08-20T10:00:00+00:00",
            "digitization_candidate": False,
            "scan_priority": "4",
        }},
        manual_entries={}, reviews={}, registration_cache={},
        scan_states={CAPTURE_ID: _scan_state_row()},
    )
    data = rows[0]["data"]
    assert data["digitization_candidate"] is True
    assert data["scan_priority"] == "4"
    assert data["projection_source"]["scan_state_updated_at"] == \
        server._capture_projection_stamp(STAMP)
    assert supabase_sync._projection_vector(data)["scan_state_updated_at"] is not None
