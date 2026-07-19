"""Tests for tools/store_sync.py — the sync channel for the files that left git.

The merge is pure (records + cloud rows + shadow ledger in, a plan out), so
most of these need no I/O at all. The round-trip tests run sync_store against
an in-memory fake of the Supabase transport, on the throwaway DATA_ROOT the
conftest provides — never against live data or a live project.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

import libcommon as lib
import store_sync as ss
from librarytool.engine.item_lifecycle import ItemLifecycleDeletionIndex

T0 = "2026-07-01T00:00:00+00:00"
T1 = "2026-07-02T00:00:00+00:00"
T2 = "2026-07-03T00:00:00+00:00"
NOW = "2026-07-04T00:00:00+00:00"

BUILDS = ss.STORES["builds"]


def rec(title, ts=T1):
    return {"title": title, "updated_at": ts}


def cloud_row(data, ts=T1, deleted=False):
    return {"data": data, "updated_at": ts, "deleted": deleted}


def shadow_of(data, ts=T1, dead=False):
    return {"h": None if dead else ss._hash(data), "ts": ts, "dead": dead}


# --- merge: the empty sides ------------------------------------------------------

def test_fresh_machine_pulls_everything():
    cloud = {"a": cloud_row(rec("A")), "b": cloud_row(rec("B"))}
    plan = ss.merge({}, cloud, {}, NOW, BUILDS)
    assert set(plan["pull"]) == {"a", "b"}
    assert not plan["push"] and not plan["tombstone"] and not plan["delete_local"]


def test_fresh_cloud_gets_everything_pushed():
    local = {"a": rec("A"), "b": rec("B")}
    plan = ss.merge(local, {}, {}, NOW, BUILDS)
    assert {p["key"] for p in plan["push"]} == {"a", "b"}
    # first push carries the record's own edit stamp, not sync time
    assert all(p["ts"] == T1 for p in plan["push"])
    assert not plan["pull"] and not plan["tombstone"]


def test_identical_sides_are_in_sync_and_seed_the_shadow():
    r = rec("A")
    plan = ss.merge({"a": r}, {"a": cloud_row(r)}, {}, NOW, BUILDS)
    assert plan["in_sync"] == 1
    assert plan["refresh"]["a"]["h"] == ss._hash(r)
    assert not plan["push"] and not plan["pull"]


# --- merge: edits and conflicts --------------------------------------------------

def test_local_edit_pushes():
    old, new = rec("A", T0), rec("A2", T1)
    plan = ss.merge({"a": new}, {"a": cloud_row(old, T0)},
                    {"a": shadow_of(old, T0)}, NOW, BUILDS)
    assert [p["key"] for p in plan["push"]] == ["a"]
    assert plan["push"][0]["data"] == new


def test_cloud_edit_pulls():
    old, new = rec("A", T0), rec("A2", T1)
    plan = ss.merge({"a": old}, {"a": cloud_row(new, T1)},
                    {"a": shadow_of(old, T0)}, NOW, BUILDS)
    assert list(plan["pull"]) == ["a"]


def test_both_changed_newest_wins_each_direction():
    base = rec("A", T0)
    shadow = {"a": shadow_of(base, T0)}
    # local newer
    plan = ss.merge({"a": rec("mine", T2)}, {"a": cloud_row(rec("theirs", T1), T1)},
                    shadow, NOW, BUILDS)
    assert [p["key"] for p in plan["push"]] == ["a"] and not plan["pull"]
    # cloud newer
    plan = ss.merge({"a": rec("mine", T1)}, {"a": cloud_row(rec("theirs", T2), T2)},
                    shadow, NOW, BUILDS)
    assert list(plan["pull"]) == ["a"] and not plan["push"]


def test_tie_goes_to_the_local_file():
    base = rec("A", T0)
    plan = ss.merge({"a": rec("mine", T1)}, {"a": cloud_row(rec("theirs", T1), T1)},
                    {"a": shadow_of(base, T0)}, NOW, BUILDS)
    assert [p["key"] for p in plan["push"]] == ["a"]


def test_stamp_that_did_not_move_falls_back_to_sync_time():
    # content changed but updated_at didn't (a hand edit): effective ts = now,
    # which beats the cloud's older edit
    old = rec("A", T0)
    plan = ss.merge({"a": {"title": "hand-edited", "updated_at": T0}},
                    {"a": cloud_row(rec("theirs", T1), T1)},
                    {"a": shadow_of(old, T0)}, NOW, BUILDS)
    assert [p["key"] for p in plan["push"]] == ["a"]
    assert plan["push"][0]["ts"] == NOW


# --- merge: deletes and tombstones ------------------------------------------------

def test_local_delete_tombstones_when_cloud_unchanged():
    old = rec("A", T0)
    plan = ss.merge({}, {"a": cloud_row(old, T0)}, {"a": shadow_of(old, T0)},
                    NOW, BUILDS)
    assert [t["key"] for t in plan["tombstone"]] == ["a"]
    assert plan["tombstone"][0]["data"] == old      # the payload survives


def test_delete_versus_edit_edit_wins():
    old = rec("A", T0)
    plan = ss.merge({}, {"a": cloud_row(rec("A2", T1), T1)},
                    {"a": shadow_of(old, T0)}, NOW, BUILDS)
    assert list(plan["pull"]) == ["a"] and not plan["tombstone"]


def test_cloud_tombstone_deletes_locally():
    old = rec("A", T0)
    plan = ss.merge({"a": old}, {"a": cloud_row(old, T1, deleted=True)},
                    {"a": shadow_of(old, T0)}, NOW, BUILDS)
    assert [k for k, _ in plan["delete_local"]] == ["a"]


def test_local_edit_newer_than_tombstone_resurrects():
    old = rec("A", T0)
    plan = ss.merge({"a": rec("A2", T2)}, {"a": cloud_row(old, T1, deleted=True)},
                    {"a": shadow_of(old, T0)}, NOW, BUILDS)
    assert [p["key"] for p in plan["push"]] == ["a"]
    assert not plan["delete_local"]


def test_tombstone_on_both_sides_just_forgets():
    plan = ss.merge({}, {"a": cloud_row(rec("A"), T1, deleted=True)},
                    {"a": shadow_of(rec("A"), T0)}, NOW, BUILDS)
    assert plan["shadow_drop"] == ["a"]
    assert not plan["tombstone"] and not plan["delete_local"]


def test_shadow_only_key_is_dropped():
    plan = ss.merge({}, {}, {"a": shadow_of(rec("A"))}, NOW, BUILDS)
    assert plan["shadow_drop"] == ["a"]


# --- merge: the wipe guard ---------------------------------------------------------

def test_wiped_local_file_is_restored_not_propagated():
    recs = {k: rec(k.upper(), T0) for k in ("a", "b", "c", "d")}
    cloud = {k: cloud_row(r, T0) for k, r in recs.items()}
    shadow = {k: shadow_of(r, T0) for k, r in recs.items()}
    plan = ss.merge({}, cloud, shadow, NOW, BUILDS)     # local: empty
    assert plan["guard"]
    assert set(plan["pull"]) == set(recs)
    assert not plan["tombstone"]


def test_a_few_real_deletes_do_not_trip_the_guard():
    recs = {k: rec(k.upper(), T0) for k in ("a", "b", "c", "d", "e")}
    cloud = {k: cloud_row(r, T0) for k, r in recs.items()}
    shadow = {k: shadow_of(r, T0) for k, r in recs.items()}
    local = {k: recs[k] for k in ("a", "b", "c")}       # deleted d and e
    plan = ss.merge(local, cloud, shadow, NOW, BUILDS)
    assert not plan["guard"]
    assert {t["key"] for t in plan["tombstone"]} == {"d", "e"}


# --- the corrections codec ---------------------------------------------------------

def test_corrections_decompose_assigns_stable_ids():
    doc = {"added": [{"title": "New Book"}],
           "edits": {"10": {"title": "Fixed"}, "3": {}}}
    records, assigned = ss._corr_decompose(doc)
    assert assigned is True
    rid = doc["added"][0]["id"]                # written back into the doc
    assert set(records) == {f"add:{rid}", "edit:10"}   # empty edit dropped
    records2, assigned2 = ss._corr_decompose(doc)
    assert assigned2 is False and set(records2) == set(records)


def test_corrections_recompose_round_trip_and_ordering():
    doc = {"added": [{"title": "First", "id": "aaa"},
                     {"title": "Second", "id": "bbb"}],
           "edits": {"5": {"title": "Fixed"}}}
    records, _ = ss._corr_decompose(doc)
    records["add:zzz"] = {"title": "From the cloud"}   # a pulled row, id-less
    out = ss._corr_recompose(doc, records)
    assert [r["title"] for r in out["added"]] == ["First", "Second", "From the cloud"]
    assert out["added"][2]["id"] == "zzz"              # key names the identity
    assert out["edits"] == {"5": {"title": "Fixed"}}


# --- the IA catalog scrub -----------------------------------------------------------

def test_ia_scrub_strips_preview_and_normalizes_saved_as():
    entry = {"identifier": "x", "saved_as": "downloads\\ia\\x.pdf",
             "preview": "downloads\\cache\\previews\\p.pdf", "book": {"t": 1}}
    s = ss._ia_scrub(entry)
    assert "preview" not in s
    assert s["saved_as"] == "downloads/ia/x.pdf"
    assert entry["preview"]                     # the original is untouched


def test_ia_adopt_keeps_the_local_preview():
    old = {"identifier": "x", "preview": "downloads/cache/previews/mine.pdf"}
    incoming = {"identifier": "x", "saved_as": "downloads/ia/x.pdf"}
    out = ss._ia_adopt(old, incoming)
    assert out["preview"] == old["preview"]
    assert ss._ia_adopt(None, incoming) == incoming


def test_ia_preview_regen_is_not_a_change():
    # the scrubbed record hashes identically whichever preview a machine holds
    a = {"identifier": "x", "preview": "one.pdf", "saved_as": "downloads/ia/x.pdf"}
    b = {"identifier": "x", "preview": "two.pdf", "saved_as": "downloads\\ia\\x.pdf"}
    assert ss._hash(ss._ia_scrub(a)) == ss._hash(ss._ia_scrub(b))


# --- entry files: the plan -----------------------------------------------------------

def test_entries_plan_pushes_pulls_and_never_deletes():
    local = {"b1/ocr/compiled.txt": {"size": 10, "mtime": 100.0}}
    remote = {"b2/preview.pdf": {"size": 5, "etag": "e", "modified": T0}}
    p = ss.entries_plan(local, remote, {})
    assert p == {"push": ["b1/ocr/compiled.txt"], "pull": ["b2/preview.pdf"],
                 "same": []}


def test_entries_plan_md5_match_is_in_sync_even_when_sizes_agree():
    both = {"f.txt": {"size": 10, "mtime": 100.0}}
    remote = {"f.txt": {"size": 10, "etag": "abc", "modified": T0}}
    assert ss.entries_plan(both, remote, {"f.txt": "abc"})["same"] == ["f.txt"]


def test_entries_plan_content_diff_direction_by_time():
    remote = {"f.txt": {"size": 10, "etag": "abc", "modified": T1}}
    older = ss._parse_ts(T0).timestamp()
    newer = ss._parse_ts(T2).timestamp()
    p = ss.entries_plan({"f.txt": {"size": 10, "mtime": newer}}, remote,
                        {"f.txt": "different"})
    assert p["push"] == ["f.txt"]
    p = ss.entries_plan({"f.txt": {"size": 10, "mtime": older}}, remote,
                        {"f.txt": "different"})
    assert p["pull"] == ["f.txt"]


def test_entries_plan_time_tie_pushes():
    remote = {"f.txt": {"size": 10, "etag": "abc", "modified": T1}}
    tie = ss._parse_ts(T1).timestamp()
    p = ss.entries_plan({"f.txt": {"size": 10, "mtime": tie}}, remote,
                        {"f.txt": "different"})
    assert p["push"] == ["f.txt"]                # ties: the local file wins


def test_sync_entry_files_backs_up_before_a_pull_overwrite(monkeypatch):
    import os

    base = ss.entries_dir()
    f = base / "b1" / "ocr" / "compiled.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("local work", "utf-8")
    old = ss._parse_ts(T0).timestamp()
    os.utime(f, (old, old))                      # the cloud copy is newer

    monkeypatch.setattr(ss.r2, "list_objects_meta",
                        lambda cfg, prefix="", timeout=60.0: {
                            "entries/b1/ocr/compiled.txt":
                                {"size": 13, "etag": "d" * 32, "modified": T2}})

    def fake_get(cfg, key, dest, **kw):
        dest.write_text("cloud version", "utf-8")
        return dest
    monkeypatch.setattr(ss.r2, "get_file", fake_get)

    res = ss.sync_entry_files({"account": "a", "bucket": "b",
                               "key_id": "k", "secret": "s"})
    assert res == {"pushed": 0, "pulled": 1, "in_sync": 0}
    assert f.read_text("utf-8") == "cloud version"
    bak = lib.OUTPUT_DIR / "backups" / "entries" / "b1" / "ocr" / "compiled.txt"
    assert bak.read_text("utf-8") == "local work"


def test_sync_entry_files_suppresses_remote_only_deleted_item(monkeypatch):
    transfers = []
    monkeypatch.setattr(ss.r2, "list_objects_meta",
                        lambda cfg, prefix="", timeout=60.0: {
                            "entries/deleted/ocr/compiled.txt": {
                                "size": 7, "etag": "a" * 32, "modified": T1,
                            },
                            "entries/deleted-extra/ocr/compiled.txt": {
                                "size": 4, "etag": "b" * 32, "modified": T1,
                            },
                        })

    def fake_get(cfg, key, dest, **kw):
        transfers.append(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("live", "utf-8")
        return dest

    monkeypatch.setattr(ss.r2, "get_file", fake_get)
    result = ss.sync_entry_files(
        {"account": "a", "bucket": "b", "key_id": "k", "secret": "s"},
        allow_item=lambda item_id: item_id != "deleted",
    )

    assert result == {"pushed": 0, "pulled": 1, "in_sync": 0,
                      "suppressed": 1}
    assert transfers == ["entries/deleted-extra/ocr/compiled.txt"]
    assert not (ss.entries_dir() / "deleted").exists()
    assert (ss.entries_dir() / "deleted-extra" / "ocr" /
            "compiled.txt").read_text("utf-8") == "live"


def test_sync_entry_files_suppresses_local_only_deleted_item(monkeypatch):
    source = ss.entries_dir() / "deleted" / "ocr" / "compiled.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("local", "utf-8")
    monkeypatch.setattr(ss.r2, "list_objects_meta",
                        lambda cfg, prefix="", timeout=60.0: {})
    transfers = []
    monkeypatch.setattr(
        ss.r2, "put_file",
        lambda cfg, key, path, **kw: transfers.append((key, path)),
    )

    result = ss.sync_entry_files(
        {"account": "a", "bucket": "b", "key_id": "k", "secret": "s"},
        allow_item=lambda item_id: item_id != "deleted",
    )

    assert result == {"pushed": 0, "pulled": 0, "in_sync": 0,
                      "suppressed": 1}
    assert transfers == []
    assert source.read_text("utf-8") == "local"


def test_sync_entry_files_rechecks_policy_before_push(monkeypatch):
    source = ss.entries_dir() / "book" / "ocr" / "compiled.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("local", "utf-8")
    monkeypatch.setattr(ss.r2, "list_objects_meta",
                        lambda cfg, prefix="", timeout=60.0: {})
    transfers = []
    monkeypatch.setattr(
        ss.r2, "put_file",
        lambda cfg, key, path, **kw: transfers.append((key, path)),
    )
    checks = 0

    def allow_item(item_id):
        nonlocal checks
        assert item_id == "book"
        checks += 1
        return checks == 1       # inventory allowed; pre-publication denied

    result = ss.sync_entry_files(
        {"account": "a", "bucket": "b", "key_id": "k", "secret": "s"},
        allow_item=allow_item,
    )

    assert result == {"pushed": 0, "pulled": 0, "in_sync": 0,
                      "suppressed": 1}
    assert checks == 2
    assert transfers == []
    assert source.read_text("utf-8") == "local"


def test_sync_entry_files_rechecks_policy_before_pull_footprint(monkeypatch):
    monkeypatch.setattr(ss.r2, "list_objects_meta",
                        lambda cfg, prefix="", timeout=60.0: {
                            "entries/book/ocr/compiled.txt": {
                                "size": 5, "etag": "c" * 32, "modified": T1,
                            },
                        })
    transfers = []
    monkeypatch.setattr(
        ss.r2, "get_file",
        lambda cfg, key, dest, **kw: transfers.append((key, dest)),
    )
    checks = 0

    def allow_item(item_id):
        nonlocal checks
        assert item_id == "book"
        checks += 1
        return checks == 1       # inventory allowed; pre-publication denied

    result = ss.sync_entry_files(
        {"account": "a", "bucket": "b", "key_id": "k", "secret": "s"},
        allow_item=allow_item,
    )

    assert result == {"pushed": 0, "pulled": 0, "in_sync": 0,
                      "suppressed": 1}
    assert checks == 2
    assert transfers == []
    assert not (ss.entries_dir() / "book").exists()
    assert not (lib.OUTPUT_DIR / "backups" / "entries" / "book").exists()


def test_sync_entry_files_fails_closed_for_invalid_lifecycle_policy(monkeypatch):
    monkeypatch.setattr(ss.r2, "list_objects_meta",
                        lambda cfg, prefix="", timeout=60.0: {
                            "entries/book/ocr/compiled.txt": {
                                "size": 5, "etag": "c" * 32, "modified": T1,
                            },
                        })

    with pytest.raises(TypeError, match="allow_item must be callable"):
        ss.sync_entry_files({}, allow_item=True)
    with pytest.raises(TypeError, match="return a boolean"):
        ss.sync_entry_files({}, allow_item=lambda _item_id: "yes")


def test_sync_entry_files_holds_policy_guard_through_transfer(monkeypatch):
    source = ss.entries_dir() / "book" / "ocr" / "compiled.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("local", "utf-8")
    monkeypatch.setattr(
        ss.r2, "list_objects_meta", lambda _cfg, prefix="": {}
    )
    held = False
    events = []

    @contextmanager
    def policy_guard():
        nonlocal held
        assert held is False
        held = True
        events.append("enter")
        try:
            yield lambda item_id: item_id != "deleted"
        finally:
            held = False
            events.append("exit")

    def guarded_put(_cfg, key, path, **_kwargs):
        assert held is True
        assert key == "entries/book/ocr/compiled.txt"
        assert path == source

    monkeypatch.setattr(ss.r2, "put_file", guarded_put)
    result = ss.sync_entry_files({}, item_policy_guard=policy_guard)

    assert result == {
        "pushed": 1,
        "pulled": 0,
        "in_sync": 0,
        "suppressed": 0,
    }
    assert held is False
    assert events == ["enter", "exit"] * 2


def test_sync_entry_files_rejects_ambiguous_or_invalid_policy_guards(
    monkeypatch,
):
    monkeypatch.setattr(
        ss.r2, "list_objects_meta", lambda _cfg, prefix="": {}
    )
    with pytest.raises(TypeError, match="mutually exclusive"):
        ss.sync_entry_files(
            {},
            allow_item=lambda _item_id: True,
            item_policy_guard=lambda: None,
        )
    with pytest.raises(TypeError, match="must be callable"):
        ss.sync_entry_files({}, item_policy_guard=True)
    with pytest.raises(TypeError, match="context manager"):
        ss.sync_entry_files({}, item_policy_guard=lambda: object())

    @contextmanager
    def invalid_policy():
        yield True

    with pytest.raises(TypeError, match="yield a callable"):
        ss.sync_entry_files({}, item_policy_guard=invalid_policy)


def test_entries_plan_multipart_etag_falls_back_to_size():
    remote = {"f.pdf": {"size": 10, "etag": "abc-2", "modified": T1}}
    p = ss.entries_plan({"f.pdf": {"size": 10, "mtime": 0.0}}, remote,
                        {"f.pdf": "whatever"})
    assert p["same"] == ["f.pdf"]


def test_local_entry_files_skips_write_temporaries(tmp_path):
    (tmp_path / "b1" / "ocr").mkdir(parents=True)
    (tmp_path / "b1" / "ocr" / "compiled.txt").write_text("x", "utf-8")
    (tmp_path / "b1" / "ocr" / "compiled.txt.tmp123").write_text("x", "utf-8")
    (tmp_path / "b1" / "preview.pdf.part").write_bytes(b"x")
    assert list(ss.local_entry_files(tmp_path)) == ["b1/ocr/compiled.txt"]


def test_unsafe_bucket_keys_are_rejected():
    assert not ss._safe_rel("../../etc/passwd")
    assert not ss._safe_rel("a/../b")
    assert not ss._safe_rel("a\\b")
    assert not ss._safe_rel("/abs")
    assert ss._safe_rel("b1/ocr/compiled.txt")


# --- sync_store round trip against a fake cloud --------------------------------------

class FakeCloud:
    """supabase_sync's two store verbs, backed by a dict."""

    def __init__(self):
        self.tables: dict[str, dict[str, dict]] = {}

    def list_store_rows(self, cfg, table, pk):
        return [dict(r) for r in self.tables.get(table, {}).values()]

    def upsert_store_rows(self, cfg, table, pk, rows, chunk=200):
        t = self.tables.setdefault(table, {})
        for r in rows:
            t[str(r[pk])] = dict(r)
        return len(rows)


@pytest.fixture()
def fake_cloud(monkeypatch):
    fake = FakeCloud()
    monkeypatch.setattr(ss.sbase, "list_store_rows", fake.list_store_rows)
    monkeypatch.setattr(ss.sbase, "upsert_store_rows", fake.upsert_store_rows)
    return fake


@pytest.fixture(autouse=True)
def clean_store_files():
    """These tests share the session DATA_ROOT: start (and leave) it clean."""
    import shutil

    def wipe():
        for p in (ss.STORES["builds"]["path"](), ss.STORES["ia_catalog"]["path"](),
                  ss.STORES["corrections"]["path"](), ss.SHADOW_PATH):
            p.unlink(missing_ok=True)
        shutil.rmtree(ss.entries_dir(), ignore_errors=True)
        shutil.rmtree(lib.OUTPUT_DIR / "backups" / "entries", ignore_errors=True)
    wipe()
    yield
    wipe()


def test_sync_store_policy_suppresses_remote_only_deleted_catalogue_row(fake_cloud):
    fake_cloud.tables["builds"] = {
        "deleted": {
            "id": "deleted",
            "data": {"id": "deleted", "title": "Remote", "updated_at": T1},
            "updated_at": T1,
            "deleted": False,
        },
    }

    result = ss.sync_store(
        {"url": "u", "key": "k"},
        "builds",
        allow_item=lambda item_id: item_id != "deleted",
    )

    assert result == {"pushed": 0, "pulled": 0, "tombstoned": 0,
                      "deleted": 0, "in_sync": 0, "guard": ""}
    assert not ss.STORES["builds"]["path"]().exists()
    assert fake_cloud.tables["builds"]["deleted"]["deleted"] is False


def test_sync_store_policy_suppresses_local_only_deleted_catalogue_row(fake_cloud):
    path = ss.STORES["builds"]["path"]()
    record = {"id": "deleted", "title": "Local", "updated_at": T1}
    lib.save_json(path, {"deleted": record})

    result = ss.sync_store(
        {"url": "u", "key": "k"},
        "builds",
        allow_item=lambda item_id: item_id != "deleted",
    )

    assert result == {"pushed": 0, "pulled": 0, "tombstoned": 0,
                      "deleted": 0, "in_sync": 0, "guard": ""}
    assert fake_cloud.tables.get("builds", {}) == {}
    assert lib.load_json(path, {}) == {"deleted": record}


def test_sync_store_policy_uses_exact_case_sensitive_catalogue_identity(fake_cloud):
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {
        "Book": {"id": "Book", "title": "Exact", "updated_at": T1},
    })
    seen = []

    def allow_item(item_id):
        seen.append(item_id)
        return item_id != "Book"

    result = ss.sync_store(
        {"url": "u", "key": "k"},
        "builds",
        allow_item=allow_item,
    )

    assert result["pushed"] == 0
    assert seen == ["Book"]
    assert fake_cloud.tables.get("builds", {}) == {}


def test_sync_store_rechecks_policy_before_local_catalogue_write(fake_cloud):
    fake_cloud.tables["builds"] = {
        "book": {
            "id": "book",
            "data": {"id": "book", "title": "Remote", "updated_at": T1},
            "updated_at": T1,
            "deleted": False,
        },
    }
    checks = 0

    def allow_item(item_id):
        nonlocal checks
        assert item_id == "book"
        checks += 1
        return checks == 1       # planning allowed; pre-publication denied

    result = ss.sync_store(
        {"url": "u", "key": "k"}, "builds", allow_item=allow_item,
    )

    assert result["pulled"] == 0
    assert checks == 2
    assert not ss.STORES["builds"]["path"]().exists()


def test_sync_store_rechecks_policy_before_cloud_catalogue_write(fake_cloud):
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {
        "book": {"id": "book", "title": "Local", "updated_at": T1},
    })
    checks = 0

    def allow_item(item_id):
        nonlocal checks
        assert item_id == "book"
        checks += 1
        return checks == 1       # planning allowed; pre-publication denied

    result = ss.sync_store(
        {"url": "u", "key": "k"}, "builds", allow_item=allow_item,
    )

    assert result["pushed"] == 0
    assert checks == 2
    assert fake_cloud.tables.get("builds", {}) == {}


def test_sync_store_policy_fails_closed_for_invalid_values(fake_cloud):
    path = ss.STORES["builds"]["path"]()
    record = {"id": "book", "title": "Local", "updated_at": T1}
    lib.save_json(path, {"book": record})

    with pytest.raises(TypeError, match="allow_item must be callable"):
        ss.sync_store({"url": "u", "key": "k"}, "builds", allow_item=True)
    with pytest.raises(TypeError, match="return a boolean"):
        ss.sync_store(
            {"url": "u", "key": "k"},
            "builds",
            allow_item=lambda _item_id: "yes",
        )

    assert fake_cloud.tables.get("builds", {}) == {}
    assert lib.load_json(path, {}) == {"book": record}


def test_sync_stores_validates_policy_before_any_store_io(fake_cloud, monkeypatch):
    reads = []

    def observe_read(cfg, table, pk):
        reads.append(table)
        return []

    monkeypatch.setattr(ss.sbase, "list_store_rows", observe_read)
    with pytest.raises(TypeError, match="allow_item must be callable"):
        ss.sync_stores({"url": "u", "key": "k"}, allow_item={"book"})
    assert reads == []


def test_sync_stores_applies_item_policy_only_to_build_catalogue(fake_cloud):
    lib.save_json(ss.STORES["builds"]["path"](), {
        "deleted": {"id": "deleted", "title": "Build", "updated_at": T1},
    })
    lib.save_json(ss.STORES["ia_catalog"]["path"](), {
        "source": {"identifier": "source", "title": "IA",
                   "downloaded_at": T1},
    })
    seen = []

    def allow_item(item_id):
        seen.append(item_id)
        return item_id != "deleted"

    result = ss.sync_stores(
        {"url": "u", "key": "k"}, allow_item=allow_item,
    )

    assert result["builds"]["pushed"] == 0
    assert result["ia_catalog"]["pushed"] == 1
    assert seen == ["deleted"]
    assert fake_cloud.tables.get("builds", {}) == {}
    assert fake_cloud.tables["ia_catalog"]["source"]["data"]["title"] == "IA"


def test_sync_store_policy_requires_outer_gate_for_atomicity(fake_cloud, monkeypatch):
    """A callback cannot close the interval after its final allowed result."""
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {
        "book": {"id": "book", "title": "Local", "updated_at": T1},
    })
    live = True
    upsert = fake_cloud.upsert_store_rows

    def race_after_final_check(cfg, table, pk, rows, chunk=200):
        nonlocal live
        live = False
        return upsert(cfg, table, pk, rows, chunk=chunk)

    monkeypatch.setattr(ss.sbase, "upsert_store_rows", race_after_final_check)
    result = ss.sync_store(
        {"url": "u", "key": "k"},
        "builds",
        allow_item=lambda _item_id: live,
    )

    # The lifecycle change happened after the final callback result.  Server
    # integration must prevent this race with the shared outer workspace gate.
    assert result["pushed"] == 1
    assert fake_cloud.tables["builds"]["book"]["data"]["title"] == "Local"
    assert "lifecycle/workspace gate" in (ss.sync_store.__doc__ or "")


def test_sync_store_policy_guard_holds_each_catalogue_publication_phase(
    fake_cloud,
    monkeypatch,
):
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {
        "book": {"id": "book", "title": "Local", "updated_at": T1},
    })
    fake_cloud.tables["builds"] = {
        "remote": {
            "id": "remote",
            "data": {"id": "remote", "title": "Cloud", "updated_at": T2},
            "updated_at": T2,
            "deleted": False,
        },
    }
    held = False
    events = []
    upsert = fake_cloud.upsert_store_rows
    save_json = ss.lib.save_json

    @contextmanager
    def policy_guard():
        nonlocal held
        assert held is False
        held = True
        events.append("enter")
        try:
            yield lambda item_id: item_id != "deleted"
        finally:
            held = False
            events.append("exit")

    def guarded_upsert(cfg, table, pk, rows, chunk=200):
        assert held is True
        return upsert(cfg, table, pk, rows, chunk=chunk)

    def guarded_save(target, value):
        assert held is True
        return save_json(target, value)

    monkeypatch.setattr(ss.sbase, "upsert_store_rows", guarded_upsert)
    monkeypatch.setattr(ss.lib, "save_json", guarded_save)
    result = ss.sync_store(
        {"url": "u", "key": "k"},
        "builds",
        item_policy_guard=policy_guard,
    )

    assert result["pushed"] == result["pulled"] == 1
    assert fake_cloud.tables["builds"]["book"]["data"]["title"] == "Local"
    assert lib.load_json(path, {})["remote"]["title"] == "Cloud"
    assert held is False
    assert events == ["enter", "exit"] * 2


def test_sync_stores_validates_policy_guard_before_any_store_io(
    fake_cloud,
    monkeypatch,
):
    reads = []

    def observe_read(_cfg, table, _pk):
        reads.append(table)
        return []

    monkeypatch.setattr(ss.sbase, "list_store_rows", observe_read)
    with pytest.raises(TypeError, match="item_policy_guard must be callable"):
        ss.sync_stores(
            {"url": "u", "key": "k"}, item_policy_guard=True
        )
    with pytest.raises(TypeError, match="mutually exclusive"):
        ss.sync_stores(
            {"url": "u", "key": "k"},
            allow_item=lambda _item_id: True,
            item_policy_guard=lambda: None,
        )
    with pytest.raises(ValueError, match="second builds lock"):
        ss.sync_stores(
            {"url": "u", "key": "k"},
            locks={"builds": object()},
            item_policy_guard=lambda: None,
        )
    with pytest.raises(ValueError, match="second builds lock"):
        ss.sync_store(
            {"url": "u", "key": "k"},
            "builds",
            lock=object(),
            item_policy_guard=lambda: None,
        )
    assert reads == []


def test_sync_store_full_cycle(fake_cloud):
    path = ss.STORES["builds"]["path"]()

    # 1. a local build is pushed
    lib.save_json(path, {"b1": {"id": "b1", "title": "One", "updated_at": T1}})
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res["pushed"] == 1 and not res["guard"]
    assert fake_cloud.tables["builds"]["b1"]["data"]["title"] == "One"

    # 2. nothing changed: the next pass is a no-op
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res == {"pushed": 0, "pulled": 0, "tombstoned": 0, "deleted": 0,
                   "in_sync": 1, "guard": ""}

    # 3. another machine pushes b2: it gets pulled
    fake_cloud.tables["builds"]["b2"] = {
        "id": "b2", "data": {"id": "b2", "title": "Two", "updated_at": T2},
        "updated_at": T2, "deleted": False}
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res["pulled"] == 1
    assert lib.load_json(path, {})["b2"]["title"] == "Two"

    # 4. deleting b1 locally tombstones it in the cloud, data intact
    doc = lib.load_json(path, {})
    del doc["b1"]
    lib.save_json(path, doc)
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res["tombstoned"] == 1
    row = fake_cloud.tables["builds"]["b1"]
    assert row["deleted"] is True and row["data"]["title"] == "One"

    # 5. the tombstone deletes b1 on "another machine" (fresh shadow via wipe
    #    of ours would complicate this; here the same shadow just converges)
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res == {"pushed": 0, "pulled": 0, "tombstoned": 0, "deleted": 0,
                   "in_sync": 1, "guard": ""}


def test_sync_store_pull_overwrite_writes_a_backup(fake_cloud):
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {"b1": {"id": "b1", "title": "Old", "updated_at": T0}})
    ss.sync_store({"url": "u", "key": "k"}, "builds")
    # the cloud row moves ahead; pulling it must snapshot the file first
    fake_cloud.tables["builds"]["b1"] = {
        "id": "b1", "data": {"id": "b1", "title": "New", "updated_at": T2},
        "updated_at": T2, "deleted": False}
    before = set((lib.OUTPUT_DIR / "backups").glob("whl_builds.json.presync.*"))
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res["pulled"] == 1
    assert lib.load_json(path, {})["b1"]["title"] == "New"
    after = set((lib.OUTPUT_DIR / "backups").glob("whl_builds.json.presync.*"))
    new_baks = after - before
    assert len(new_baks) == 1
    saved = json.loads(new_baks.pop().read_text("utf-8"))
    assert saved["b1"]["title"] == "Old"


def test_sync_store_wipe_guard_restores_locally(fake_cloud):
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {f"b{i}": {"id": f"b{i}", "title": str(i),
                                   "updated_at": T1} for i in range(4)})
    ss.sync_store({"url": "u", "key": "k"}, "builds")
    lib.save_json(path, {})                          # the disaster
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res["guard"]
    assert res["tombstoned"] == 0 and res["pulled"] == 4
    assert len(lib.load_json(path, {})) == 4         # restored from the cloud
    assert all(not r["deleted"] for r in fake_cloud.tables["builds"].values())


def test_sync_store_failed_push_retries_next_pass(fake_cloud, monkeypatch):
    path = ss.STORES["builds"]["path"]()
    lib.save_json(path, {"b1": {"id": "b1", "title": "One", "updated_at": T1}})

    def boom(cfg, table, pk, rows, chunk=200):
        raise ss.sbase.SyncError("HTTP 500")
    monkeypatch.setattr(ss.sbase, "upsert_store_rows", boom)
    with pytest.raises(ss.sbase.SyncError):
        ss.sync_store({"url": "u", "key": "k"}, "builds")
    monkeypatch.setattr(ss.sbase, "upsert_store_rows", fake_cloud.upsert_store_rows)
    res = ss.sync_store({"url": "u", "key": "k"}, "builds")
    assert res["pushed"] == 1                        # the shadow never advanced


def test_sync_stores_isolates_a_failing_store(fake_cloud, monkeypatch):
    def boom(cfg, table, pk):
        if table == "builds":
            raise ss.sbase.SyncError("HTTP 404: relation does not exist")
        return []
    monkeypatch.setattr(ss.sbase, "list_store_rows", boom)
    out = ss.sync_stores({"url": "u", "key": "k"})
    assert "error" in out["builds"]
    assert out["ia_catalog"] == {"pushed": 0, "pulled": 0, "tombstoned": 0,
                                 "deleted": 0, "in_sync": 0, "guard": ""}


def test_server_cloud_sync_policy_guard_holds_casefold_index(monkeypatch):
    import server

    held = False
    events = []

    class Lifecycle:
        @contextmanager
        def deletion_index_guard(self):
            nonlocal held
            assert held is False
            held = True
            events.append("enter")
            try:
                yield ItemLifecycleDeletionIndex(("Deleted",))
            finally:
                held = False
                events.append("exit")

    monkeypatch.setattr(server, "_item_lifecycle_engine", Lifecycle)

    with server._cloud_sync_item_policy_guard() as allows:
        assert held is True
        assert allows("Deleted") is False
        assert allows("deleted") is False
        assert allows("DELETED") is False
        assert allows("deleted-extra") is True

    assert held is False
    assert events == ["enter", "exit"]


def test_cloud_sync_pass_carries_the_stores(fake_cloud, monkeypatch):
    """The server's sync pass: stores merge inside it, results in the report."""
    import server

    state = lib.load_json(lib.CLIENT_STATE_PATH, {})
    settings = dict(state.get("settings") or {})
    state["settings"] = dict(settings, supabaseUrl="https://x.supabase.co",
                             supabaseKey="k")
    lib.save_json(lib.CLIENT_STATE_PATH, state)
    monkeypatch.setattr(server.sbase, "list_pending_captures",
                        lambda cfg, limit=50: [])
    monkeypatch.setattr(server.sbase, "push_books", lambda cfg, rows: len(rows))
    real_sync_stores = server.store_sync.sync_stores
    calls = []

    def observed_sync_stores(cfg, **kwargs):
        calls.append(("stores", cfg, kwargs))
        assert "builds" not in kwargs["locks"]
        assert kwargs["item_policy_guard"] is (
            server._cloud_sync_item_policy_guard
        )
        return real_sync_stores(cfg, **kwargs)

    entry_result = {
        "pushed": 0,
        "pulled": 0,
        "in_sync": 0,
        "suppressed": 0,
    }

    def observed_sync_entry_files(cfg, **kwargs):
        calls.append(("entries", cfg, kwargs))
        assert kwargs["item_policy_guard"] is (
            server._cloud_sync_item_policy_guard
        )
        return entry_result

    monkeypatch.setattr(
        server.store_sync, "sync_stores", observed_sync_stores
    )
    monkeypatch.setattr(
        server.store_sync, "sync_entry_files", observed_sync_entry_files
    )
    monkeypatch.setattr(server, "_r2_cfg", lambda: {"bucket": "test"})
    monkeypatch.setattr(server.r2, "configured", lambda _cfg: True)
    lib.save_json(ss.STORES["builds"]["path"](),
                  {"b1": {"id": "b1", "title": "One", "updated_at": T1}})
    try:
        res = server._cloud_sync_run()
    finally:
        state["settings"] = settings
        lib.save_json(lib.CLIENT_STATE_PATH, state)
    assert res["ok"] is True, res
    assert res["stores"]["builds"]["pushed"] == 1
    assert res["entries"] == entry_result
    assert fake_cloud.tables["builds"]["b1"]["data"]["title"] == "One"
    assert [call[0] for call in calls] == ["stores", "entries"]


def test_corrections_sync_survives_the_id_backfill(fake_cloud):
    path = ss.STORES["corrections"]["path"]()
    lib.save_json(path, {"added": [{"title": "New Row"}],
                         "edits": {"10": {"title": "Fixed"}}})
    res = ss.sync_store({"url": "u", "key": "k"}, "corrections")
    assert res["pushed"] == 2
    doc = lib.load_json(path, {})
    rid = doc["added"][0]["id"]                      # backfilled and persisted
    assert f"add:{rid}" in fake_cloud.tables["corrections"]
    res = ss.sync_store({"url": "u", "key": "k"}, "corrections")
    assert res["pushed"] == 0 and res["in_sync"] == 2


def test_standalone_cli_refuses_mutation_before_credentials_or_io(monkeypatch):
    def unexpected_config_read():
        pytest.fail("mutating CLI inspected credentials before refusing")

    monkeypatch.setattr(ss, "_cli_cfg", unexpected_config_read)

    with pytest.raises(SystemExit, match="item-lifecycle guard"):
        ss.main(["sync", "--run"])
