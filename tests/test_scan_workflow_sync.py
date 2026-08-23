from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import pytest

import server
import supabase_sync


OWNER_ID = "11111111-2222-3333-4444-555555555555"
CAPTURE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
QUEUE_ID = "12345678-1234-4234-8234-1234567890ab"
SESSION_ID = "87654321-4321-4321-8321-ba0987654321"
SCAN_COLLECTION_ID = "99999999-8888-4777-8666-555555555555"
SOURCE_COLLECTION_ID = "77777777-6666-4555-8444-333333333333"
STAMP = "2026-08-21T12:00:00+00:00"
VISUAL_SIGNATURE = {
    "version": 1,
    "algorithm": "whl-cover-v1",
    "aspect_milli": 750,
    "hue_hist": [0] * 12,
    "chroma_hist": [0] * 16,
    "chroma_grid": [85, 85, 0] * 48,
    "tone_grid": [128] * 48,
    "edge_grid": [0] * 48,
    "gradient_hist": [0] * 8,
    "dhash": "0000000000000000",
}
MATCH_EVIDENCE = {
    "version": 1,
    "components": {"text": 0.91, "color": 0.84, "structure": 0.79},
    "band": "likely",
}


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


def _queue_row(
    *,
    status="pending",
    candidate_capture_id=None,
    matched_capture_id=None,
    match_confidence=None,
    match_evidence=None,
    revision=1,
) -> dict:
    if status in {"proposed", "rejected", "matched"}:
        candidate_capture_id = candidate_capture_id or CAPTURE_ID
        match_confidence = 0.84 if match_confidence is None else match_confidence
        match_evidence = match_evidence or MATCH_EVIDENCE
    if status == "matched":
        matched_capture_id = matched_capture_id or candidate_capture_id
    return {
        "id": QUEUE_ID,
        "session_id": SESSION_ID,
        "owner_id": OWNER_ID,
        "scan_collection_id": SCAN_COLLECTION_ID,
        "photo_role": "cover",
        "ocr_text": "A captured herbal",
        "visual_signature": VISUAL_SIGNATURE,
        "status": status,
        "candidate_capture_id": candidate_capture_id,
        "matched_capture_id": matched_capture_id,
        "match_confidence": match_confidence,
        "match_evidence": match_evidence,
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
        if path == "rpc/propose_scan_search":
            return [_queue_row(status="proposed", revision=2)]
        if path == "rpc/approve_scan_search":
            return [_queue_row(status="matched", revision=3)]
        if path == "rpc/reject_scan_search":
            return [_queue_row(status="rejected", revision=3)]
        if path == "rpc/complete_scan_search":
            return [_queue_row(status="matched", revision=2)]
        raise AssertionError(path)

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    cfg = _user_cfg()

    assert supabase_sync.list_scan_search_queue(
        cfg, page_size=1, maximum_rows=2,
    ) == [_queue_row()]
    assert supabase_sync.enqueue_scan_search(
        cfg, QUEUE_ID, SCAN_COLLECTION_ID, "cover", "A captured herbal",
        session_id=SESSION_ID, visual_signature=VISUAL_SIGNATURE,
    )["status"] == "pending"
    assert supabase_sync.propose_scan_search(
        cfg, QUEUE_ID, CAPTURE_ID, 0.84, MATCH_EVIDENCE,
        expected_rows=[_queue_row()],
    )["status"] == "proposed"
    assert supabase_sync.approve_scan_search(
        cfg, QUEUE_ID, CAPTURE_ID,
    )["status"] == "matched"
    assert supabase_sync.reject_scan_search(
        cfg, QUEUE_ID, CAPTURE_ID,
    )["status"] == "rejected"
    assert supabase_sync.complete_scan_search(
        cfg, QUEUE_ID, CAPTURE_ID,
    )["matched_capture_id"] == CAPTURE_ID

    assert f"owner_id=eq.{OWNER_ID}" in calls[0][1]
    assert "status=in.(pending,proposed,failed)" in calls[0][1]
    assert calls[-5] == ("POST", "rpc/enqueue_scan_search", {
        "p_id": QUEUE_ID,
        "p_session_id": SESSION_ID,
        "p_scan_collection_id": SCAN_COLLECTION_ID,
        "p_photo_role": "cover",
        "p_ocr_text": "A captured herbal",
        "p_visual_signature": VISUAL_SIGNATURE,
    })
    assert calls[-4] == ("POST", "rpc/propose_scan_search", {
        "p_id": QUEUE_ID,
        "p_capture_id": CAPTURE_ID,
        "p_match_confidence": 0.84,
        "p_match_evidence": MATCH_EVIDENCE,
        "p_expected_row_ids": [QUEUE_ID],
    })
    assert calls[-3] == ("POST", "rpc/approve_scan_search", {
        "p_id": QUEUE_ID, "p_capture_id": CAPTURE_ID,
    })
    assert calls[-2] == ("POST", "rpc/reject_scan_search", {
        "p_id": QUEUE_ID, "p_capture_id": CAPTURE_ID,
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


def test_scan_queue_accepts_visual_only_cover_and_rejects_unshaped_proposal(
        monkeypatch):
    visual_only = {
        **_queue_row(),
        "ocr_text": "",
        "visual_signature": VISUAL_SIGNATURE,
    }
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: [visual_only],
    )
    row = supabase_sync.enqueue_scan_search(
        _user_cfg(),
        QUEUE_ID,
        SCAN_COLLECTION_ID,
        "cover",
        "",
        session_id=SESSION_ID,
        visual_signature=VISUAL_SIGNATURE,
    )
    assert row["ocr_text"] == ""
    assert row["visual_signature"] == VISUAL_SIGNATURE

    malformed = {
        **_queue_row(),
        "status": "proposed",
        "candidate_capture_id": CAPTURE_ID,
        "match_confidence": 0.8,
        "match_evidence": None,
    }
    with pytest.raises(supabase_sync.SyncError, match="invalid row"):
        supabase_sync._scan_search_queue_row(malformed, owner_id=OWNER_ID)

    invalid_signature = {"version": 1, "algorithm": "whl-cover-v1"}
    with pytest.raises(ValueError, match="visual signature is invalid"):
        supabase_sync.enqueue_scan_search(
            _user_cfg(),
            QUEUE_ID,
            SCAN_COLLECTION_ID,
            "cover",
            "",
            session_id=SESSION_ID,
            visual_signature=invalid_signature,
        )
    with pytest.raises(supabase_sync.SyncError, match="invalid visual signature"):
        supabase_sync._scan_search_queue_row(
            {**visual_only, "visual_signature": invalid_signature},
            owner_id=OWNER_ID,
        )


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_scan_queue_accepts_evidence_less_pending_or_failed_row(status):
    placeholder = {
        **_queue_row(status=status),
        "ocr_text": "",
        "visual_signature": None,
    }

    parsed = supabase_sync._scan_search_queue_row(
        placeholder,
        owner_id=OWNER_ID,
    )

    assert parsed["status"] == status
    assert parsed["ocr_text"] == ""
    assert parsed["visual_signature"] is None


@pytest.mark.parametrize("status", ["proposed", "matched", "rejected"])
def test_scan_queue_rejects_evidence_less_review_or_terminal_row(status):
    malformed = {
        **_queue_row(status=status),
        "ocr_text": "",
        "visual_signature": None,
    }

    with pytest.raises(supabase_sync.SyncError, match="invalid row"):
        supabase_sync._scan_search_queue_row(malformed, owner_id=OWNER_ID)


@pytest.mark.parametrize(
    "expected_rows",
    [
        [],
        [{"id": "not-a-uuid"}],
        [{"id": QUEUE_ID}, {"id": QUEUE_ID}],
        [{"id": SESSION_ID}],
    ],
)
def test_scan_proposal_requires_an_exact_unique_session_snapshot(
    monkeypatch,
    expected_rows,
):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: pytest.fail("invalid snapshots stay local"),
    )

    with pytest.raises(ValueError, match="expected scan queue|expected scan search"):
        supabase_sync.propose_scan_search(
            _user_cfg(),
            QUEUE_ID,
            CAPTURE_ID,
            0.84,
            MATCH_EVIDENCE,
            expected_rows=expected_rows,
        )


def test_scan_match_inventory_is_owner_bounded(monkeypatch):
    calls = []
    raw = {
        "id": CAPTURE_ID,
        "created_by": OWNER_ID,
        "title": "A captured herbal",
        "author": "A. Botanist",
        "year": "1812",
        "photo_count": 2,
        "removed": False,
    }

    def rest(_cfg, method, path, *_args, **_kwargs):
        calls.append((method, path))
        query = parse_qs(path.partition("?")[2])
        return [raw] if query.get("offset") == ["0"] else []

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    rows = supabase_sync.list_scan_match_candidates(
        _user_cfg(), page_size=1, maximum_rows=2,
    )
    assert rows == [{
        "capture_id": CAPTURE_ID,
        "title": "A captured herbal",
        "author": "A. Botanist",
        "year": "1812",
        "photo_count": 2,
    }]
    assert len(calls) == 2
    assert all(f"created_by=eq.{OWNER_ID}" in path for _method, path in calls)
    assert all("removed=eq.false" in path for _method, path in calls)


def test_desktop_matcher_persists_one_session_proposal(monkeypatch):
    candidate = {
        "capture_id": CAPTURE_ID,
        "title": "A captured herbal",
        "ocr_text": "A captured herbal by A. Botanist",
    }
    monkeypatch.setattr(
        server.sbase,
        "list_scan_match_candidates",
        lambda _cfg: [candidate],
    )
    monkeypatch.setattr(
        server,
        "_scan_match_candidate_record",
        lambda row: dict(row),
    )
    proposals = []
    monkeypatch.setattr(
        server.sbase,
        "propose_scan_search",
        lambda cfg, queue_id, capture_id, confidence, evidence, *, expected_rows:
            proposals.append((
                dict(cfg), queue_id, capture_id, confidence, evidence,
                [dict(row) for row in expected_rows],
            )) or _queue_row(status="proposed", revision=2),
    )

    assert server._scan_propose_pending_sessions(
        _user_cfg(), [{**_queue_row(), "visual_signature": None}],
    ) is True
    assert proposals[0][1:3] == (QUEUE_ID, CAPTURE_ID)
    assert 0 <= proposals[0][3] <= 1
    assert proposals[0][4]["components"]["text"] > 0.8
    assert proposals[0][5] == [{**_queue_row(), "visual_signature": None}]


def test_desktop_matcher_retries_a_changed_session_snapshot(monkeypatch):
    candidate = {
        "capture_id": CAPTURE_ID,
        "title": "A captured herbal",
        "ocr_text": "A captured herbal by A. Botanist",
    }
    monkeypatch.setattr(
        server.sbase,
        "list_scan_match_candidates",
        lambda _cfg: [candidate],
    )
    monkeypatch.setattr(
        server,
        "_scan_match_candidate_record",
        lambda row: dict(row),
    )
    expected = []

    def changed(_cfg, _queue_id, _capture_id, _confidence, _evidence, *, expected_rows):
        expected.extend(dict(row) for row in expected_rows)
        raise supabase_sync.SyncError("session snapshot changed", error_code="40001")

    monkeypatch.setattr(server.sbase, "propose_scan_search", changed)
    row = {**_queue_row(), "visual_signature": None}

    assert server._scan_propose_pending_sessions(_user_cfg(), [row]) is False
    assert expected == [row]


@pytest.mark.parametrize("blocked_status", ["pending", "failed"])
def test_desktop_matcher_skips_session_with_blank_or_failed_row(
        monkeypatch, blocked_status):
    monkeypatch.setattr(
        server.sbase,
        "list_scan_match_candidates",
        lambda _cfg: pytest.fail("blocked sessions must not load match candidates"),
    )
    ready = {**_queue_row(), "visual_signature": None}
    blocked = {
        **_queue_row(status=blocked_status),
        "id": CAPTURE_ID,
    }
    if blocked_status == "pending":
        blocked.update(ocr_text="", visual_signature=None)

    assert server._scan_propose_pending_sessions(
        _user_cfg(),
        [ready, blocked],
    ) is False


def test_local_match_candidate_carries_inventory_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "CAPTURES_DIR", tmp_path)
    record = server._scan_match_candidate_record({
        "capture_id": CAPTURE_ID,
        "title": "A captured herbal",
        "author": "A. Botanist",
        "year": "1812",
        "photo_count": 0,
    })

    assert record == {
        "capture_id": CAPTURE_ID,
        "title": "A captured herbal",
        "author": "A. Botanist",
        "year": "1812",
    }


def test_local_match_candidate_cache_reuses_and_invalidates_file_evidence(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(server, "CAPTURES_DIR", tmp_path)
    directory = tmp_path / CAPTURE_ID
    directory.mkdir()
    ocr_path = directory / "ocr.txt"
    ocr_path.write_text("A captured herbal", encoding="utf-8")
    (directory / "photo_assets.json").write_text(json.dumps({
        "assets": [{
            "capture_order": 1,
            "role": {"suggested": "cover", "manual_override": None},
        }],
    }), encoding="utf-8")
    photo = directory / "photo_1.jpg"
    photo.write_bytes(b"candidate-cover-one")
    calls = []

    def signature(image):
        calls.append(bytes(image))
        return {"version": 1, "digest": len(image)}

    monkeypatch.setattr(server.cover_matching, "build_visual_signature", signature)
    server._scan_match_candidate_cache.clear()
    row = {
        "capture_id": CAPTURE_ID,
        "title": "A captured herbal",
        "author": "A. Botanist",
        "year": "1812",
        "photo_count": 1,
    }

    first = server._scan_match_candidate_record(row)
    first["visual_signatures"][0]["digest"] = -1
    second = server._scan_match_candidate_record(row)
    assert len(calls) == 1
    assert second["visual_signatures"][0]["digest"] == len(b"candidate-cover-one")

    metadata_changed = {**row, "author": "Another Botanist"}
    assert server._scan_match_candidate_record(metadata_changed)["author"] == "Another Botanist"
    assert len(calls) == 2

    photo.write_bytes(b"candidate-cover-two-with-a-new-identity")
    refreshed = server._scan_match_candidate_record(metadata_changed)
    assert len(calls) == 3
    assert refreshed["visual_signatures"][0]["digest"] == len(
        b"candidate-cover-two-with-a-new-identity"
    )

    ocr_path.write_text("A revised captured herbal with more text", encoding="utf-8")
    revised = server._scan_match_candidate_record(metadata_changed)
    assert revised["ocr_text"] == "A revised captured herbal with more text"
    assert len(calls) == 4
    server._scan_match_candidate_cache.clear()


def test_local_match_candidate_cache_is_lru_and_byte_bounded():
    cache = server._BoundedScanMatchCandidateCache(max_entries=2, max_bytes=256)
    cache.put("one", {"ocr_text": "one"})
    cache.put("two", {"ocr_text": "two"})
    assert cache.get("one")[0] is True  # one is now most recently used
    cache.put("three", {"ocr_text": "three"})
    assert cache.get("two") == (False, None)
    assert cache.get("one")[0] is True
    assert cache.get("three")[0] is True

    cache.put("oversized", {"ocr_text": "x" * 1_000})
    assert cache.get("oversized") == (False, None)


def test_local_match_candidate_cache_is_safe_under_concurrent_refreshes():
    cache = server._BoundedScanMatchCandidateCache(max_entries=8, max_bytes=4_096)

    def refresh(index):
        key = f"candidate-{index % 20}"
        cache.put(key, {"ocr_text": f"text-{index}"})
        hit, value = cache.get(key)
        assert not hit or isinstance(value, dict)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(refresh, range(500)))

    assert 0 <= cache._bytes <= 4_096
    assert len(cache._entries) <= 8


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
        server, "_scan_propose_pending_sessions", lambda _cfg, _rows: False,
    )
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
        lambda cfg, queue_id, collection_id, photo_role, ocr_text, **kwargs:
            enqueues.append((
                dict(cfg), queue_id, collection_id, photo_role, ocr_text, kwargs,
            ))
            or _queue_row(),
    )
    approvals = []
    monkeypatch.setattr(
        server.sbase,
        "approve_scan_search",
        lambda cfg, queue_id, capture_id:
            approvals.append((dict(cfg), queue_id, capture_id)) or
            _queue_row(status="matched", revision=2),
    )
    rejections = []
    monkeypatch.setattr(
        server.sbase,
        "reject_scan_search",
        lambda cfg, queue_id, capture_id:
            rejections.append((dict(cfg), queue_id, capture_id)) or
            _queue_row(status="rejected", revision=2),
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
        "session_id": SESSION_ID,
        "scan_collection_id": SCAN_COLLECTION_ID,
        "photo_role": "cover",
        "ocr_text": "A captured herbal",
        "visual_signature": VISUAL_SIGNATURE,
    })
    approved = client.post(
        f"/api/scan-search-queue/{QUEUE_ID}/approve",
        json={"capture_id": CAPTURE_ID},
    )
    rejected = client.post(
        f"/api/scan-search-queue/{QUEUE_ID}/reject",
        json={"capture_id": CAPTURE_ID},
    )
    completed = client.post(
        f"/api/scan/search-queue/{QUEUE_ID}/complete",
        json={"capture_id": CAPTURE_ID},
    )

    assert queue["queue"][0]["id"] == QUEUE_ID
    assert states["states"][0]["active"] is True
    assert enqueued.status_code == 200
    assert enqueues[0][1:] == (
        QUEUE_ID, SCAN_COLLECTION_ID, "cover", "A captured herbal",
        {"session_id": SESSION_ID, "visual_signature": VISUAL_SIGNATURE},
    )
    assert approved.status_code == 200
    assert rejected.status_code == 200
    assert approvals[0][1:] == (QUEUE_ID, CAPTURE_ID)
    assert rejections[0][1:] == (QUEUE_ID, CAPTURE_ID)
    assert completed.status_code == 200
    assert completions[0][1:] == (QUEUE_ID, CAPTURE_ID)
    assert all(cfg["access_token"] == _jwt() for cfg in configs)
    assert completions[0][0]["access_token"] == _jwt()
    assert enqueues[0][0]["access_token"] == _jwt()
    assert approvals[0][0]["access_token"] == _jwt()
    assert rejections[0][0]["access_token"] == _jwt()


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
