from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from librarytool.adapters.filesystem.capture_asset_lifecycle import (
    CAPTURE_ASSET_LIFECYCLE_FIELD,
    CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA,
    FilesystemCaptureAssetLifecycleStore,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.engine.errors import ConflictError, RepositoryError


ITEM_ID = "item-asset-lifecycle"
CAPTURE_ID = "capture-asset-lifecycle"
ASSET_IDS = ("asset-page-one", "asset-page-three")
ARTIFACT_REVISION = "artifact-r1"


def _opaque_identity(namespace: str, *parts) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()[:40]}"


def _artifact_id(asset_id: str, rendition: str = "display") -> str:
    return f"{_opaque_identity('capture', CAPTURE_ID, asset_id)}:{rendition}"


def _manifest() -> dict:
    assets = []
    imports = []
    for asset_id, order in zip(ASSET_IDS, (1, 3), strict=True):
        assets.append(
            {
                "asset_id": asset_id,
                "capture_order": order,
                "capture_file": f"photo_{order}.jpg",
                "original": {
                    "reference": f"phone_{order}.jpg",
                    "sha256": f"{order}" * 64,
                    "revision": 1,
                    "width": 10,
                    "height": 20,
                    "orientation": 0,
                },
                "display": {
                    "reference": f"photo_{order}.jpg",
                    "sha256": f"{order + 1}" * 64,
                    "revision": 1,
                    "width": 10,
                    "height": 20,
                    "orientation": 0,
                    "recipe": "desktop-standardize",
                    "recipe_version": "1",
                },
                # This is Android's processing lifecycle. Desktop membership
                # must remain a separate additive object.
                "lifecycle": {"state": "completed", "updated_at": order},
                "role": {},
                "geometry": [],
            }
        )
        imports.append(
            {
                "order": order - 1,
                "asset_id": asset_id,
                "raw_ref": f"orig_{order}.jpg",
                "display_ref": f"photo_{order}.jpg",
                "source_checksum": f"{order}" * 64,
                "derivative_checksum": f"{order + 1}" * 64,
                "transport_representation": "original",
                "recipe": "desktop-standardize-v1",
                "lifecycle": "completed",
            }
        )
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "legacy_fallback": False,
        "assets": assets,
        "selections": {},
        "transport": {"representation": "original", "version": 1},
        "desktop_import": {"version": 1, "assets": imports},
    }


@contextmanager
def _lock():
    yield


class _Fixture:
    def __init__(self, tmp_path: Path, *, publish_hook=None) -> None:
        self.root = tmp_path / "library"
        self.output = self.root / "output"
        self.captures = self.root / "captures"
        self.capture_dir = self.captures / CAPTURE_ID
        self.output.mkdir(parents=True)
        self.capture_dir.mkdir(parents=True)
        self.manifest_path = self.capture_dir / "photo_assets.json"
        self.catalogue_path = self.output / "catalogue.json"
        self.manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
        self.catalogue_path.write_text(
            json.dumps({"updated_at": "item-r1"}),
            encoding="utf-8",
        )
        for order in (1, 3):
            (self.capture_dir / f"orig_{order}.jpg").write_bytes(
                f"original-{order}".encode()
            )
            (self.capture_dir / f"photo_{order}.jpg").write_bytes(
                f"display-{order}".encode()
            )
        self.artifacts = {
            _artifact_id(asset_id, rendition): SimpleNamespace(
                revision=ARTIFACT_REVISION
            )
            for asset_id in ASSET_IDS
            for rendition in ("display", "original")
        }
        self.coordination = RecoverableWriteSet(self.output)
        self.store = self.new_store(publish_hook=publish_hook)

    def new_store(self, *, publish_hook=None):
        return FilesystemCaptureAssetLifecycleStore(
            RecoverableWriteSet(self.root, publish_hook=publish_hook),
            coordination_write_set=self.coordination,
            storage_root=self.output,
            capture_authority_root=self.captures,
            capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
            capture_directory_for=lambda capture_id: self.captures / capture_id,
            artifact_for=lambda key: self.artifacts.get(key.artifact_id),
            lock_context_for=_lock,
            item_updated_at_publication_for=self.advance_item,
        )

    def advance_item(self, item_id: str):
        assert item_id == ITEM_ID
        current = json.loads(self.catalogue_path.read_text("utf-8"))
        number = int(current["updated_at"].removeprefix("item-r")) + 1
        updated_at = f"item-r{number}"
        return (
            self.catalogue_path,
            json.dumps({"updated_at": updated_at}, sort_keys=True).encode(),
            updated_at,
        )

    def manifest(self) -> dict:
        return json.loads(self.manifest_path.read_text("utf-8"))

    def file_payloads(self) -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in self.capture_dir.iterdir()
            if path.name != "photo_assets.json"
        }

    def receipt_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode()).hexdigest()
        return (
            self.output
            / ".engine"
            / "receipts"
            / "capture-asset-lifecycle"
            / f"{digest}.json"
        )


def test_delete_is_additive_idempotent_and_preserves_contract_and_files(tmp_path):
    fixture = _Fixture(tmp_path)
    files_before = fixture.file_payloads()
    manifest_before = fixture.manifest()
    artifact_id = _artifact_id(ASSET_IDS[0])

    result = fixture.store.delete_capture_asset(
        ITEM_ID,
        artifact_id,
        ARTIFACT_REVISION,
        "delete-page-one",
    )

    manifest = fixture.manifest()
    lifecycle = manifest["assets"][0][CAPTURE_ASSET_LIFECYCLE_FIELD]
    assert lifecycle == result["after_lifecycle"]
    assert lifecycle["state"] == "deleted"
    assert lifecycle["revision"] == 1
    assert lifecycle["updated_at"] > 0
    assert (
        manifest["assets"][0]["lifecycle"] == manifest_before["assets"][0]["lifecycle"]
    )
    assert [asset["capture_order"] for asset in manifest["assets"]] == [1, 3]
    assert manifest["desktop_import"] == manifest_before["desktop_import"]
    assert fixture.file_payloads() == files_before
    assert json.loads(fixture.catalogue_path.read_text())["updated_at"] == "item-r2"
    assert result["before_lifecycle"] is None
    assert result["inverse"] == {
        "schema": CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA,
        "action": "restore",
        "source_operation_id": "delete-page-one",
        "item_id": ITEM_ID,
        "capture_id": CAPTURE_ID,
        "asset_id": ASSET_IDS[0],
        "artifact_id": artifact_id,
        "artifact_revision": ARTIFACT_REVISION,
        "capture_order": 1,
        "expected_lifecycle": lifecycle,
    }
    receipt = json.loads(fixture.receipt_path("delete-page-one").read_text("ascii"))
    assert receipt["result"] == result

    replay = fixture.new_store().delete_capture_asset(
        ITEM_ID,
        artifact_id,
        ARTIFACT_REVISION,
        "delete-page-one",
    )
    assert replay == {**result, "replayed": True}
    assert json.loads(fixture.catalogue_path.read_text())["updated_at"] == "item-r2"

    with pytest.raises(ConflictError) as reused:
        fixture.store.delete_capture_asset(
            ITEM_ID,
            _artifact_id(ASSET_IDS[1]),
            ARTIFACT_REVISION,
            "delete-page-one",
        )
    assert reused.value.code == "operation_id_conflict"


def test_delete_requires_artifact_cas_and_rejects_duplicate_delete(tmp_path):
    fixture = _Fixture(tmp_path)
    artifact_id = _artifact_id(ASSET_IDS[0])

    with pytest.raises(ConflictError) as stale:
        fixture.store.delete_capture_asset(
            ITEM_ID,
            artifact_id,
            "artifact-stale",
            "delete-stale",
        )
    assert stale.value.code == "raster_resource_revision_conflict"
    assert CAPTURE_ASSET_LIFECYCLE_FIELD not in fixture.manifest()["assets"][0]

    fixture.store.delete_capture_asset(
        ITEM_ID,
        artifact_id,
        ARTIFACT_REVISION,
        "delete-first",
    )
    with pytest.raises(ConflictError) as duplicate:
        fixture.store.delete_capture_asset(
            ITEM_ID,
            artifact_id,
            ARTIFACT_REVISION,
            "delete-second",
        )
    assert duplicate.value.code == "capture_asset_already_deleted"


def test_restore_is_bound_to_persisted_delete_and_monotonic_lifecycle(tmp_path):
    fixture = _Fixture(tmp_path)
    artifact_id = _artifact_id(ASSET_IDS[0])
    files_before = fixture.file_payloads()
    deleted = fixture.store.delete_capture_asset(
        ITEM_ID,
        artifact_id,
        ARTIFACT_REVISION,
        "delete-for-restore",
    )
    inverse = deleted["inverse"]

    foreign = copy.deepcopy(inverse)
    foreign["asset_id"] = ASSET_IDS[1]
    with pytest.raises(ConflictError) as foreign_receipt:
        fixture.store.restore_capture_asset(
            ITEM_ID,
            artifact_id,
            foreign,
            "restore-foreign-receipt",
        )
    assert foreign_receipt.value.code == "capture_asset_restore_inverse_conflict"

    with pytest.raises(ConflictError) as foreign_target:
        fixture.store.restore_capture_asset(
            ITEM_ID,
            _artifact_id(ASSET_IDS[1]),
            inverse,
            "restore-foreign-target",
        )
    assert foreign_target.value.code == "capture_asset_restore_target_conflict"

    restored = fixture.store.restore_capture_asset(
        ITEM_ID,
        artifact_id,
        inverse,
        "restore-page-one",
    )
    lifecycle = fixture.manifest()["assets"][0][CAPTURE_ASSET_LIFECYCLE_FIELD]
    assert lifecycle == restored["after_lifecycle"]
    assert lifecycle["state"] == "active"
    assert lifecycle["revision"] == 2
    assert lifecycle["updated_at"] > deleted["after_lifecycle"]["updated_at"]
    assert fixture.file_payloads() == files_before
    assert json.loads(fixture.catalogue_path.read_text())["updated_at"] == "item-r3"

    replay = fixture.new_store().restore_capture_asset(
        ITEM_ID,
        artifact_id,
        inverse,
        "restore-page-one",
    )
    assert replay == {**restored, "replayed": True}
    assert json.loads(fixture.catalogue_path.read_text())["updated_at"] == "item-r3"

    with pytest.raises(ConflictError) as already_restored:
        fixture.store.restore_capture_asset(
            ITEM_ID,
            artifact_id,
            inverse,
            "restore-page-one-again",
        )
    assert already_restored.value.code == "capture_asset_lifecycle_revision_conflict"

    deleted_again = fixture.store.delete_capture_asset(
        ITEM_ID,
        artifact_id,
        ARTIFACT_REVISION,
        "delete-page-one-again",
    )
    assert deleted_again["after_lifecycle"]["revision"] == 3
    with pytest.raises(ConflictError) as stale_inverse:
        fixture.store.restore_capture_asset(
            ITEM_ID,
            artifact_id,
            inverse,
            "restore-stale-delete",
        )
    assert stale_inverse.value.code == "capture_asset_lifecycle_revision_conflict"


@pytest.mark.parametrize(
    "target_name", ["catalogue.json", "photo_assets.json", "receipt"]
)
def test_interrupted_delete_rolls_back_every_publication_stage(tmp_path, target_name):
    crash_receipt = {"path": None}

    def crash(_index: int, target: Path) -> None:
        if target_name == "receipt":
            if target == crash_receipt["path"]:
                raise SystemExit("simulated delete process loss")
        elif target.name == target_name:
            raise SystemExit("simulated delete process loss")

    fixture = _Fixture(tmp_path, publish_hook=crash)
    crash_receipt["path"] = fixture.receipt_path("delete-crash")
    manifest_before = fixture.manifest_path.read_bytes()
    catalogue_before = fixture.catalogue_path.read_bytes()
    files_before = fixture.file_payloads()

    with pytest.raises(SystemExit, match="simulated delete process loss"):
        fixture.store.delete_capture_asset(
            ITEM_ID,
            _artifact_id(ASSET_IDS[0]),
            ARTIFACT_REVISION,
            "delete-crash",
        )

    results = RecoverableWriteSet(fixture.root).recover_all()
    assert any(result.action == "rolled_back_interrupted" for result in results)
    assert fixture.manifest_path.read_bytes() == manifest_before
    assert fixture.catalogue_path.read_bytes() == catalogue_before
    assert fixture.file_payloads() == files_before
    assert not fixture.receipt_path("delete-crash").exists()


@pytest.mark.parametrize(
    "target_name", ["catalogue.json", "photo_assets.json", "receipt"]
)
def test_interrupted_restore_rolls_back_every_publication_stage(tmp_path, target_name):
    fixture = _Fixture(tmp_path)
    artifact_id = _artifact_id(ASSET_IDS[0])
    deleted = fixture.store.delete_capture_asset(
        ITEM_ID,
        artifact_id,
        ARTIFACT_REVISION,
        "delete-before-crash",
    )
    restore_receipt = fixture.receipt_path("restore-crash")

    def crash(_index: int, target: Path) -> None:
        if target_name == "receipt":
            if target == restore_receipt:
                raise SystemExit("simulated restore process loss")
        elif target.name == target_name:
            raise SystemExit("simulated restore process loss")

    crashing_store = fixture.new_store(publish_hook=crash)
    manifest_before = fixture.manifest_path.read_bytes()
    catalogue_before = fixture.catalogue_path.read_bytes()
    files_before = fixture.file_payloads()

    with pytest.raises(SystemExit, match="simulated restore process loss"):
        crashing_store.restore_capture_asset(
            ITEM_ID,
            artifact_id,
            deleted["inverse"],
            "restore-crash",
        )

    results = RecoverableWriteSet(fixture.root).recover_all()
    assert any(result.action == "rolled_back_interrupted" for result in results)
    assert fixture.manifest_path.read_bytes() == manifest_before
    assert fixture.catalogue_path.read_bytes() == catalogue_before
    assert fixture.file_payloads() == files_before
    assert not restore_receipt.exists()
    assert fixture.receipt_path("delete-before-crash").is_file()


def test_malformed_additive_lifecycle_is_rejected_without_mutation(tmp_path):
    fixture = _Fixture(tmp_path)
    manifest = fixture.manifest()
    manifest["assets"][0][CAPTURE_ASSET_LIFECYCLE_FIELD] = {
        "state": "deleted",
        "revision": 0,
        "updated_at": 1,
    }
    fixture.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    catalogue_before = fixture.catalogue_path.read_bytes()

    with pytest.raises(RepositoryError) as invalid:
        fixture.store.delete_capture_asset(
            ITEM_ID,
            _artifact_id(ASSET_IDS[0]),
            ARTIFACT_REVISION,
            "delete-invalid-lifecycle",
        )
    assert invalid.value.code == "invalid_capture_asset_lifecycle"
    assert fixture.catalogue_path.read_bytes() == catalogue_before
    assert not fixture.receipt_path("delete-invalid-lifecycle").exists()
