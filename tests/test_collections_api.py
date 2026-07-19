from __future__ import annotations

import copy
import json
from datetime import datetime
from urllib.parse import parse_qs

import pytest

SURVIVOR = "11111111-2222-3333-4444-555555555555"
DUPLICATE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FINAL = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(autouse=True)
def _isolated_collection_alias_cache(monkeypatch, data_root, request):
    """A merge in one test must never canonicalize ids in a later test."""
    import server

    safe_name = "".join(ch if ch.isalnum() else "-" for ch in request.node.name)
    monkeypatch.setattr(
        server, "COLLECTION_ALIASES_PATH",
        data_root / "output" / f"collection-aliases-{safe_name}.json",
    )


def _signed_in(monkeypatch, server):
    monkeypatch.setattr(server, "_auth_cfg", lambda: {
        "url": "https://example.supabase.co", "key": "anon",
    })
    monkeypatch.setattr(server, "_auth_session", lambda: {
        "access_token": "user-jwt", "user_id": "user-1",
    })


class CollectionCloud:
    """Small PostgREST compare-and-swap model for route tests."""

    def __init__(self, rows):
        self.rows = {row["id"]: copy.deepcopy(row) for row in rows}
        self.calls = []

    def __call__(self, _cfg, _token, method, path, body=None, **kwargs):
        self.calls.append((method, path, copy.deepcopy(body), kwargs))
        table, _, raw = path.partition("?")
        if table == "rpc/merge_collections":
            assert method == "POST"
            survivor = self.rows.get(body["p_survivor_id"])
            duplicate = self.rows.get(body["p_duplicate_id"])
            if not survivor or not duplicate:
                return None
            if (duplicate.get("deleted")
                    and duplicate.get("merged_into") == survivor["id"]):
                return {"survivor": copy.deepcopy(survivor),
                        "duplicate": copy.deepcopy(duplicate),
                        "continued": True}
            if (survivor.get("deleted") or duplicate.get("deleted")
                    or survivor.get("updated_at") != body["p_survivor_updated_at"]
                    or duplicate.get("updated_at") != body["p_duplicate_updated_at"]):
                return None
            duplicate.update({
                "deleted": True,
                "merged_into": survivor["id"],
                "updated_at": "2026-07-19T12:00:00.654321+00:00",
            })
            return {"survivor": copy.deepcopy(survivor),
                    "duplicate": copy.deepcopy(duplicate),
                    "continued": False}
        assert table == "collections"
        query = parse_qs(raw)
        if method == "GET":
            rows = sorted(self.rows.values(), key=lambda row: row["id"])
            if "id" in query:
                id_filter = query["id"][0]
                if id_filter.startswith("eq."):
                    wanted = id_filter.removeprefix("eq.")
                    rows = [row for row in rows if row["id"] == wanted]
                elif id_filter.startswith("gt."):
                    cursor = id_filter.removeprefix("gt.")
                    rows = [row for row in rows if row["id"] > cursor]
            if query.get("deleted") == ["eq.false"]:
                rows = [row for row in rows if not row.get("deleted")]
            if "limit" in query:
                rows = rows[:int(query["limit"][0])]
            return copy.deepcopy(rows)
        if method == "PATCH":
            cid = query["id"][0].removeprefix("eq.")
            row = self.rows.get(cid)
            if not row:
                return []
            if query.get("deleted") == ["eq.false"] and row.get("deleted"):
                return []
            expected = query.get("updated_at", [""])[0].removeprefix("eq.")
            if expected and row.get("updated_at") != expected:
                return []
            row.update(copy.deepcopy(body))
            return [copy.deepcopy(row)]
        if method == "POST":
            assert isinstance(body, list) and len(body) == 1
            row = copy.deepcopy(body[0])
            # The database, not the desktop clock, supplies the initial revision.
            assert "updated_at" not in row
            row["updated_at"] = "2026-07-19T12:00:00.123456+00:00"
            self.rows[row["id"]] = row
            return [copy.deepcopy(row)]
        raise AssertionError((method, path))


def _row(cid, name, updated_at, *, deleted=False, from_place="Storage",
         merged_into=None):
    return {
        "id": cid, "name": name, "from_place": from_place,
        "created_by": "user-1", "updated_at": updated_at,
        "deleted": deleted, "merged_into": merged_into,
    }


def test_collection_aliases_flatten_chains_idempotently(monkeypatch, data_root):
    import server

    alias_path = data_root / "output" / "collection_aliases-test.json"
    monkeypatch.setattr(server, "COLLECTION_ALIASES_PATH", alias_path)

    server._remember_collection_alias(DUPLICATE, SURVIVOR)
    server._remember_collection_alias(SURVIVOR, FINAL)
    server._remember_collection_alias(DUPLICATE, FINAL)

    assert server._resolve_collection_alias(DUPLICATE) == FINAL
    assert server._resolve_collection_alias(SURVIVOR) == FINAL
    assert server.lib.load_json(alias_path, {}) == {
        "version": 2,
        "aliases": {DUPLICATE: FINAL, SURVIVOR: FINAL},
    }


def test_restore_and_checked_write_boundaries_heal_merged_ids(
        client, monkeypatch, data_root):
    import server

    alias_path = data_root / "output" / "collection_aliases-boundaries.json"
    monkeypatch.setattr(server, "COLLECTION_ALIASES_PATH", alias_path)
    server._remember_collection_alias(DUPLICATE, SURVIVOR)

    def snapshot(eid):
        return {"id": eid, "title": "A Book", "extra": {
            "scan_collection_id": DUPLICATE,
            "scan_collection": "Blue crate before merge",
            "scan_from": "Christopher Office",
        }}

    restored = client.post("/api/manual/restore", json={
        "entry": snapshot("alias-undo-restore"),
    })
    assert restored.status_code == 200
    assert restored.get_json()["entry"]["extra"] == {
        "scan_collection_id": SURVIVOR,
        "scan_collection": "Blue crate before merge",
        "scan_from": "Christopher Office",
    }

    record_path = data_root / "trash-record-alias.json"
    record_path.write_text(json.dumps(snapshot("alias-trash-restore")), encoding="utf-8")
    monkeypatch.setattr(server, "_trash_payload_path",
                        lambda _trash_id, rel: record_path if rel == "record.json" else None)
    result, status = server._trash_restore_record({
        "id": "trash-id", "kind": "manual_entry",
        "origin": {"entry_id": "alias-trash-restore"},
    })
    assert status == 200 and result["ok"] is True
    trashed = server.lib.load_json(server.lib.MANUAL_ENTRIES_PATH, {})[
        "alias-trash-restore"]
    assert trashed["extra"] == {
        "scan_collection_id": SURVIVOR,
        "scan_collection": "Blue crate before merge",
        "scan_from": "Christopher Office",
    }

    stale_book = snapshot("ignored")
    response = client.put("/api/client_state", json={
        "checked": [["ch:alias-stale", {"book": stale_book}]],
    })
    assert response.status_code == 200
    checked = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})["checked"]
    extra = checked[0][1]["book"]["extra"]
    assert extra == {
        "scan_collection_id": SURVIVOR,
        "scan_collection": "Blue crate before merge",
        "scan_from": "Christopher Office",
    }


def test_collection_create_uses_database_revision(client, monkeypatch):
    import server

    _signed_in(monkeypatch, server)
    cloud = CollectionCloud([])
    monkeypatch.setattr(server.sauth, "rest", cloud)

    response = client.post("/api/collections", json={
        "name": "  Blue   crate  ", "from": " Christopher   Office ",
    })

    assert response.status_code == 200
    collection = response.get_json()["collection"]
    assert collection["name"] == "Blue crate"
    assert collection["from"] == "Christopher Office"
    assert collection["updated_at"] == "2026-07-19T12:00:00.123456+00:00"
    sent = next(call[2][0] for call in cloud.calls if call[0] == "POST")
    assert "updated_at" not in sent


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/collections"),
    ("PATCH", f"/api/collections/{DUPLICATE}"),
    ("DELETE", f"/api/collections/{DUPLICATE}"),
    ("POST", "/api/collections/merge"),
])
def test_collection_mutations_reject_non_object_json(
        client, monkeypatch, method, path):
    import server

    _signed_in(monkeypatch, server)
    monkeypatch.setattr(
        server.sauth, "rest",
        lambda *_args, **_kwargs: pytest.fail("invalid JSON must not reach Supabase"),
    )

    response = client.open(path, method=method, json=["not", "an", "object"])

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False, "error": "JSON body must be an object",
    }


def test_signed_out_collection_list_still_returns_cached_merge_aliases(
        client, monkeypatch):
    import server

    server._remember_collection_alias(DUPLICATE, SURVIVOR)
    monkeypatch.setattr(server, "_collection_auth", lambda: None)

    response = client.get("/api/collections")

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True, "signed_in": False, "collections": [],
        "aliases": {DUPLICATE: SURVIVOR},
    }


def test_future_phone_revision_cannot_move_backwards(client, monkeypatch):
    import server

    _signed_in(monkeypatch, server)
    future = "2099-01-01T00:00:00.999999+00:00"
    cloud = CollectionCloud([_row(SURVIVOR, "Old name", future)])
    monkeypatch.setattr(server.sauth, "rest", cloud)

    response = client.patch(f"/api/collections/{SURVIVOR}", json={
        "name": "New name", "from": "Attic", "expected_updated_at": future,
    })

    assert response.status_code == 200
    updated = response.get_json()["collection"]["updated_at"]
    assert datetime.fromisoformat(updated) > datetime.fromisoformat(future)
    patch_path = next(call[1] for call in cloud.calls if call[0] == "PATCH")
    assert "updated_at=eq." in patch_path


def test_desktop_future_clock_cannot_poison_logical_collection_revision(
        monkeypatch):
    import server

    real_datetime = datetime

    class PoisonedDesktopClock:
        @classmethod
        def fromisoformat(cls, value):
            return real_datetime.fromisoformat(value)

        @classmethod
        def now(cls, _tz=None):
            return real_datetime.fromisoformat("2199-01-01T00:00:00+00:00")

    monkeypatch.setattr(server, "datetime", PoisonedDesktopClock)
    assert server._next_collection_timestamp(
        "2026-07-19T10:00:00.000001+00:00"
    ) == "2026-07-19T10:00:00.000002+00:00"


def test_stale_rename_loses_to_newer_delete(client, monkeypatch):
    import server

    _signed_in(monkeypatch, server)
    stale = "2026-07-19T10:00:00.000001+00:00"
    deleted = "2026-07-19T11:00:00.000001+00:00"
    cloud = CollectionCloud([_row(SURVIVOR, "Blue crate", deleted, deleted=True)])
    monkeypatch.setattr(server.sauth, "rest", cloud)

    response = client.patch(f"/api/collections/{SURVIVOR}", json={
        "name": "Resurrected", "expected_updated_at": stale,
    })

    assert response.status_code == 409
    body = response.get_json()
    assert body["conflict"] is True
    assert body["current"]["deleted"] is True
    assert cloud.rows[SURVIVOR]["name"] == "Blue crate"


def test_stale_delete_loses_to_newer_rename(client, monkeypatch):
    import server

    _signed_in(monkeypatch, server)
    stale = "2026-07-19T10:00:00.000001+00:00"
    renamed = "2026-07-19T11:00:00.000001+00:00"
    cloud = CollectionCloud([_row(SURVIVOR, "Renamed on phone", renamed)])
    monkeypatch.setattr(server.sauth, "rest", cloud)

    response = client.delete(f"/api/collections/{SURVIVOR}", json={
        "expected_updated_at": stale,
    })

    assert response.status_code == 409
    body = response.get_json()
    assert body["current"]["name"] == "Renamed on phone"
    assert body["current"]["deleted"] is False


def _seed_linked_entries(server, old_id):
    manual = {
        "m1": {"id": "m1", "title": "Manual", "extra": {
            "scan_collection_id": old_id,
            "scan_collection": "Blue crate",
            "scan_from": "Christopher Office",
        }},
    }
    checked = {"checked": [["ch:1", {"book": {"title": "Checked", "extra": {
        "scan_collection_id": old_id,
        "scan_collection": "Blue crate",
        "scan_from": "Christopher Office",
    }}}]]}
    server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, manual)
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, checked)


def _assert_only_ids_repointed(server, expected_id):
    manual = server.lib.load_json(server.lib.MANUAL_ENTRIES_PATH, {})["m1"]["extra"]
    checked = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})["checked"][0][1]["book"]["extra"]
    for extra in (manual, checked):
        assert extra == {
            "scan_collection_id": expected_id,
            "scan_collection": "Blue crate",
            "scan_from": "Christopher Office",
        }


def test_merge_tombstone_then_failed_save_continues_idempotently(
        client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    survivor_rev = "2026-07-19T10:00:00.000001+00:00"
    duplicate_rev = "2026-07-19T10:00:00.000002+00:00"
    cloud = CollectionCloud([
        _row(SURVIVOR, "Blue crate", survivor_rev),
        _row(DUPLICATE, "Blue crate", duplicate_rev),
    ])
    monkeypatch.setattr(server.sauth, "rest", cloud)
    _seed_linked_entries(server, DUPLICATE)
    real_repoint = server._repoint_collection_entries

    # The CAS wins, then a local save/process failure interrupts the repoint.
    monkeypatch.setattr(server, "_repoint_collection_entries",
                        lambda *_args: (_ for _ in ()).throw(RuntimeError("disk full")))
    payload = {
        "survivor_id": SURVIVOR, "duplicate_id": DUPLICATE,
        "survivor_updated_at": survivor_rev,
        "duplicate_updated_at": duplicate_rev,
    }
    failed = client.post("/api/collections/merge", json=payload)
    assert failed.status_code == 500
    assert cloud.rows[DUPLICATE]["deleted"] is True
    assert cloud.rows[DUPLICATE]["merged_into"] == SURVIVOR
    assert len([call for call in cloud.calls
                if call[1] == "rpc/merge_collections"]) == 1
    _assert_only_ids_repointed(server, DUPLICATE)

    monkeypatch.setattr(server, "_repoint_collection_entries", real_repoint)
    response = client.post("/api/collections/merge", json=payload)
    assert response.status_code == 200
    assert response.get_json()["continued"] is True
    assert response.get_json()["repointed"] == 2
    assert cloud.rows[DUPLICATE]["deleted"] is True
    _assert_only_ids_repointed(server, SURVIVOR)

    # Simulate an old local copy surfacing after the cloud tombstone. A retry
    # continues the merge, changes only identity, and does not need a new delete.
    _seed_linked_entries(server, DUPLICATE)
    before_rpc = len([call for call in cloud.calls
                      if call[1] == "rpc/merge_collections"])
    retried = client.post("/api/collections/merge", json=payload)
    assert retried.status_code == 200
    assert retried.get_json()["continued"] is True
    assert len([call for call in cloud.calls
                if call[1] == "rpc/merge_collections"]) == before_rpc + 1
    _assert_only_ids_repointed(server, SURVIVOR)

    # A capture queued on the offline phone before the merge may arrive later.
    # Its link follows the alias, while the frozen capture strings stay exact.
    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    entry_id, _ = server.ingest_capture({
        "id": "deaddead-1111-2222-3333-444444444444",
        "meta": {
            "title": "Late arrival",
            "scan_collection_id": DUPLICATE,
            "scan_collection": "Blue crate before merge",
            "scan_from": "Christopher Office",
        },
    }, [b"image"], "")
    late = server.lib.load_json(server.lib.MANUAL_ENTRIES_PATH, {})[entry_id]["extra"]
    assert late == {
        "scan_collection_id": SURVIVOR,
        "scan_collection": "Blue crate before merge",
        "scan_from": "Christopher Office",
    }


def test_merge_lost_cas_does_not_repoint_local_entries(client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    survivor_rev = "2026-07-19T10:00:00.000001+00:00"
    duplicate_rev = "2026-07-19T10:00:00.000002+00:00"
    cloud = CollectionCloud([
        _row(SURVIVOR, "Blue crate", survivor_rev),
        _row(DUPLICATE, "Blue crate", duplicate_rev),
    ])
    _seed_linked_entries(server, DUPLICATE)

    def lose_in_rpc(cfg, token, method, path, body=None, **kwargs):
        if path == "rpc/merge_collections":
            # Model the database's locked revision check rejecting a phone
            # rename that committed before the RPC acquired both rows.
            cloud.rows[DUPLICATE]["name"] = "Renamed on phone"
            cloud.rows[DUPLICATE]["updated_at"] = "2026-07-19T11:00:00+00:00"
            return None
        return cloud(cfg, token, method, path, body, **kwargs)

    monkeypatch.setattr(server.sauth, "rest", lose_in_rpc)
    response = client.post("/api/collections/merge", json={
        "survivor_id": SURVIVOR, "duplicate_id": DUPLICATE,
        "survivor_updated_at": survivor_rev,
        "duplicate_updated_at": duplicate_rev,
    })

    assert response.status_code == 409
    assert response.get_json()["current"]["name"] == "Renamed on phone"
    _assert_only_ids_repointed(server, DUPLICATE)


def test_merge_never_mistakes_normal_tombstone_for_retry(
        client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    survivor_rev = "2026-07-19T10:00:00.000001+00:00"
    duplicate_rev = "2026-07-19T10:00:00.000002+00:00"
    cloud = CollectionCloud([
        _row(SURVIVOR, "Blue crate", survivor_rev),
        _row(DUPLICATE, "Blue crate", "2026-07-19T11:00:00+00:00",
             deleted=True),
    ])
    monkeypatch.setattr(server.sauth, "rest", cloud)
    _seed_linked_entries(server, DUPLICATE)

    response = client.post("/api/collections/merge", json={
        "survivor_id": SURVIVOR, "duplicate_id": DUPLICATE,
        "survivor_updated_at": survivor_rev,
        "duplicate_updated_at": duplicate_rev,
    })

    assert response.status_code == 409
    assert "deleted, not merged" in response.get_json()["error"]
    assert cloud.rows[DUPLICATE]["merged_into"] is None
    assert server._resolve_collection_alias(DUPLICATE) == DUPLICATE
    _assert_only_ids_repointed(server, DUPLICATE)


def test_merge_conflict_adopts_other_desktops_authoritative_marker(
        client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    survivor_rev = "2026-07-19T10:00:00.000001+00:00"
    cloud = CollectionCloud([
        _row(SURVIVOR, "Blue crate", survivor_rev),
        _row(DUPLICATE, "Blue crate", "2026-07-19T12:00:00+00:00",
             deleted=True, merged_into=FINAL),
        _row(FINAL, "Blue crate", "2026-07-19T13:00:00+00:00"),
    ])
    monkeypatch.setattr(server.sauth, "rest", cloud)
    _seed_linked_entries(server, DUPLICATE)

    response = client.post("/api/collections/merge", json={
        "survivor_id": SURVIVOR, "duplicate_id": DUPLICATE,
        "survivor_updated_at": survivor_rev,
        "duplicate_updated_at": "stale-before-other-merge",
    })

    assert response.status_code == 409
    assert "another identity" in response.get_json()["error"]
    assert response.get_json()["aliases"] == {DUPLICATE: FINAL}
    assert server._resolve_collection_alias(DUPLICATE) == FINAL
    _assert_only_ids_repointed(server, FINAL)


def test_merge_rpc_rejects_survivor_change_without_tombstoning_duplicate(
        client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    survivor_rev = "2026-07-19T10:00:00.000001+00:00"
    duplicate_rev = "2026-07-19T10:00:00.000002+00:00"
    cloud = CollectionCloud([
        _row(SURVIVOR, "Blue crate", survivor_rev),
        _row(DUPLICATE, "Blue crate", duplicate_rev),
    ])
    _seed_linked_entries(server, DUPLICATE)

    def delete_survivor_in_rpc(cfg, token, method, path, body=None, **kwargs):
        if path == "rpc/merge_collections":
            cloud.rows[SURVIVOR]["deleted"] = True
            cloud.rows[SURVIVOR]["updated_at"] = "2026-07-19T11:00:00+00:00"
        return cloud(cfg, token, method, path, body, **kwargs)

    monkeypatch.setattr(server.sauth, "rest", delete_survivor_in_rpc)
    response = client.post("/api/collections/merge", json={
        "survivor_id": SURVIVOR, "duplicate_id": DUPLICATE,
        "survivor_updated_at": survivor_rev,
        "duplicate_updated_at": duplicate_rev,
    })

    assert response.status_code == 409
    assert response.get_json()["current"]["id"] == SURVIVOR
    assert cloud.rows[DUPLICATE]["deleted"] is False
    assert cloud.rows[DUPLICATE]["merged_into"] is None
    _assert_only_ids_repointed(server, DUPLICATE)


def test_marker_chain_retry_repoints_directly_to_final_identity(
        client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    cloud = CollectionCloud([
        _row(DUPLICATE, "Blue crate", "2026-07-19T12:00:00+00:00",
             deleted=True, merged_into=SURVIVOR),
        _row(SURVIVOR, "Blue crate", "2026-07-19T13:00:00+00:00",
             deleted=True, merged_into=FINAL),
        _row(FINAL, "Blue crate", "2026-07-19T14:00:00+00:00"),
    ])
    monkeypatch.setattr(server.sauth, "rest", cloud)
    _seed_linked_entries(server, DUPLICATE)

    response = client.post("/api/collections/merge", json={
        "survivor_id": SURVIVOR, "duplicate_id": DUPLICATE,
        "survivor_updated_at": "older-survivor-revision",
        "duplicate_updated_at": "older-duplicate-revision",
    })

    assert response.status_code == 200
    assert response.get_json()["continued"] is True
    assert response.get_json()["resolved_survivor_id"] == FINAL
    _assert_only_ids_repointed(server, FINAL)


def test_alias_refresh_keyset_pages_and_heals_cross_desktop_chain(
        monkeypatch, data_root):
    import server

    alias_path = data_root / "output" / "collection-alias-pages.json"
    monkeypatch.setattr(server, "COLLECTION_ALIASES_PATH", alias_path)
    monkeypatch.setattr(server, "_COLLECTION_PAGE_SIZE", 3)
    cloud = CollectionCloud([
        _row(DUPLICATE, "Blue crate", "2026-07-19T12:00:00+00:00",
             deleted=True, merged_into=SURVIVOR),
        _row(SURVIVOR, "Blue crate", "2026-07-19T13:00:00+00:00",
             deleted=True, merged_into=FINAL),
        _row(FINAL, "Blue crate", "2026-07-19T14:00:00+00:00"),
    ])
    _seed_linked_entries(server, DUPLICATE)

    def capped(cfg, token, method, path, body=None, **kwargs):
        result = cloud(cfg, token, method, path, body, **kwargs)
        # Simulate a PostgREST max_rows cap lower than the requested limit.
        if method == "GET" and path.startswith("collections?select="):
            return result[:1]
        return result

    monkeypatch.setattr(server.sauth, "rest", capped)
    rows = server._refresh_collection_aliases({}, "jwt")

    assert {row["id"] for row in rows} == {DUPLICATE, SURVIVOR, FINAL}
    assert len([call for call in cloud.calls
                if call[0] == "GET" and "select=" in call[1]]) == 4
    assert server._resolve_collection_alias(DUPLICATE) == FINAL
    assert server._resolve_collection_alias(SURVIVOR) == FINAL
    _assert_only_ids_repointed(server, FINAL)


def test_collection_list_returns_remote_aliases_for_loaded_browser_state(
        client, monkeypatch, data_root):
    import server

    _signed_in(monkeypatch, server)
    cloud = CollectionCloud([
        _row(DUPLICATE, "Old blue", "2026-07-19T12:00:00+00:00",
             deleted=True, merged_into=SURVIVOR),
        _row(SURVIVOR, "Blue", "2026-07-19T13:00:00+00:00",
             deleted=True, merged_into=FINAL),
        _row(FINAL, "Blue", "2026-07-19T14:00:00+00:00"),
    ])
    monkeypatch.setattr(server.sauth, "rest", cloud)
    _seed_linked_entries(server, DUPLICATE)

    response = client.get("/api/collections")

    assert response.status_code == 200
    body = response.get_json()
    assert [row["id"] for row in body["collections"]] == [FINAL]
    assert body["aliases"] == {DUPLICATE: FINAL, SURVIVOR: FINAL}
    _assert_only_ids_repointed(server, FINAL)


def test_malformed_alias_refresh_preserves_last_complete_cache(
        monkeypatch, data_root):
    import server

    alias_path = data_root / "output" / "collection-alias-preserve.json"
    monkeypatch.setattr(server, "COLLECTION_ALIASES_PATH", alias_path)
    server._remember_collection_alias(DUPLICATE, SURVIVOR)
    monkeypatch.setattr(server.sauth, "rest", lambda *_a, **_k: {"bad": True})

    try:
        server._refresh_collection_aliases({}, "jwt")
    except server.sauth.AuthError as exc:
        assert "malformed" in str(exc)
    else:
        raise AssertionError("malformed pagination must fail closed")

    assert server._resolve_collection_alias(DUPLICATE) == SURVIVOR


def test_stale_refresh_snapshot_cannot_erase_concurrent_merge_alias(
        monkeypatch, data_root):
    import server

    alias_path = data_root / "output" / "collection-alias-race.json"
    monkeypatch.setattr(server, "COLLECTION_ALIASES_PATH", alias_path)
    # Models an RPC caching D->S after a refresh query took its snapshot but
    # before that snapshot reaches the cache replacement boundary.
    server._remember_collection_alias(DUPLICATE, SURVIVOR)
    aliases = server._replace_collection_aliases([
        _row(SURVIVOR, "Blue crate", "2026-07-19T14:00:00+00:00"),
    ])

    assert aliases[DUPLICATE] == SURVIVOR
    assert server._resolve_collection_alias(DUPLICATE) == SURVIVOR
