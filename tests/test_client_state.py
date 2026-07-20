"""Characterization tests for GET/PUT /api/client_state and its shrink backup.

This suite is the regression test for the checked-books wipe recounted in
tools/README.md: a near-empty client once clobbered the full checked set, and
the server-side shrink backup (_backup_client_state, last 40 kept) is the
second safety net behind the client's adopt-by-merge. Every test pins CURRENT
behavior exactly — including the known holes (non-list "checked" is persisted
without a backup, top-level non-dict JSON 500s, an empty payload still
re-stamps the file, and a backup failure is completely silent). Do not "fix"
these here; changing them is a product decision.

All I/O happens inside the throwaway WHL_DATA_ROOT set by conftest.py. The
client-state file, backups/, and activity.jsonl persist across tests in the
session tmp dir, so each test resets them explicitly to stay order-independent.
"""
from __future__ import annotations

import json
import re
import shutil

URL = "/api/client_state"

# updated_at is stamped datetime.now(timezone.utc).isoformat(timespec="seconds")
UPDATED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

# client_state.autobak.<UTC %Y%m%dT%H%M%S_%f>.<old_n>to<new_n>.json
BACKUP_NAME_RE = re.compile(
    r"^client_state\.autobak\.\d{8}T\d{6}_\d{6}\.(\d+)to(\d+)\.json$"
)


def _state_path(data_root):
    return data_root / "output" / "client_state.json"


def _backups_dir(data_root):
    return data_root / "output" / "backups"


def _activity_path(data_root):
    return data_root / "output" / "activity.jsonl"


def _reset(data_root):
    """Session DATA_ROOT is shared across tests; start each test clean."""
    _state_path(data_root).unlink(missing_ok=True)
    _activity_path(data_root).unlink(missing_ok=True)
    shutil.rmtree(_backups_dir(data_root), ignore_errors=True)


def _backups(data_root):
    bdir = _backups_dir(data_root)
    if not bdir.is_dir():
        return []
    return sorted(bdir.glob("client_state.autobak.*.json"))


# --- GET ---------------------------------------------------------------------


def test_get_with_no_file_returns_empty_and_does_not_create_it(client, data_root):
    _reset(data_root)
    resp = client.get(URL)
    assert resp.status_code == 200
    assert resp.get_json() == {}
    # GET is read-only: it never materializes client_state.json
    assert not _state_path(data_root).exists()


# --- PUT round-trip ----------------------------------------------------------


def test_put_round_trip_whitelists_keys_and_stamps_updated_at(client, data_root):
    _reset(data_root)
    resp = client.put(
        URL,
        json={
            "checked": ["a", "b", "c", "d", "e"],
            "settings": {"theme": "dark"},
            "attention": {"bk1": True},
            "extra_key": "should-be-dropped",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    state = client.get(URL).get_json()
    # only _CLIENT_STATE_KEYS = ("checked", "settings", "attention") survive
    assert set(state) == {"checked", "settings", "attention", "updated_at"}
    assert state["checked"] == ["a", "b", "c", "d", "e"]
    assert state["settings"] == {"theme": "dark"}
    assert state["attention"] == {"bk1": True}
    assert UPDATED_AT_RE.match(state["updated_at"])

    # on-disk file under the isolated DATA_ROOT mirrors the GET body
    on_disk = json.loads(_state_path(data_root).read_text(encoding="utf-8"))
    assert on_disk == state


def test_legacy_azure_key_never_leaves_client_state_api(client, data_root):
    """Retiring a provider must not reclassify its stored credential as a pref."""
    _reset(data_root)
    path = _state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "settings": {"theme": "dark", "ocrAzureKey": "legacy-secret"},
    }), encoding="utf-8")

    # GET is safe even before the startup migration repairs the legacy file.
    assert client.get(URL).get_json()["settings"] == {"theme": "dark"}

    # A normal write strips the retired secret from the persisted blob too.
    response = client.put(URL, json={"settings": {
        "theme": "light", "ocrAzureKey": "must-not-persist",
    }})
    assert response.status_code == 200
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["settings"] == {"theme": "light"}


def test_partial_put_replaces_only_the_sent_top_level_key(client, data_root):
    _reset(data_root)
    client.put(
        URL,
        json={
            "checked": ["a", "b"],
            "settings": {"theme": "dark", "zoom": 2},
            "attention": {"bk1": True},
        },
    )
    client.put(URL, json={"settings": {"theme": "light"}})

    state = client.get(URL).get_json()
    assert state["checked"] == ["a", "b"]
    assert state["attention"] == {"bk1": True}
    # per-top-level-key REPLACE, not a deep merge: "zoom" is gone
    assert state["settings"] == {"theme": "light"}


# --- shrink guard ------------------------------------------------------------


def test_shrink_creates_backup_of_full_prewrite_state(client, data_root):
    _reset(data_root)
    client.put(
        URL,
        json={
            "checked": ["a", "b", "c", "d", "e"],
            "settings": {"theme": "dark"},
            "attention": {"bk1": True},
        },
    )
    pre_shrink = client.get(URL).get_json()

    resp = client.put(URL, json={"checked": ["a"]})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    baks = _backups(data_root)
    assert len(baks) == 1
    m = BACKUP_NAME_RE.match(baks[0].name)
    assert m, baks[0].name
    assert (m.group(1), m.group(2)) == ("5", "1")

    # backup content is the FULL pre-write state, not just the checked list
    saved = json.loads(baks[0].read_text(encoding="utf-8"))
    assert saved == pre_shrink
    assert saved["checked"] == ["a", "b", "c", "d", "e"]

    # the shrinking PUT is accepted as-is: no server-side merge or healing
    after = client.get(URL).get_json()
    assert after["checked"] == ["a"]
    assert after["settings"] == {"theme": "dark"}


def test_no_backup_on_growth_or_equal_length(client, data_root):
    _reset(data_root)
    client.put(URL, json={"checked": ["a", "b", "c"]})
    assert _backups(data_root) == []

    # growth: 3 -> 4
    client.put(URL, json={"checked": ["a", "b", "c", "d"]})
    assert _backups(data_root) == []

    # equal length with different items: still no backup (strict < only)
    client.put(URL, json={"checked": ["w", "x", "y", "z"]})
    assert _backups(data_root) == []
    assert client.get(URL).get_json()["checked"] == ["w", "x", "y", "z"]


def test_backup_pruning_keeps_newest_40(client, data_root):
    _reset(data_root)
    client.put(URL, json={"checked": ["a", "b", "c"]})

    bdir = _backups_dir(data_root)
    bdir.mkdir(parents=True, exist_ok=True)
    for i in range(1, 46):
        (bdir / f"client_state.autobak.20200101T000000_{i:06d}.9to1.json").write_text(
            "{}", encoding="utf-8"
        )
    assert len(_backups(data_root)) == 45

    # trigger one more shrink backup: 45 seeds + 1 fresh = 46, pruned to 40
    client.put(URL, json={"checked": ["a"]})
    baks = _backups(data_root)
    assert len(baks) == 40
    # prune order is lexicographic filename sort = chronological; the six
    # oldest seeds are unlinked, so the oldest survivor is seed 000007
    assert baks[0].name == "client_state.autobak.20200101T000000_000007.9to1.json"
    # the newest file is the real backup just written (2026 > 2020)
    assert BACKUP_NAME_RE.match(baks[-1].name).groups() == ("3", "1")


# --- malformed payloads ------------------------------------------------------


def test_unparseable_json_body_is_200_and_still_restamps_file(client, data_root):
    _reset(data_root)
    # plant a sentinel updated_at directly so the re-stamp is observable
    _state_path(data_root).parent.mkdir(parents=True, exist_ok=True)
    _state_path(data_root).write_text(
        json.dumps({"checked": ["a", "b"], "updated_at": "1999-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    resp = client.put(URL, data="this is not json", content_type="application/json")
    # get_json(silent=True) -> None -> payload {}: no keys applied, yet the
    # file is rewritten and updated_at re-stamped (the write is unconditional)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    state = client.get(URL).get_json()
    assert state["checked"] == ["a", "b"]
    assert state["updated_at"] != "1999-01-01T00:00:00+00:00"
    assert UPDATED_AT_RE.match(state["updated_at"])


def test_json_body_without_content_type_is_ignored(client, data_root):
    _reset(data_root)
    client.put(URL, json={"checked": ["a", "b"]})

    # valid JSON, but Flask won't parse it without the JSON content type
    resp = client.put(URL, data='{"checked": ["only"]}')
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert client.get(URL).get_json()["checked"] == ["a", "b"]


def test_json_null_body_is_ok_noop(client, data_root):
    _reset(data_root)
    client.put(URL, json={"checked": ["a"]})
    resp = client.put(URL, data="null", content_type="application/json")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert client.get(URL).get_json()["checked"] == ["a"]


def test_non_list_checked_is_persisted_without_backup(client, data_root):
    # Known hole, pinned as-is: the isinstance guard only skips the BACKUP for
    # a non-list "checked"; the copy loop has no type check, so the bad value
    # replaces the stored list and destroys it with no safety net.
    _reset(data_root)
    client.put(URL, json={"checked": ["a", "b", "c"]})

    resp = client.put(URL, json={"checked": "notalist"})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert client.get(URL).get_json()["checked"] == "notalist"
    assert _backups(data_root) == []

    # dict is persisted the same way
    client.put(URL, json={"checked": {"a": 1}})
    assert client.get(URL).get_json()["checked"] == {"a": 1}
    assert _backups(data_root) == []

    # once stored "checked" is a non-list it counts as length 0, so a list
    # PUT afterwards is a "growth" and still takes no backup
    client.put(URL, json={"checked": ["z"]})
    assert client.get(URL).get_json()["checked"] == ["z"]
    assert _backups(data_root) == []


def test_wrong_types_for_settings_and_attention_persist_verbatim(client, data_root):
    _reset(data_root)
    client.put(URL, json={"settings": ["not", "a", "dict"], "attention": 7})
    state = client.get(URL).get_json()
    assert state["settings"] == ["not", "a", "dict"]
    assert state["attention"] == 7


def test_top_level_non_dict_json_is_500_via_global_errorhandler(client, data_root):
    # payload.get() blows up on a list/str; the app-wide errorhandler turns
    # the AttributeError into a 500 {"ok": false} instead of a 400. Pinned.
    _reset(data_root)

    resp = client.put(URL, json=[1, 2, 3])
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "'list' object has no attribute 'get'",
    }

    resp = client.put(URL, json="hello")
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "'str' object has no attribute 'get'",
    }

    # neither request created the state file
    assert not _state_path(data_root).exists()


# --- silent backup failure (finding C28) --------------------------------------


def test_backup_failure_is_silent_and_write_still_lands(client, data_root, monkeypatch):
    # _backup_client_state swallows every exception (bare except: pass), so a
    # failed backup is invisible: the shrinking write is saved anyway and the
    # response is still {"ok": true}. Pinned as-is.
    _reset(data_root)
    client.put(URL, json={"checked": ["a", "b", "c"]})

    import libcommon as lib  # same module object as server.lib

    real_save_json = lib.save_json

    def failing_save_json(path, obj, *args, **kwargs):
        if "autobak" in str(path):
            raise OSError("disk full (simulated)")
        return real_save_json(path, obj, *args, **kwargs)

    monkeypatch.setattr(lib, "save_json", failing_save_json)

    resp = client.put(URL, json={"checked": []})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert client.get(URL).get_json()["checked"] == []
    assert _backups(data_root) == []


# --- activity side effect ------------------------------------------------------


def _pair(key, title=None):
    """A checked entry as the client stores it: a [key, value] pair, where a
    real value carries the book metadata the feed pulls titles from."""
    return [key, {"book": {"title": title}} if title else {}]


def test_activity_events_on_checked_delta(client, data_root):
    # Since the accounts change, checked-set activity is a KEY diff, not a
    # count diff: adds and removals log as separate events whose n counts key
    # changes, while detail names up to 3 titled books plus a "(+N more)"
    # overflow counting only the titled remainder.
    _reset(data_root)
    activity = _activity_path(data_root)

    # growth 0 -> 5 (4 of them titled): one "added" event, default actor
    client.put(URL, json={"checked": [
        _pair("a", "Alpha"), _pair("b", "Beta"), _pair("c", "Gamma"),
        _pair("d", "Delta"), _pair("e"),
    ]})
    lines = activity.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["actor"] == "Unnamed user"
    assert ev["verb"] == "added"
    assert ev["subject"] == "Checked Books"
    assert ev["n"] == 5
    assert ev["detail"] == "Alpha; Beta; Gamma (+1 more)"

    # same-length replace with disjoint keys: TWO events (added, then
    # removed) — each n agrees with the titles it names, no net delta
    client.put(URL, json={"checked": [
        _pair("v", "V"), _pair("w", "W"), _pair("x", "X"),
        _pair("y", "Y"), _pair("z", "Z"),
    ]})
    lines = activity.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    added, removed = json.loads(lines[1]), json.loads(lines[2])
    assert (added["verb"], added["n"]) == ("added", 5)
    assert added["detail"] == "V; W; X (+2 more)"
    assert (removed["verb"], removed["n"]) == ("removed", 5)
    assert removed["detail"] == "Alpha; Beta; Gamma (+1 more)"

    # non-list payload for "checked": no event
    client.put(URL, json={"settings": {"theme": "dark"}})
    assert len(activity.read_text(encoding="utf-8").splitlines()) == 3

    # shrink 5 -> 3 with a named actor: one "removed" event naming the losses
    client.put(
        URL,
        json={"checked": [_pair("v", "V"), _pair("w", "W"), _pair("x", "X")]},
        headers={"X-WHL-Actor": "Tester"},
    )
    lines = activity.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    ev = json.loads(lines[3])
    assert ev["actor"] == "Tester"
    assert (ev["verb"], ev["n"]) == ("removed", 2)
    assert ev["detail"] == "Y; Z"


def test_activity_ignores_malformed_checked_shapes(client, data_root):
    # Entries that are not [key, value] pairs (e.g. bare strings) are dropped
    # by the diff — the PUT persists them, but no activity event fires and
    # the feed file is never created. Pins the shape contract of the diff.
    _reset(data_root)
    client.put(URL, json={"checked": ["p", "q", "r"]})
    assert not _activity_path(data_root).exists()
