from __future__ import annotations

from copy import deepcopy

import pytest
import supabase_sync


CAPTURE_ID = "2ec86526-1133-4e74-a2c7-497886201d76"
SECOND_CAPTURE_ID = "ed3cb24e-490a-49b1-a066-4e9768bf3f00"
OWNER_ID = "7a1d2f30-59c2-4a41-9a3b-8f6f6f0c2ad1"
ASSET_ID = "asset-page-2"
SOURCE_SHA256 = "2e" * 32
SELECTED = (
    "capture_id,asset_id,source_original_sha256,result,revision,updated_at"
)


def lifecycle_result(**overrides):
    result = {
        "schema": "org.whl.capture-asset-lifecycle",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "asset_id": ASSET_ID,
        "source_original_sha256": SOURCE_SHA256,
        "state": "deleted",
        "capture_order": 2,
        "lifecycle_revision": 1,
        "changed_at": 1_786_281_234_567,
    }
    result.update(overrides)
    return result


def lifecycle_row(**overrides):
    row = {
        "capture_id": CAPTURE_ID,
        "asset_id": ASSET_ID,
        "source_original_sha256": SOURCE_SHA256,
        "result": lifecycle_result(),
    }
    row.update(overrides)
    return row


def cloud_row(revision=1, **overrides):
    return {
        **lifecycle_row(**overrides),
        "owner_id": OWNER_ID,
        "revision": revision,
        "updated_at": "2026-08-09T12:00:01Z",
    }


def test_lifecycle_listing_is_capture_scoped(monkeypatch):
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, payload, prefer))
        if len(calls) == 1:
            return [cloud_row(), {**cloud_row(), "capture_id": SECOND_CAPTURE_ID}]
        return []

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    rows = supabase_sync.list_capture_asset_lifecycle(
        {"url": "test"}, [CAPTURE_ID, "not-a-uuid", CAPTURE_ID])

    assert [row["capture_id"] for row in rows] == [CAPTURE_ID]
    assert len(calls) == 2
    assert calls[0][0] == "GET"
    assert calls[0][1].startswith(
        f"capture_asset_lifecycle?capture_id=in.({CAPTURE_ID})")
    assert "not-a-uuid" not in calls[0][1]
    assert "owner_id,source_original_sha256,result,revision,updated_at" \
        in calls[0][1]
    assert "order=capture_id.asc,asset_id.asc&limit=500" in calls[0][1]


def test_lifecycle_listing_continues_after_server_capped_short_pages(monkeypatch):
    calls = []
    first_asset = "asset-page-1"
    second_asset = "asset-page-2"

    def row(asset_id):
        return cloud_row(
            asset_id=asset_id,
            result=lifecycle_result(asset_id=asset_id),
        )

    def rest(_cfg, method, path, payload=None, prefer=""):
        del payload, prefer
        calls.append((method, path))
        if "asset_id.gt.asset-page-2" in path:
            return []
        if "asset_id.gt.asset-page-1" in path:
            return [row(second_asset)]
        return [row(first_asset)]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    rows = supabase_sync.list_capture_asset_lifecycle(
        {"url": "test"}, [CAPTURE_ID], page_size=500)

    assert [item["asset_id"] for item in rows] == [first_asset, second_asset]
    assert len(calls) == 3
    assert all("capture_id=in.(" + CAPTURE_ID + ")" in path
               for _, path in calls)
    assert all("order=capture_id.asc,asset_id.asc" in path
               for _, path in calls)
    assert "or=(capture_id.gt." in calls[1][1]
    assert "capture_id.eq." + CAPTURE_ID in calls[1][1]
    assert "asset_id.gt.asset-page-1" in calls[1][1]


def test_lifecycle_listing_quotes_dotted_asset_cursor(monkeypatch):
    calls = []
    dotted_asset = "asset.page-1"

    def rest(_cfg, method, path, payload=None, prefer=""):
        del payload, prefer
        calls.append((method, path))
        if len(calls) == 1:
            return [cloud_row(
                asset_id=dotted_asset,
                result=lifecycle_result(asset_id=dotted_asset),
            )]
        return []

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    rows = supabase_sync.list_capture_asset_lifecycle(
        {"url": "test"}, [CAPTURE_ID])

    assert [row["asset_id"] for row in rows] == [dotted_asset]
    assert "asset_id.gt.%22asset.page-1%22" in calls[1][1]


@pytest.mark.parametrize("unsafe_asset", [".", ".."])
def test_lifecycle_listing_rejects_unsafe_asset_cursor(
        monkeypatch, unsafe_asset):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: [cloud_row(
            asset_id=unsafe_asset,
            result=lifecycle_result(asset_id=unsafe_asset),
        )],
    )

    with pytest.raises(supabase_sync.SyncError, match="invalid cursor"):
        supabase_sync.list_capture_asset_lifecycle(
            {"url": "test"}, [CAPTURE_ID])


def test_lifecycle_listing_fails_instead_of_truncating_at_safety_bound(
        monkeypatch):
    calls = []

    def rest(_cfg, _method, path, _payload=None, _prefer=""):
        calls.append(path)
        asset_id = "asset-page-2" if "asset_id.gt.asset-page-1" in path \
            else "asset-page-1"
        return [cloud_row(
            asset_id=asset_id,
            result=lifecycle_result(asset_id=asset_id),
        )]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="safety limit"):
        supabase_sync.list_capture_asset_lifecycle(
            {"url": "test"}, [CAPTURE_ID], maximum_rows=1)

    assert len(calls) == 2
    assert calls[0].endswith("&limit=2")
    assert calls[1].endswith("&limit=1")


def test_lifecycle_listing_propagates_missing_migration_errors(monkeypatch):
    def rest(*_args, **_kwargs):
        raise supabase_sync.SyncError(
            "HTTP 404: capture_asset_lifecycle is missing")

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="HTTP 404"):
        supabase_sync.list_capture_asset_lifecycle(
            {"url": "test"}, [CAPTURE_ID])


def test_lifecycle_publish_inserts_with_composite_conflict_target(monkeypatch):
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        if method == "GET":
            return []
        return [{**payload[0], "revision": 1,
                 "updated_at": "2026-08-09T12:00:01Z"}]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    assert supabase_sync.publish_capture_asset_lifecycle(
        {"url": "test"}, [lifecycle_row()]) == 1
    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][1:] == (
        "capture_asset_lifecycle?on_conflict=capture_id,asset_id"
        f"&select={SELECTED}",
        [lifecycle_row()],
        "resolution=ignore-duplicates,return=representation",
    )


def test_lifecycle_publish_updates_through_observed_revision_cas(monkeypatch):
    desired = lifecycle_row(result=lifecycle_result(
        state="active", lifecycle_revision=2, changed_at=1_786_281_234_568))
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        assert method != "GET"
        return [{
            **deepcopy(payload),
            "capture_id": CAPTURE_ID,
            "asset_id": ASSET_ID,
            "revision": 7,
            "updated_at": "2026-08-09T12:00:02Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    assert supabase_sync.publish_capture_asset_lifecycle(
        {"url": "test"},
        [desired],
        expected_revisions={(CAPTURE_ID, ASSET_ID): 6},
    ) == 1
    assert len(calls) == 1
    method, path, payload, prefer = calls[0]
    assert method == "PATCH"
    assert f"capture_id=eq.{CAPTURE_ID}" in path
    assert f"asset_id=eq.{ASSET_ID}" in path
    assert "revision=eq.6" in path
    assert payload == {
        "source_original_sha256": SOURCE_SHA256,
        "result": desired["result"],
    }
    assert prefer == "return=representation"


def test_lifecycle_publish_quotes_dotted_asset_in_cas_filter(monkeypatch):
    dotted_asset = "asset.page-2"
    desired = lifecycle_row(
        asset_id=dotted_asset,
        result=lifecycle_result(
            asset_id=dotted_asset,
            state="active",
            lifecycle_revision=2,
            changed_at=1_786_281_234_568,
        ),
    )
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        return [{
            **deepcopy(payload),
            "capture_id": CAPTURE_ID,
            "asset_id": dotted_asset,
            "revision": 7,
            "updated_at": "2026-08-09T12:00:02Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.publish_capture_asset_lifecycle(
        {"url": "test"},
        [desired],
        expected_revisions={(CAPTURE_ID, dotted_asset): 6},
    ) == 1
    assert "asset_id=eq.%22asset.page-2%22" in calls[0][1]


def test_lifecycle_publish_reports_compare_and_set_conflict(monkeypatch):
    monkeypatch.setattr(supabase_sync, "_rest", lambda *_args, **_kwargs: [])

    with pytest.raises(supabase_sync.SyncError, match="compare-and-set"):
        supabase_sync.publish_capture_asset_lifecycle(
            {"url": "test"},
            [lifecycle_row()],
            expected_revisions={(CAPTURE_ID, ASSET_ID): 4},
        )


def test_lifecycle_publish_does_not_rewrite_exact_cloud_state(monkeypatch):
    calls = []

    def rest(_cfg, method, _path, payload=None, prefer=""):
        del payload, prefer
        calls.append(method)
        if method == "GET" and len(calls) == 1:
            return [cloud_row(revision=3)]
        if method == "GET":
            return []
        raise AssertionError("equal lifecycle state must not be rewritten")

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    assert supabase_sync.publish_capture_asset_lifecycle(
        {"url": "test"}, [lifecycle_row()]) == 0
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize(("field", "value"), [
    ("state", "removed"),
    ("capture_order", 0),
    ("capture_order", True),
    ("lifecycle_revision", 0),
    ("lifecycle_revision", True),
    ("changed_at", 0),
    ("changed_at", True),
    ("capture_id", SECOND_CAPTURE_ID),
    ("asset_id", "another-asset"),
    ("source_original_sha256", "9f" * 32),
])
def test_lifecycle_publish_rejects_invalid_or_unbound_result_without_http(
        monkeypatch, field, value):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid rows must not reach PostgREST")),
    )

    with pytest.raises(supabase_sync.SyncError, match="invalid|contradicts"):
        supabase_sync.publish_capture_asset_lifecycle(
            {"url": "test"},
            [lifecycle_row(result=lifecycle_result(**{field: value}))],
        )


def test_lifecycle_publish_requires_the_exact_v1_result_shape(monkeypatch):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid rows must not reach PostgREST")),
    )
    extra = {**lifecycle_result(), "deleted_at": 1_786_281_234_567}

    with pytest.raises(supabase_sync.SyncError, match="invalid"):
        supabase_sync.publish_capture_asset_lifecycle(
            {"url": "test"}, [lifecycle_row(result=extra)])


@pytest.mark.parametrize("unsafe_asset", [".", ".."])
def test_lifecycle_publish_rejects_unsafe_asset_identity_without_http(
        monkeypatch, unsafe_asset):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe rows must not reach PostgREST")),
    )

    with pytest.raises(supabase_sync.SyncError, match="invalid"):
        supabase_sync.publish_capture_asset_lifecycle(
            {"url": "test"},
            [lifecycle_row(
                asset_id=unsafe_asset,
                result=lifecycle_result(asset_id=unsafe_asset),
            )],
        )
