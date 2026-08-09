from __future__ import annotations

from copy import deepcopy

import pytest
import supabase_sync


CAPTURE_ID = "2ec86526-1133-4e74-a2c7-497886201d76"
SECOND_CAPTURE_ID = "ed3cb24e-490a-49b1-a066-4e9768bf3f00"
OWNER_ID = "7a1d2f30-59c2-4a41-9a3b-8f6f6f0c2ad1"
ASSET_ID = "asset-2ec86526-p1"
CORRECTION_ID = "1f" * 32
SOURCE_SHA256 = "2e" * 32
DISPLAY_SHA256 = "3d" * 32
THUMBNAIL_SHA256 = "4e" * 32
SELECTED = ("capture_id,asset_id,correction_id,source_original_sha256,"
            "result,revision,updated_at")


def correction_result(correction_id=CORRECTION_ID,
                      display_sha256=DISPLAY_SHA256):
    prefix = (f"{OWNER_ID}/{CAPTURE_ID}/{ASSET_ID}/"
              f"desktop-{correction_id[:20]}")
    return {
        "schema": "org.whl.capture-correction-result",
        "version": 1,
        "processor": "whl-desktop-corrections",
        "recipe": "whl-desktop-correction-v1",
        "correction_id": correction_id,
        "source": {"original_sha256": SOURCE_SHA256},
        "geometry_strategy": "replace_and_reocr",
        "artifacts": {
            "display": {
                "bucket": "capture-derivatives",
                "path": f"{prefix}/display-{display_sha256[:20]}.jpg",
                "sha256": display_sha256,
                "bytes": 123456,
                "width": 1600,
                "height": 1200,
                "content_type": "image/jpeg",
            },
            "thumbnail": {
                "bucket": "capture-derivatives",
                "path": f"{prefix}/thumbnail-{THUMBNAIL_SHA256[:20]}.jpg",
                "sha256": THUMBNAIL_SHA256,
                "bytes": 12_345,
                "width": 512,
                "height": 384,
                "content_type": "image/jpeg",
            },
        },
        "generated_at": "2026-08-01T12:00:00Z",
    }


def correction_row(**overrides):
    row = {
        "capture_id": CAPTURE_ID,
        "asset_id": ASSET_ID,
        "correction_id": CORRECTION_ID,
        "source_original_sha256": SOURCE_SHA256,
        "result": correction_result(),
    }
    row.update(overrides)
    return row


def cloud_row(revision=1, **overrides):
    return {
        **correction_row(**overrides),
        "owner_id": OWNER_ID,
        "revision": revision,
        "updated_at": "2026-08-01T12:00:01Z",
    }


def test_correction_listing_is_explicitly_scoped(monkeypatch):
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, payload, prefer))
        return [cloud_row(), {**cloud_row(), "capture_id": SECOND_CAPTURE_ID}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    rows = supabase_sync.list_capture_corrections(
        {"url": "test"}, [CAPTURE_ID, "not-a-uuid", CAPTURE_ID])

    # Out-of-scope rows are dropped even if the server returned them.
    assert [row["capture_id"] for row in rows] == [CAPTURE_ID]
    assert len(calls) == 1
    method, path, _payload, _prefer = calls[0]
    assert method == "GET"
    assert path.startswith(f"capture_corrections?capture_id=in.({CAPTURE_ID})")
    assert "not-a-uuid" not in path
    assert ("&select=capture_id,asset_id,owner_id,correction_id,"
            "source_original_sha256,result,revision,updated_at") in path


def test_correction_publish_inserts_with_composite_conflict_target(monkeypatch):
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        if method == "GET":
            return []
        return [{**payload[0], "revision": 1,
                 "updated_at": "2026-08-01T12:00:01Z"}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"}, [correction_row()]) == 1
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][1:] == (
        f"capture_corrections?on_conflict=capture_id,asset_id"
        f"&select={SELECTED}",
        [correction_row()],
        "resolution=ignore-duplicates,return=representation",
    )


def test_correction_publish_updates_via_revision_cas(monkeypatch):
    new_correction = "4b" * 32
    new_display = "5c" * 32
    desired = correction_row(
        correction_id=new_correction,
        result=correction_result(new_correction, new_display),
    )
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        if method == "GET":
            return [cloud_row(revision=6)]
        return [{**deepcopy(payload), "capture_id": CAPTURE_ID,
                 "asset_id": ASSET_ID, "revision": 7,
                 "updated_at": "2026-08-01T12:00:02Z"}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"}, [desired]) == 1
    method, path, payload, prefer = calls[1]
    assert method == "PATCH"
    assert f"capture_id=eq.{CAPTURE_ID}" in path
    assert f"asset_id=eq.{ASSET_ID}" in path
    assert "revision=eq.6" in path
    assert prefer == "return=representation"
    # The immutable key columns are filters, never part of the PATCH body.
    assert payload == {
        "correction_id": new_correction,
        "source_original_sha256": SOURCE_SHA256,
        "result": desired["result"],
    }


def test_correction_publish_quotes_dotted_asset_in_cas_filter(monkeypatch):
    dotted_asset = "asset.page-2"
    desired = correction_row(asset_id=dotted_asset)
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        return [{
            **deepcopy(payload),
            "capture_id": CAPTURE_ID,
            "asset_id": dotted_asset,
            "revision": 7,
            "updated_at": "2026-08-01T12:00:02Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"},
        [desired],
        expected_revisions={(CAPTURE_ID, dotted_asset): 6},
    ) == 1
    assert "asset_id=eq.%22asset.page-2%22" in calls[0][1]


def test_correction_publish_uses_callers_observed_revision_without_reread(
        monkeypatch):
    new_correction = "4b" * 32
    new_display = "5c" * 32
    desired = correction_row(
        correction_id=new_correction,
        result=correction_result(new_correction, new_display),
    )
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        assert method != "GET"
        return [{**deepcopy(payload), "capture_id": CAPTURE_ID,
                 "asset_id": ASSET_ID, "revision": 7,
                 "updated_at": "2026-08-01T12:00:02Z"}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"},
        [desired],
        expected_revisions={(CAPTURE_ID, ASSET_ID): 6},
    ) == 1
    assert len(calls) == 1
    assert calls[0][0] == "PATCH"
    assert "revision=eq.6" in calls[0][1]


def test_correction_publish_does_not_overwrite_row_newer_than_observation(
        monkeypatch):
    desired = correction_row(
        correction_id="4b" * 32,
        result=correction_result("4b" * 32, "5c" * 32),
    )
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        del payload, prefer
        calls.append((method, path))
        assert method != "GET"
        return []  # revision 6 was replaced before this exact CAS

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(
            supabase_sync.SyncError,
            match="compare-and-set conflict"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"},
            [desired],
            expected_revisions={(CAPTURE_ID, ASSET_ID): 6},
        )
    assert len(calls) == 1
    assert calls[0][0] == "PATCH"
    assert "revision=eq.6" in calls[0][1]


def test_correction_publish_short_circuits_matching_cloud_row(monkeypatch):
    calls = []
    published = cloud_row(revision=3)
    # Assembly time is volatile and does not change the immutable correction.
    published["result"]["generated_at"] = "2026-08-02T12:00:00Z"

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append(method)
        if method == "GET":
            return [published]
        raise AssertionError("an equal row must not be rewritten")

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"}, [correction_row()]) == 0
    assert calls == ["GET"]


def test_correction_publish_repairs_bool_instead_of_integer(monkeypatch):
    desired = correction_row()
    desired["result"]["artifacts"]["display"]["width"] = 1
    malformed = cloud_row(revision=3)
    malformed["result"]["artifacts"]["display"]["width"] = True
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        del path, prefer
        calls.append((method, deepcopy(payload)))
        if method == "GET":
            return [malformed]
        return [{
            **deepcopy(payload),
            "capture_id": CAPTURE_ID,
            "asset_id": ASSET_ID,
            "revision": 4,
            "updated_at": "2026-08-02T12:00:01Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"}, [desired]) == 1
    assert [method for method, _payload in calls] == ["GET", "PATCH"]
    assert calls[1][1]["result"]["artifacts"]["display"]["width"] == 1
    assert calls[1][1]["result"]["artifacts"]["display"]["width"] is not True


def test_correction_writable_equality_ignores_json_object_key_order():
    expected = correction_row()
    reordered_result = {
        key: expected["result"][key]
        for key in reversed(expected["result"])
    }
    reordered = {
        "result": reordered_result,
        "source_original_sha256": expected["source_original_sha256"],
        "correction_id": expected["correction_id"],
        "asset_id": expected["asset_id"],
        "capture_id": expected["capture_id"],
    }

    assert supabase_sync._capture_correction_writable_equal(
        reordered, expected)


def test_correction_publish_allows_equal_row_after_expected_revision_changes(
        monkeypatch):
    calls = []
    published = cloud_row(revision=4)
    published["result"]["generated_at"] = "2026-08-02T12:00:00Z"

    def rest(_cfg, method, path, payload=None, prefer=""):
        del path, payload, prefer
        calls.append(method)
        if method == "GET":
            return [published]
        raise AssertionError("an equal newer row must not be rewritten")

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"},
        [correction_row()],
        expected_existing={(CAPTURE_ID, ASSET_ID): 3},
    ) == 0
    assert calls == ["GET"]


@pytest.mark.parametrize("expected_revision,current_revision", [
    (None, 1),
    (6, 7),
])
def test_correction_publish_rejects_cloud_change_since_candidate_selection(
        monkeypatch, expected_revision, current_revision):
    foreign_correction = "fb" * 32
    published = cloud_row(
        revision=current_revision,
        correction_id=foreign_correction,
        result=correction_result(foreign_correction, "ac" * 32),
    )
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        del path, payload, prefer
        calls.append(method)
        if method == "GET":
            return [published]
        raise AssertionError("a changed guarded row must not be written")

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(
            supabase_sync.SyncError,
            match="cloud correction changed since candidate selection"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"},
            [correction_row()],
            expected_existing={
                (CAPTURE_ID, ASSET_ID): expected_revision,
            },
        )
    assert calls == ["GET"]


def test_correction_publish_repairs_a_malformed_same_id_row(monkeypatch):
    calls = []
    published = cloud_row(revision=3)
    # These fields are all consumed by Android. The old display-only equality
    # check treated this row as current and left the phone rejecting it forever.
    published["source_original_sha256"] = "9f" * 32
    published["result"]["artifacts"]["display"]["bytes"] = 1
    published["result"]["artifacts"]["thumbnail"]["path"] = "wrong/path.jpg"

    def rest(_cfg, method, path, payload=None, prefer=""):
        del path, prefer
        calls.append((method, deepcopy(payload)))
        if method == "GET":
            return [published]
        return [{
            **deepcopy(payload),
            "capture_id": CAPTURE_ID,
            "asset_id": ASSET_ID,
            "revision": 4,
            "updated_at": "2026-08-02T12:00:01Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_corrections(
        {"url": "test"}, [correction_row()]) == 1
    assert [method for method, _payload in calls] == ["GET", "PATCH"]
    assert calls[1][1] == {
        "correction_id": CORRECTION_ID,
        "source_original_sha256": SOURCE_SHA256,
        "result": correction_result(),
    }


def test_correction_publish_reports_cas_miss_after_other_rows_succeed(monkeypatch):
    conflicted = correction_row(
        correction_id="4b" * 32,
        result=correction_result("4b" * 32, "5c" * 32),
    )
    fresh = correction_row(
        capture_id=SECOND_CAPTURE_ID,
        result={**correction_result(),
                "artifacts": {"display": {
                    **correction_result()["artifacts"]["display"],
                    "path": correction_result()["artifacts"]["display"][
                        "path"].replace(CAPTURE_ID, SECOND_CAPTURE_ID),
                }}},
    )
    writes = []

    def rest(_cfg, method, _path, payload=None, prefer=""):
        del prefer
        if method == "GET":
            return [cloud_row(revision=3)]
        writes.append((method, deepcopy(payload)))
        if method == "PATCH":
            return []  # another writer advanced the revision
        return [{**payload[0], "revision": 1,
                 "updated_at": "2026-08-01T12:00:01Z"}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(
            supabase_sync.SyncError,
            match=r"1 capture correction row\(s\) failed \(1 succeeded\).*"
                  r"compare-and-set"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"}, [conflicted, fresh])
    assert [method for method, _payload in writes] == ["PATCH", "POST"]
    assert writes[1][1][0]["capture_id"] == SECOND_CAPTURE_ID


def test_correction_publish_rejects_unconfirmed_representation(monkeypatch):
    def rest(_cfg, method, _path, payload=None, prefer=""):
        del prefer
        if method == "GET":
            return []
        return [{**payload[0], "revision": 2,  # not the fresh-insert revision
                 "updated_at": "2026-08-01T12:00:01Z"}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(supabase_sync.SyncError, match="invalid row"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"}, [correction_row()])


@pytest.mark.parametrize("mutation", [
    {"capture_id": "not-a-uuid"},
    {"asset_id": ""},
    {"asset_id": "spaced asset"},
    {"asset_id": "a" * 161},
    {"correction_id": CORRECTION_ID[:63]},
    {"correction_id": CORRECTION_ID.upper()},
    {"source_original_sha256": "zz" * 32},
    {"result": None},
])
def test_correction_publish_rejects_invalid_rows_without_http(
        monkeypatch, mutation):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid rows must not reach PostgREST"),
        ),
    )

    with pytest.raises(supabase_sync.SyncError, match="invalid"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"}, [correction_row(**mutation)])


@pytest.mark.parametrize("mutation", [
    {"schema": "org.whl.other"},
    {"version": 2},
    {"version": True},
    {"processor": "whl-cloud-run"},
    {"recipe": "whl-desktop-correction-v2"},
    {"correction_id": "4b" * 32},
])
def test_correction_publish_rejects_contradictory_result(monkeypatch, mutation):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid rows must not reach PostgREST"),
        ),
    )

    with pytest.raises(supabase_sync.SyncError, match="contradicts"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"},
            [correction_row(result={**correction_result(), **mutation})])


def test_correction_publish_rejects_oversized_result_without_http(monkeypatch):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid rows must not reach PostgREST"),
        ),
    )

    with pytest.raises(supabase_sync.SyncError, match="64 KiB"):
        supabase_sync.publish_capture_corrections(
            {"url": "test"},
            [correction_row(result={**correction_result(),
                                    "pad": "x" * (65 * 1024)})])
