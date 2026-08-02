"""CH master-list reconciliation endpoints for the Corrections manager."""

from __future__ import annotations

import hashlib
import io
import json

import libcommon as lib
import pytest
import server
import whl_client
from PIL import Image


# The three fixture rows exercise, in order: a canonical match target, a
# same-title-bucket hard negative that only the strict threshold excludes,
# and a basis whose Java hash is negative on 32 bits.
CH_ROWS = [
    {
        "authors": "Grieve, Maud",
        "publication": "A_Modern_Herbal_Volume_1",
        "year_of_publication": 1931,
        "edition": "First",
        "condition": "good",
        "page_reference": "888",
        "city_published": "London",
        "publisher": "Jonathan Cape",
        "key": "Herbal", "key_2": "", "key_3": "",
        "illustrations": "line drawings",
        "notes": "",
        "price": "12",
        "date": "1994-05-01",
    },
    {
        "authors": "Smith, John",
        "publication": (
            "A_Modern_Herbal_Encyclopaedia_of_Completely_Different_Things"
        ),
        "year_of_publication": 1950,
        "edition": "", "condition": "", "page_reference": "",
        "city_published": "", "publisher": "",
        "key": "", "key_2": "", "key_3": "",
        "illustrations": "", "notes": "", "price": "", "date": "",
    },
    {
        "authors": "Fernie, W. T.",
        "publication": "Herbal_Simples",
        "year_of_publication": 1897,
        "edition": "", "condition": "", "page_reference": "",
        "city_published": "", "publisher": "",
        "key": "", "key_2": "", "key_3": "",
        "illustrations": "", "notes": "", "price": "", "date": "",
    },
]
ROW0_KEY = "6e805628-39"      # "a modern herbal volume 1maud grieve1931"
ROW2_KEY = "8a7b43ca-28"      # "herbal simplesw t fernie1897" (negative hash)


@pytest.fixture()
def ch_workspace(monkeypatch, tmp_path):
    """Isolated stores + engine session, as in test_capture_archive_import."""

    workspace = tmp_path / "output"
    workspace.mkdir()
    monkeypatch.setattr(
        lib,
        "MANUAL_ENTRIES_PATH",
        workspace / "manual_entries.json",
    )
    monkeypatch.setattr(lib, "CH_LIBRARY_JSON_PATH", tmp_path / "ch_library.json")
    monkeypatch.setattr(server, "BUILDS_PATH", workspace / "whl_builds.json")
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
        yield workspace
    finally:
        session.close()


def _write_ch_rows(rows=CH_ROWS) -> None:
    lib.CH_LIBRARY_JSON_PATH.write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _jpeg(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), tuple(digest[:3])).save(
        stream,
        format="JPEG",
        quality=92,
    )
    return stream.getvalue()


def _ingest(monkeypatch, capture_id: str, meta: dict) -> tuple[str, str]:
    """One capture-backed item; returns (canonical item id, entry id)."""

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda _entry: {})
    monkeypatch.setattr(server, "activity", lambda *_args, **_kwargs: None)
    entry_id, errors = server.ingest_capture(
        {
            "id": capture_id,
            "ocr": {"photo_1.jpg": "Garden sage."},
            "meta": meta,
        },
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    assert entry_id
    assert errors == []
    return server._capture_archive_association(capture_id).book_id, entry_id


def _client():
    server.app.config["TESTING"] = True
    return server.app.test_client()


def _state(client, item_id: str):
    return client.get(
        "/api/corrections/ch/state", query_string={"item_id": item_id})


def _manual_entry(entry_id: str) -> dict:
    return lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]


# --- key math ------------------------------------------------------------------


def test_phone_key_matches_java_hashcode_contract():
    # Anchors verified against the JVM: "abc".hashCode() == 96354 and
    # "polygenelubricants".hashCode() == Integer.MIN_VALUE, whose unsigned
    # rendering exercises the negative-hash path end to end.
    assert server._ch_java_hash_hex("abc") == "17862"
    assert server._ch_java_hash_hex("polygenelubricants") == "80000000"
    assert server._ch_java_hash_hex("") == "0"
    assert server._ch_phone_key("abc", "", "") == "17862-3"
    assert server._ch_phone_key(
        "herbal simples", "w t fernie", "1897") == ROW2_KEY


def test_index_keys_use_display_title_flipped_author_and_optstring_year(
        ch_workspace):
    _write_ch_rows()

    index = server._ch_reconcile_index()

    keys = {entry["index"]: entry["key"] for entry in index["entries"]}
    assert keys[0] == ROW0_KEY
    assert keys[2] == ROW2_KEY
    assert index["by_key"][ROW0_KEY] == [0]
    # int years render exactly as the phone's JSONObject.optString does
    assert index["entries"][0]["year"] == "1931"


# --- state ---------------------------------------------------------------------


def test_state_resolves_phone_stamp_by_key(monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, _entry_id = _ingest(monkeypatch, "c1a10001-0000-4000-8000-000000000001", {
        "title": "A Modern Herbal",
        "ch_match": {
            "key": ROW0_KEY,
            "title": "A Modern Herbal Volume 1",
            "author": "Grieve, Maud",
            "score": 0.92,
            "adopted": "author,year",
            "conflicts": "publisher",
        },
    })

    response = _state(_client(), item_id)

    assert response.status_code == 200
    state = response.get_json()
    assert state["ok"] is True
    assert state["list_available"] is True
    assert state["candidates"] == []
    assert state["rejected"] is None
    match = state["match"]
    assert match["resolution"] == "key"
    assert match["key"] == ROW0_KEY
    assert match["score"] == 0.92
    assert match["adopted"] == ["author", "year"]
    assert match["conflicts"] == ["publisher"]
    assert match["row"]["index"] == 0
    assert match["row"]["key"] == ROW0_KEY
    assert match["row"]["fields"]["publisher"] == "Jonathan Cape"


def test_state_rematches_a_stale_stamp_key_by_strict_title(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, _entry_id = _ingest(monkeypatch, "c1a10002-0000-4000-8000-000000000002", {
        "title": "A Modern Herbal",
        "ch_match": {
            "key": "deadbeef-39",
            "title": "A Modern Herbal Volume 1",
            "author": "Grieve, Maud",
            "score": 0.92,
            "adopted": "author",
        },
    })

    match = _state(_client(), item_id).get_json()["match"]

    assert match["resolution"] == "rematch"
    assert match["key"] == "deadbeef-39"
    assert match["row"]["key"] == ROW0_KEY
    assert match["row"]["index"] == 0
    assert match["adopted"] == ["author"]
    assert match["conflicts"] == []


def test_state_candidates_apply_the_shared_thresholds(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, _entry_id = _ingest(monkeypatch, "c1a10003-0000-4000-8000-000000000003", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
    })

    state = _state(_client(), item_id).get_json()

    assert state["match"] is None
    keys = [candidate["key"] for candidate in state["candidates"]]
    # Row 1 shares the 12-char title bucket but fails the strict full-title
    # bar for a different author; row 2 shares no bucket and no author token.
    assert keys == [ROW0_KEY]
    candidate = state["candidates"][0]
    assert candidate["index"] == 0
    assert candidate["score"] == round(whl_client.similarity(
        "A Modern Herbal", "A Modern Herbal Volume 1"), 3)
    assert candidate["fields"]["categories"] == "Herbal"


def test_state_candidates_are_capped_at_five(monkeypatch, ch_workspace):
    _write_ch_rows([
        {
            "authors": "Grieve, Maud",
            "publication": f"A_Modern_Herbal_Volume_{volume}",
            "year_of_publication": 1931 + volume,
        }
        for volume in range(1, 8)
    ])
    item_id, _entry_id = _ingest(monkeypatch, "c1a10004-0000-4000-8000-000000000004", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
    })

    state = _state(_client(), item_id).get_json()

    assert len(state["candidates"]) == 5


def test_state_without_master_list_reports_unavailable(
        monkeypatch, ch_workspace):
    item_id, _entry_id = _ingest(monkeypatch, "c1a10005-0000-4000-8000-000000000005", {
        "title": "A Modern Herbal",
    })

    response = _state(_client(), item_id)

    assert response.status_code == 200
    state = response.get_json()
    assert state == {
        "ok": True,
        "item_id": item_id,
        "list_available": False,
        "match": None,
        "candidates": [],
        "rejected": None,
    }


def test_state_requires_a_capture_backed_item(ch_workspace):
    _write_ch_rows()
    build, error = server._create_build({"title": "Plain catalogue build"})
    assert error == ""
    client = _client()

    response = _state(client, build["id"])
    missing = _state(client, "b-" + "0" * 32)

    assert response.status_code == 422
    assert response.get_json()["code"] == "not_capture_backed"
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "item_not_found"


# --- approve -------------------------------------------------------------------


def test_approve_merges_blank_fields_and_stamps_phone_wire_format(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a10006-0000-4000-8000-000000000006", {
        "title": "A Modern Herbal",
        "edition": "Second",
    })
    capture_association = server._capture_archive_association(
        "c1a10006-0000-4000-8000-000000000006")
    assert capture_association.state.value == "current"
    client = _client()

    response = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-merge-1",
    })

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["replayed"] is False
    assert body["adopted"] == [
        "author", "year", "publisher", "city", "pages", "condition",
        "illustrations", "price", "categories",
    ]
    assert body["conflicts"] == ["title", "edition"]
    assert body["match"]["resolution"] == "key"
    assert body["match"]["row"]["index"] == 0

    entry = _manual_entry(entry_id)
    assert entry["title"] == "A Modern Herbal"        # conflict keeps the scan
    assert entry["edition"] == "Second"               # conflict keeps the scan
    assert entry["author"] == "Grieve, Maud"
    assert entry["year"] == "1931"
    assert entry["publisher"] == "Jonathan Cape"
    assert entry["city"] == "London"
    assert entry["pages"] == "888"
    assert entry["condition"] == "good"
    assert entry["illustrations"] == "line drawings"
    assert entry["price"] == "12"
    assert entry["categories"] == "Herbal"
    # Byte-exact phone wire format (ChMergePresenter.apply's ch_match object).
    assert entry["extra"]["ch_match"] == {
        "key": ROW0_KEY,
        "title": "A Modern Herbal Volume 1",
        "author": "Grieve, Maud",
        "score": whl_client.similarity(
            "A Modern Herbal", "A Modern Herbal Volume 1"),
        "adopted": (
            "author,year,publisher,city,pages,condition,"
            "illustrations,price,categories"
        ),
        "conflicts": "title,edition",
    }
    stale = server._capture_archive_association("c1a10006-0000-4000-8000-000000000006")
    assert stale.state.value == "stale"


def test_approve_adopts_the_title_over_the_ingest_placeholder(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a10007-0000-4000-8000-000000000007", {
        "author": "Grieve, Maud",
    })
    assert _manual_entry(entry_id)["title"].startswith("(untitled capture")

    response = _client().post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-untitled-1",
    })

    body = response.get_json()
    assert body["adopted"][0] == "title"
    assert "author" not in body["adopted"]             # already agrees
    entry = _manual_entry(entry_id)
    assert entry["title"] == "A Modern Herbal Volume 1"
    assert entry["extra"]["ch_match"]["score"] == 0.0  # no scan title to score


def test_approve_replays_idempotently_and_defends_the_operation_id(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a10008-0000-4000-8000-000000000008", {
        "title": "A Modern Herbal",
    })
    client = _client()
    first = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-replay-1",
    })
    assert first.status_code == 200
    stamped = _manual_entry(entry_id)

    replay = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-replay-1",
    })
    reused = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW2_KEY,
        "operation_id": "ch-approve-replay-1",
    })

    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["adopted"] == first.get_json()["adopted"]
    assert _manual_entry(entry_id) == stamped          # no second write
    assert reused.status_code == 409
    assert reused.get_json()["code"] == "operation_id_conflict"


def test_approve_rejects_unknown_keys_missing_lists_and_non_captures(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, _entry_id = _ingest(monkeypatch, "c1a10009-0000-4000-8000-000000000009", {
        "title": "A Modern Herbal",
    })
    build, error = server._create_build({"title": "Plain catalogue build"})
    assert error == ""
    client = _client()

    unknown = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id, "key": "deadbeef-5",
        "operation_id": "ch-approve-unknown",
    })
    non_capture = client.post("/api/corrections/ch/approve", json={
        "item_id": build["id"], "key": ROW0_KEY,
        "operation_id": "ch-approve-noncapture",
    })
    bad_operation = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id, "key": ROW0_KEY,
        "operation_id": "spaces are invalid",
    })
    lib.CH_LIBRARY_JSON_PATH.unlink()
    no_list = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id, "key": ROW0_KEY,
        "operation_id": "ch-approve-nolist",
    })

    assert unknown.status_code == 409
    assert unknown.get_json()["code"] == "ch_key_unknown"
    assert non_capture.status_code == 422
    assert non_capture.get_json()["code"] == "not_capture_backed"
    assert bad_operation.status_code == 400
    assert bad_operation.get_json()["code"] == "invalid_operation_id"
    assert no_list.status_code == 409
    assert no_list.get_json()["code"] == "ch_list_unavailable"


def test_approve_on_a_promoted_capture_skips_columns_builds_cannot_hold(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    capture_id = "c1a1000c-0000-4000-8000-00000000000c"
    item_id, _entry_id = _ingest(monkeypatch, capture_id, {
        "title": "A Modern Herbal",
    })
    build, error = server._create_build({
        "title": "A Modern Herbal",
        "capture_id": capture_id,
    })
    assert error == ""

    response = _client().post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-build-1",
    })

    assert response.status_code == 200
    body = response.get_json()
    # WhlCatalogueItemCodec has no condition/illustrations/price columns.
    assert body["adopted"] == [
        "author", "year", "edition", "publisher", "city", "pages",
        "categories",
    ]
    row = lib.load_json(server.BUILDS_PATH, {})[build["id"]]
    assert row["authors"] == "Grieve, Maud"
    assert row["year"] == "1931"
    assert row["publisher_city"] == "London"
    assert "condition" not in row
    assert row["extra"]["ch_match"]["key"] == ROW0_KEY
    assert row["extra"]["ch_match"]["adopted"] == (
        "author,year,edition,publisher,city,pages,categories"
    )


# --- reject --------------------------------------------------------------------


def test_reject_remembers_the_decision_and_suppresses_the_candidate(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a1000a-0000-4000-8000-00000000000a", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
    })
    client = _client()
    before = _state(client, item_id).get_json()
    assert [c["key"] for c in before["candidates"]] == [ROW0_KEY]

    response = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-reject-1",
    })

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["rejected"]["key"] == ROW0_KEY
    review = _manual_entry(entry_id)["extra"]["ch_review"]
    assert review["decision"] == "rejected"
    assert review["key"] == ROW0_KEY
    assert review["decided_at"] == body["rejected"]["decided_at"]
    state = _state(client, item_id).get_json()
    assert state["rejected"] == body["rejected"]
    assert state["candidates"] == []
    assert server._capture_archive_association(
        "c1a1000a-0000-4000-8000-00000000000a").state.value == "stale"

    replay = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-reject-1",
    })
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["rejected"] == body["rejected"]


def test_reject_of_the_approved_key_revokes_the_approval(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a1000b-0000-4000-8000-00000000000b", {
        "title": "A Modern Herbal",
    })
    client = _client()
    approved = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-revoke-approve",
    })
    assert approved.status_code == 200

    response = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-revoke-reject",
    })

    assert response.status_code == 200
    extra = _manual_entry(entry_id)["extra"]
    assert "ch_match" not in extra
    assert extra["ch_review"]["key"] == ROW0_KEY
    state = _state(client, item_id).get_json()
    assert state["match"] is None
    assert state["rejected"]["key"] == ROW0_KEY


def test_reject_of_a_rematched_row_key_revokes_the_stale_approval(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a1000c-0000-4000-8000-00000000000c", {
        "title": "A Modern Herbal",
        "ch_match": {
            "key": "deadbeef-39",
            "title": "A Modern Herbal Volume 1",
            "author": "Grieve, Maud",
            "score": 0.92,
            "adopted": "author",
        },
    })
    client = _client()
    match = _state(client, item_id).get_json()["match"]
    assert match["resolution"] == "rematch"
    assert match["row"]["key"] == ROW0_KEY

    response = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-rematch-reject",
    })

    assert response.status_code == 200
    extra = _manual_entry(entry_id)["extra"]
    assert "ch_match" not in extra
    assert extra["ch_review"]["key"] == ROW0_KEY
    state = _state(client, item_id).get_json()
    assert state["match"] is None
    assert state["rejected"]["key"] == ROW0_KEY


# --- extra-held scan values ----------------------------------------------------


def test_merge_consults_extra_held_scan_values(monkeypatch, ch_workspace):
    """CH-only phone fields land in the entry's extra at ingest; the phone's
    presenter compares against them, so the desktop must not adopt over an
    agreeing value or overwrite a differing one."""
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a1000d-0000-4000-8000-00000000000d", {
        "title": "A Modern Herbal",
        "pages": "888",       # agrees with the CH row after normalization
        "price": "999",       # differs from the CH row's "12"
    })
    entry = _manual_entry(entry_id)
    assert entry["extra"]["pages"] == "888"
    assert not entry.get("pages")

    response = _client().post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-extra-1",
    })

    assert response.status_code == 200
    body = response.get_json()
    assert "pages" not in body["adopted"]
    assert "pages" not in body["conflicts"]
    assert "price" in body["conflicts"]
    assert "price" not in body["adopted"]
    merged = _manual_entry(entry_id)
    assert not merged.get("pages")        # agreement writes nothing
    assert not merged.get("price")        # the conflict keeps the scan value
    assert merged["extra"]["price"] == "999"


# --- duplicate CH rows ---------------------------------------------------------


def test_duplicate_identical_rows_dedupe_and_resolve_to_the_lowest_index(
        monkeypatch, ch_workspace):
    _write_ch_rows([CH_ROWS[0], dict(CH_ROWS[0]), CH_ROWS[2]])
    item_id, entry_id = _ingest(monkeypatch, "c1a1000e-0000-4000-8000-00000000000e", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
    })
    client = _client()
    state = _state(client, item_id).get_json()
    assert [c["key"] for c in state["candidates"]] == [ROW0_KEY]

    response = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-dupe-1",
    })

    assert response.status_code == 200
    body = response.get_json()
    assert body["match"]["row"]["index"] == 0
    assert _manual_entry(entry_id)["extra"]["ch_match"]["key"] == ROW0_KEY


def test_duplicate_diverging_rows_still_conflict(monkeypatch, ch_workspace):
    variant = dict(CH_ROWS[0])
    variant["publisher"] = "Somebody Else"
    _write_ch_rows([CH_ROWS[0], variant])
    item_id, _entry_id = _ingest(monkeypatch, "c1a1000f-0000-4000-8000-00000000000f", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
    })
    client = _client()

    approve = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-ambiguous-1",
    })
    reject = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-reject-ambiguous-1",
    })

    assert approve.status_code == 409
    assert approve.get_json()["code"] == "ch_key_ambiguous"
    assert reject.status_code == 409
    assert reject.get_json()["code"] == "ch_key_ambiguous"


# --- re-pin provenance ---------------------------------------------------------


def test_approve_re_pin_carries_stamp_provenance_forward(
        monkeypatch, ch_workspace):
    """Approving the moved key of an already-stamped row must not restate the
    merge against post-merge values: the phone's recorded adoptions and
    conflicts carry into the new stamp, unioned with new adoptions."""
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a10012-0000-4000-8000-000000000012", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
        "publisher": "Different House",
        "ch_match": {
            "key": "deadbeef-39",
            "title": "A Modern Herbal Volume 1",
            "author": "Grieve, Maud",
            "score": 0.92,
            "adopted": "author,year",
            "conflicts": "publisher",
        },
    })
    client = _client()
    match = _state(client, item_id).get_json()["match"]
    assert match["resolution"] == "rematch"
    assert match["row"]["key"] == ROW0_KEY

    response = client.post("/api/corrections/ch/approve", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-approve-repin-1",
    })

    assert response.status_code == 200
    body = response.get_json()
    # "author" survives although the merged record already agrees with CH.
    assert body["adopted"] == [
        "author", "year", "edition", "city", "pages", "condition",
        "illustrations", "price", "categories",
    ]
    assert body["conflicts"] == ["title", "publisher"]
    stamp = _manual_entry(entry_id)["extra"]["ch_match"]
    assert stamp["key"] == ROW0_KEY
    assert stamp["adopted"] == (
        "author,year,edition,city,pages,condition,"
        "illustrations,price,categories"
    )
    assert stamp["conflicts"] == "title,publisher"


# --- unreject ------------------------------------------------------------------


def test_unreject_clears_the_decision_and_restores_the_candidate(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a10010-0000-4000-8000-000000000010", {
        "title": "A Modern Herbal",
        "author": "Grieve, Maud",
    })
    client = _client()
    rejected = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-unreject-setup",
    })
    assert rejected.status_code == 200
    assert _state(client, item_id).get_json()["candidates"] == []

    response = client.post("/api/corrections/ch/unreject", json={
        "item_id": item_id,
        "operation_id": "ch-unreject-1",
    })

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "replayed": False}
    # The codec drops an emptied extra from the stored row entirely.
    assert "ch_review" not in _manual_entry(entry_id).get("extra", {})
    state = _state(client, item_id).get_json()
    assert state["rejected"] is None
    assert [c["key"] for c in state["candidates"]] == [ROW0_KEY]

    replay = client.post("/api/corrections/ch/unreject", json={
        "item_id": item_id,
        "operation_id": "ch-unreject-2",
    })
    assert replay.status_code == 200
    assert replay.get_json() == {"ok": True, "replayed": True}


def test_unreject_without_a_rejection_replays_without_writing(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, entry_id = _ingest(monkeypatch, "c1a10013-0000-4000-8000-000000000013", {
        "title": "A Modern Herbal",
    })
    before = _manual_entry(entry_id)

    response = _client().post("/api/corrections/ch/unreject", json={
        "item_id": item_id,
        "operation_id": "ch-unreject-noop",
    })

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "replayed": True}
    assert _manual_entry(entry_id) == before


def test_unreject_validates_like_the_other_ch_mutations(
        monkeypatch, ch_workspace):
    _write_ch_rows()
    item_id, _entry_id = _ingest(monkeypatch, "c1a10011-0000-4000-8000-000000000011", {
        "title": "A Modern Herbal",
    })
    build, error = server._create_build({"title": "Plain catalogue build"})
    assert error == ""
    client = _client()
    rejected = client.post("/api/corrections/ch/reject", json={
        "item_id": item_id,
        "key": ROW0_KEY,
        "operation_id": "ch-unreject-reused",
    })
    assert rejected.status_code == 200

    missing_item = client.post("/api/corrections/ch/unreject", json={
        "operation_id": "ch-unreject-x",
    })
    bad_operation = client.post("/api/corrections/ch/unreject", json={
        "item_id": item_id,
        "operation_id": "spaces are invalid",
    })
    non_capture = client.post("/api/corrections/ch/unreject", json={
        "item_id": build["id"],
        "operation_id": "ch-unreject-x",
    })
    unknown = client.post("/api/corrections/ch/unreject", json={
        "item_id": "b-" + "0" * 32,
        "operation_id": "ch-unreject-x",
    })
    reused = client.post("/api/corrections/ch/unreject", json={
        "item_id": item_id,
        "operation_id": "ch-unreject-reused",
    })

    assert missing_item.status_code == 400
    assert missing_item.get_json()["code"] == "invalid_ch_request"
    assert bad_operation.status_code == 400
    assert bad_operation.get_json()["code"] == "invalid_operation_id"
    assert non_capture.status_code == 422
    assert non_capture.get_json()["code"] == "not_capture_backed"
    assert unknown.status_code == 404
    assert unknown.get_json()["code"] == "item_not_found"
    assert reused.status_code == 409
    assert reused.get_json()["code"] == "operation_id_conflict"
