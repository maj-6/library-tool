from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import libcommon as lib
import pytest
import server


CAPTURE_ID = "2ec86526-1133-4e74-a2c7-497886201d76"
OWNER_ID = "7a1d2f30-59c2-4a41-9a3b-8f6f6f0c2ad1"


def _manifest(states=("deleted", "active", None)) -> dict:
    assets = []
    imports = []
    for index, state in enumerate(states, 1):
        asset_id = f"asset-page-{index}"
        source_sha = f"{index}" * 64
        asset = {
            "asset_id": asset_id,
            "capture_order": index,
            "original": {
                "reference": f"phone_{index}.jpg",
                "sha256": source_sha,
                "revision": 1,
                "width": 20,
                "height": 30,
                "orientation": 0,
            },
            "display": {
                "reference": f"photo_{index}.jpg",
                "sha256": f"{index + 3}" * 64,
                "revision": 1,
                "width": 20,
                "height": 30,
                "orientation": 0,
                "recipe": "desktop-standardize",
                "recipe_version": "1",
            },
            "lifecycle": {"state": "completed", "updated_at": index},
            "role": {},
            "geometry": [],
        }
        if state is not None:
            asset["desktop_lifecycle"] = {
                "state": state,
                "revision": index,
                "updated_at": 1_786_281_234_560 + index,
            }
        assets.append(asset)
        imports.append({
            "order": index - 1,
            "asset_id": asset_id,
            "raw_ref": f"orig_{index}.jpg",
            "display_ref": f"photo_{index}.jpg",
            "source_checksum": source_sha,
            "derivative_checksum": f"{index + 3}" * 64,
            "transport_representation": "original",
            "recipe": "desktop-standardize-v1",
            "lifecycle": "completed",
        })
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "assets": assets,
        "desktop_import": {"version": 1, "assets": imports},
    }


@pytest.fixture()
def lifecycle_workspace(monkeypatch, tmp_path: Path):
    manual_path = tmp_path / "manual_entries.json"
    captures = tmp_path / "captures"
    capture_dir = captures / CAPTURE_ID
    capture_dir.mkdir(parents=True)
    manual_path.write_text(json.dumps({
        "item-1": {"capture_id": CAPTURE_ID},
    }), encoding="utf-8")
    manifest_path = capture_dir / "photo_assets.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures)
    write_set = SimpleNamespace(
        workspace_lease=lambda: contextlib.nullcontext())
    monkeypatch.setattr(
        server,
        "_ensure_engine_session",
        lambda: SimpleNamespace(write_set=write_set),
    )
    monkeypatch.setattr(
        server,
        "_corrections_workspace_locks",
        lambda: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        server,
        "_capture_correction_owner_ids",
        lambda _cfg, ids: {capture_id: OWNER_ID for capture_id in ids},
    )
    return manifest_path


def test_publisher_emits_only_explicit_memberships_with_bound_identity(
        monkeypatch, lifecycle_workspace):
    del lifecycle_workspace
    published = []
    monkeypatch.setattr(
        server.sbase, "list_capture_asset_lifecycle",
        lambda _cfg, _ids: [], raising=False)

    def publish(_cfg, rows, *, expected_revisions):
        published.extend(rows)
        assert set(expected_revisions.values()) == {None}
        return len(rows)

    monkeypatch.setattr(
        server.sbase, "publish_capture_asset_lifecycle",
        publish, raising=False)

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert outcome["candidates"] == 2
    assert outcome["pushed"] == 2
    assert outcome["errors"] == []
    assert [row["asset_id"] for row in published] == [
        "asset-page-1", "asset-page-2"]
    assert [row["result"]["state"] for row in published] == [
        "deleted", "active"]
    for row in published:
        assert set(row["result"]) == {
            "schema", "version", "capture_id", "asset_id",
            "source_original_sha256", "state", "capture_order",
            "lifecycle_revision", "changed_at",
        }
        assert row["result"]["capture_id"] == row["capture_id"]
        assert row["result"]["asset_id"] == row["asset_id"]
        assert row["result"]["source_original_sha256"] == \
            row["source_original_sha256"]


def test_publisher_keeps_a_newer_cloud_lifecycle(monkeypatch,
                                                 lifecycle_workspace):
    manifest = json.loads(lifecycle_workspace.read_text("utf-8"))
    local = server._capture_asset_lifecycle_manifest_rows(
        CAPTURE_ID, manifest)[0]
    cloud = {
        **local,
        "result": {
            **local["result"],
            "state": "active",
            "lifecycle_revision": 9,
            "changed_at": local["result"]["changed_at"] + 100,
        },
        "revision": 4,
        "updated_at": "2026-08-09T12:00:00Z",
    }
    monkeypatch.setattr(
        server.sbase, "list_capture_asset_lifecycle",
        lambda _cfg, _ids: [cloud], raising=False)
    published = []
    monkeypatch.setattr(
        server.sbase, "publish_capture_asset_lifecycle",
        lambda _cfg, rows, **_kwargs: published.extend(rows), raising=False)

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert all(row["asset_id"] != "asset-page-1" for row in published)
    assert any("kept newer cloud" in notice
               for notice in outcome["notices"])


def test_publisher_rejects_unequal_state_at_same_semantic_revision(
        monkeypatch, lifecycle_workspace):
    manifest = json.loads(lifecycle_workspace.read_text("utf-8"))
    local = server._capture_asset_lifecycle_manifest_rows(
        CAPTURE_ID, manifest)[0]
    cloud = {
        **local,
        "result": {
            **local["result"],
            "state": "active",
            "changed_at": local["result"]["changed_at"] + 100,
        },
        "revision": 4,
        "updated_at": "2026-08-09T12:00:00Z",
    }
    monkeypatch.setattr(
        server.sbase, "list_capture_asset_lifecycle",
        lambda _cfg, _ids: [cloud], raising=False)
    published = []
    monkeypatch.setattr(
        server.sbase, "publish_capture_asset_lifecycle",
        lambda _cfg, rows, **_kwargs: published.extend(rows), raising=False)

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert all(row["asset_id"] != "asset-page-1" for row in published)
    assert any("conflicts at revision 1" in error
               for error in outcome["errors"])


def test_publisher_repairs_a_malformed_cloud_result(monkeypatch,
                                                    lifecycle_workspace):
    manifest = json.loads(lifecycle_workspace.read_text("utf-8"))
    local = server._capture_asset_lifecycle_manifest_rows(
        CAPTURE_ID, manifest)[0]
    cloud = {
        **local,
        "result": {**local["result"], "schema": "org.whl.damaged"},
        "revision": 4,
        "updated_at": "2026-08-09T12:00:00Z",
    }
    monkeypatch.setattr(
        server.sbase, "list_capture_asset_lifecycle",
        lambda _cfg, _ids: [cloud], raising=False)
    published = []

    def publish(_cfg, rows, *, expected_revisions):
        published.extend(rows)
        assert expected_revisions[(CAPTURE_ID, "asset-page-1")] == 4
        return len(rows)

    monkeypatch.setattr(
        server.sbase, "publish_capture_asset_lifecycle",
        publish, raising=False)

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    repaired = next(row for row in published
                    if row["asset_id"] == "asset-page-1")
    assert repaired["result"]["schema"] == \
        "org.whl.capture-asset-lifecycle"
    assert outcome["errors"] == []


def test_publisher_revalidates_manifest_before_cloud_write(
        monkeypatch, lifecycle_workspace):
    def list_rows(_cfg, _ids):
        changed = json.loads(lifecycle_workspace.read_text("utf-8"))
        changed["assets"][0]["desktop_lifecycle"] = {
            "state": "active",
            "revision": 2,
            "updated_at": 1_786_281_234_999,
        }
        lifecycle_workspace.write_text(json.dumps(changed), encoding="utf-8")
        return []

    monkeypatch.setattr(
        server.sbase, "list_capture_asset_lifecycle",
        list_rows, raising=False)
    published = []
    monkeypatch.setattr(
        server.sbase, "publish_capture_asset_lifecycle",
        lambda _cfg, rows, **_kwargs: published.extend(rows), raising=False)

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert all(row["asset_id"] != "asset-page-1" for row in published)
    assert any("skipped stale lifecycle" in notice
               for notice in outcome["notices"])


def test_publisher_isolates_invalid_capture_without_cloud_io(
        monkeypatch, lifecycle_workspace):
    manifest = json.loads(lifecycle_workspace.read_text("utf-8"))
    manifest["assets"][0]["original"]["sha256"] = "9" * 64
    lifecycle_workspace.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        server.sbase,
        "list_capture_asset_lifecycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid local capture must not reach cloud")),
        raising=False,
    )

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert outcome["candidates"] == 0
    assert outcome["unreadable_capture"] == 1
    assert outcome["errors"] == []


@pytest.mark.parametrize("unsafe_asset", [".", ".."])
def test_publisher_rejects_unsafe_asset_identity_without_cloud_io(
        monkeypatch, lifecycle_workspace, unsafe_asset):
    manifest = json.loads(lifecycle_workspace.read_text("utf-8"))
    manifest["assets"][0]["asset_id"] = unsafe_asset
    manifest["desktop_import"]["assets"][0]["asset_id"] = unsafe_asset
    lifecycle_workspace.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        server.sbase,
        "list_capture_asset_lifecycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe local capture must not reach cloud")),
        raising=False,
    )

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert outcome["candidates"] == 0
    assert outcome["unreadable_capture"] == 1
    assert outcome["errors"] == []


def test_publisher_treats_missing_migration_as_skipped(
        monkeypatch, lifecycle_workspace):
    del lifecycle_workspace

    def missing(_cfg, _ids):
        raise server.sbase.SyncError(
            "HTTP 404 on GET https://cloud.example/rest/v1/"
            "capture_asset_lifecycle: PGRST205 relation missing")

    monkeypatch.setattr(
        server.sbase, "list_capture_asset_lifecycle", missing, raising=False)

    outcome = server._publish_capture_asset_lifecycle({"url": "test"})

    assert "027_capture_asset_lifecycle.sql" in outcome["skipped"]
    assert outcome["errors"] == []
