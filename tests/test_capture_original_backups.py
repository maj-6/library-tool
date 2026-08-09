import hashlib
import io
import json
import os
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from librarytool.adapters.filesystem.capture_original_backups import (
    FilesystemCaptureOriginalBackupStore,
    parse_original_backup_marker,
)
from librarytool.adapters.filesystem.correction_transform_store import (
    FilesystemCorrectionTransformStore,
    correction_display_head_path,
)
from librarytool.adapters.filesystem.correction_repository import (
    FilesystemCorrectionRepository,
)
from librarytool.adapters.filesystem.corrections_artifact_repository import (
    FilesystemCorrectionsArtifactRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.composition.filesystem import _CorrectionProjectionUnion
from librarytool.engine.correction_transforms import (
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    _build_commit_draft,
)
from librarytool.engine.correction_projection import (
    CorrectionAggregateProjector,
    CorrectionProjectionService,
    reconcile_correction_aggregates,
)
from librarytool.engine.corrections import (
    AssignImageCategoryCommand,
    CorrectionService,
    SetManualCaptionCommand,
)
from librarytool.engine.errors import ConflictError, NotFoundError, RepositoryError
from librarytool.engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
)
from librarytool.processing.raster import ManualBinaryAdjustRecipe


ITEM_ID = "book-1"
CAPTURE_ID = "capture-1"
ASSET_ID = "asset-1"
FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def _image(format_name: str, color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (24, 18), color).save(output, format=format_name)
    return output.getvalue()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _opaque_identity(namespace: str, *parts) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()[:40]}"


def _display_id() -> str:
    return f"{_opaque_identity('capture', CAPTURE_ID, ASSET_ID)}:display"


def _original_id() -> str:
    return f"{_opaque_identity('capture', CAPTURE_ID, ASSET_ID)}:original"


def _manifest(original: bytes, display: bytes) -> dict:
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "legacy_fallback": False,
        "assets": [
            {
                "asset_id": ASSET_ID,
                "capture_order": 1,
                "capture_file": "photo_1.jpg",
                "original": {
                    "reference": "phone-original.jpg",
                    "sha256": _sha256(original),
                    "revision": 3,
                    "width": 24,
                    "height": 18,
                    "orientation": 0,
                },
                "display": {
                    "reference": "photo_1.jpg",
                    "sha256": _sha256(display),
                    "revision": 4,
                    "width": 24,
                    "height": 18,
                    "orientation": 0,
                    "recipe": "desktop-standardize",
                    "recipe_version": "1",
                },
                "lifecycle": {"state": "completed"},
                "role": {},
                "geometry": [],
            }
        ],
        "selections": {},
        "transport": {"representation": "original", "version": 1},
        "desktop_import": {
            "version": 1,
            "assets": [
                {
                    "order": 0,
                    "asset_id": ASSET_ID,
                    "raw_ref": "orig_1.jpg",
                    "display_ref": "photo_1.jpg",
                    "source_checksum": _sha256(original),
                    "derivative_checksum": _sha256(display),
                    "transport_representation": "original",
                    "recipe": "desktop-standardize-v1",
                    "lifecycle": "completed",
                }
            ],
        },
    }


def _source(content: bytes, *, revision: str = "artifact-r1"):
    artifact = RasterArtifactView(
        key=RasterArtifactKey(ITEM_ID, _display_id()),
        revision=revision,
        kind="processed-image",
        media_type=("image/png" if content.startswith(b"\x89PNG") else "image/jpeg"),
        content_sha256=_sha256(content),
        dimensions=RasterDimensions(24, 18),
        source=RasterSourceRef(
            "capture",
            "capture-r1",
            _opaque_identity("capture", CAPTURE_ID, ASSET_ID),
            "canvas-r1",
        ),
        resource_state="available",
        resource=RasterResourceRef("resource:capture-display", "bytes-r1", "display"),
    )
    return CorrectionSourceSnapshot(artifact, "bytes-r1", content)


def _draft(
    source: CorrectionSourceSnapshot,
    operation_id: str,
    *,
    quad=FULL_FRAME,
):
    command = CorrectionTransformCommand(
        item_id=ITEM_ID,
        artifact_id=source.artifact.key.artifact_id,
        artifact_revision=source.artifact.revision,
        source_revision=source.source_revision,
        source_sha256=source.source_sha256,
        quad=quad,
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=5),
        rerun_ocr=False,
        operation_id=operation_id,
    )
    return _build_commit_draft(command, source, thumbnail_max_edge=64)


@contextmanager
def _lock():
    yield


def _stores(
    root: Path,
    source_box: dict[str, CorrectionSourceSnapshot],
    artifact_box: dict[str, RasterArtifactView],
    *,
    publish_hook=None,
    item_update_for=None,
    revision_for_publication=None,
    coordination_write_set=None,
):
    output = root / "output"
    captures = root / "captures"
    capture_dir = captures / CAPTURE_ID
    output.mkdir(parents=True, exist_ok=True)
    capture_dir.mkdir(parents=True, exist_ok=True)
    coordination = coordination_write_set or RecoverableWriteSet(output)
    transaction = RecoverableWriteSet(root, publish_hook=publish_hook)
    originals = FilesystemCaptureOriginalBackupStore(
        transaction,
        coordination_write_set=coordination,
        storage_root=output,
        capture_authority_root=captures,
        backup_root=output / "backups" / "originals",
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        capture_directory_for=lambda capture_id: captures / capture_id,
        artifact_for=lambda key: artifact_box.get(key.artifact_id),
        artifact_revision_for_publication=(
            revision_for_publication
            or (
                lambda _item_id, _capture_id, _artifact_id, _manifest, content: (
                    f"artifact:{_sha256(content)}"
                )
            )
        ),
        lock_context_for=_lock,
        item_updated_at_publication_for=item_update_for,
    )
    transforms = FilesystemCorrectionTransformStore(
        transaction,
        source_snapshot_for=lambda _key: source_box["value"],
        lock_context_for=_lock,
        storage_root=output,
        coordination_write_set=coordination,
        publication_plan_for=originals.plan_transform_publication,
        recover=False,
    )
    return transforms, originals, transaction, capture_dir, output


def _write_capture(capture_dir: Path, original: bytes, display: bytes) -> bytes:
    manifest = _manifest(original, display)
    payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (capture_dir / "orig_1.jpg").write_bytes(original)
    (capture_dir / "photo_1.jpg").write_bytes(display)
    (capture_dir / "photo_assets.json").write_bytes(payload)
    return payload


def _backup_path(output: Path, original: bytes) -> Path:
    digest = _sha256(original)
    return output / "backups" / "originals" / "v1" / "sha256" / digest[:2] / digest[2:]


def test_first_and_repeat_corrections_backup_once_then_restore_on_demand(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}
    root = tmp_path / "library"
    catalogue_path = root / "output" / "catalogue.json"
    repository_box = {}
    coordination = RecoverableWriteSet(root / "output")

    def advance_item(_item_id: str):
        catalogue = json.loads(catalogue_path.read_text("utf-8"))
        revision = f"item-r{int(catalogue['updated_at'].removeprefix('item-r')) + 1}"
        catalogue["updated_at"] = revision
        return catalogue_path, json.dumps(catalogue).encode("utf-8"), revision

    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        item_update_for=advance_item,
        coordination_write_set=coordination,
        revision_for_publication=lambda *args: repository_box[
            "value"
        ].capture_display_revision_for_publication(*args),
    )
    repository_write_set = coordination
    repository_box["value"] = FilesystemCorrectionsArtifactRepository(
        repository_write_set,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    catalogue_path.write_text(
        json.dumps({"updated_at": "item-r0"}),
        encoding="utf-8",
    )
    _write_capture(capture_dir, original, display)
    base_artifact = repository_box["value"].get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert base_artifact is not None and base_artifact.resource is not None
    source = CorrectionSourceSnapshot(
        base_artifact,
        base_artifact.resource.revision,
        display,
    )
    source_box["value"] = source
    artifact_box[_display_id()] = base_artifact
    first_draft = _draft(source, "transform-op-1")
    first = transforms.commit_transform(first_draft)

    backup_path = _backup_path(output, original)
    assert backup_path.read_bytes() == original
    assert not (capture_dir / "orig_1.jpg").exists()
    corrected_path = capture_dir / "photo_1.jpg"
    assert corrected_path.is_file()
    promoted = json.loads((capture_dir / "photo_assets.json").read_text("ascii"))
    imported = promoted["desktop_import"]["assets"][0]
    marker = imported["original_backup"]
    assert "raw_ref" not in imported
    assert marker == {
        "version": 1,
        "store": "output-originals-sha256",
        "key": f"sha256:{_sha256(original)}",
        "sha256": _sha256(original),
        "bytes": len(original),
        "media_type": "image/jpeg",
    }
    assert imported["active_desktop_correction_id"] == first.operation_id
    assert corrected_path.read_bytes() == display
    assert promoted["assets"][0]["display"] == _manifest(original, display)[
        "assets"
    ][0]["display"]
    assert promoted["assets"][0]["geometry"] == []
    assert isinstance(promoted["assets"][0]["lifecycle"]["updated_at"], int)
    assert json.loads(catalogue_path.read_text("utf-8"))["updated_at"] == "item-r1"
    first_updated_at = promoted["assets"][0]["lifecycle"]["updated_at"]

    first_head = transforms.project_item(ITEM_ID).display_heads[0]
    assert first_head.logical_key.artifact_id == _display_id()
    assert first_head.operation_id == first.operation_id
    assert first_head.artifact.resource is not None
    composed = _CorrectionProjectionUnion(
        repository_box["value"],
        transforms,
        write_set=repository_write_set,
        lock_context_for=_lock,
        original_backups=originals,
    )
    fresh_display = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert fresh_display is not None
    assert fresh_display.content_sha256 == first_head.artifact.content_sha256
    assert fresh_display.extensions["correction_display_head"]["operation_id"] == (
        first.operation_id
    )

    # The cold-backup manifest is the current-head authority. A retained op1
    # head must not project after the manifest advances to op2, in either the
    # detail or batched index path.
    manifest_path = capture_dir / "photo_assets.json"
    mismatched = json.loads(manifest_path.read_text("ascii"))
    mismatched["desktop_import"]["assets"][0][
        "active_desktop_correction_id"
    ] = "transform-op-2"
    manifest_path.write_text(json.dumps(mismatched), encoding="ascii")
    mismatched_base = repository_box["value"].get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    mismatched_display = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert mismatched_base is not None
    assert mismatched_display is not None
    assert mismatched_display.content_sha256 == mismatched_base.content_sha256
    assert "correction_display_head" not in mismatched_display.extensions
    assert composed.list_capture_index_hints_many([ITEM_ID])[ITEM_ID] == (
        repository_box["value"].list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    )

    mismatched["desktop_import"]["assets"][0][
        "active_desktop_correction_id"
    ] = first.operation_id
    manifest_path.write_text(json.dumps(mismatched), encoding="ascii")
    matched_display = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert matched_display is not None
    assert matched_display.content_sha256 == first_head.artifact.content_sha256
    assert matched_display.extensions["correction_display_head"][
        "operation_id"
    ] == first.operation_id
    matched_hints = composed.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    base_hints = repository_box["value"].list_capture_index_hints_many(
        [ITEM_ID]
    )[ITEM_ID]
    assert matched_hints[0]["revision"] != base_hints[0]["revision"]

    second_source = CorrectionSourceSnapshot(
        first_head.artifact,
        first_head.artifact.resource.revision,
        first_draft.output("corrected-display").content,
        annotations=first_head.spatial_annotations,
    )
    source_box["value"] = second_source
    backup_before = backup_path.read_bytes()
    second = transforms.commit_transform(_draft(second_source, "transform-op-2"))
    assert second.operation_id == "transform-op-2"
    assert backup_path.read_bytes() == backup_before
    promoted = json.loads((capture_dir / "photo_assets.json").read_text("ascii"))
    assert (
        promoted["desktop_import"]["assets"][0]["active_desktop_correction_id"]
        == "transform-op-2"
    )
    assert corrected_path.read_bytes() == display
    assert json.loads(catalogue_path.read_text("utf-8"))["updated_at"] == "item-r2"

    second_head = transforms.project_item(ITEM_ID).display_heads[0]
    assert second_head.operation_id == second.operation_id
    current = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert current is not None
    assert current.content_sha256 == second_head.artifact.content_sha256
    assert current.extensions["correction_display_head"]["operation_id"] == (
        second.operation_id
    )

    # Restore clears the active operation before deleting the retained head.
    # That intermediate durable state must already fall back to the physical
    # display rather than reactivating the last correction.
    without_active = json.loads(manifest_path.read_text("ascii"))
    without_active["desktop_import"]["assets"][0].pop(
        "active_desktop_correction_id"
    )
    manifest_path.write_text(json.dumps(without_active), encoding="ascii")
    inactive_base = repository_box["value"].get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    inactive_display = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert inactive_base is not None
    assert inactive_display is not None
    assert inactive_display.content_sha256 == inactive_base.content_sha256
    assert "correction_display_head" not in inactive_display.extensions
    assert composed.list_capture_index_hints_many([ITEM_ID])[ITEM_ID] == (
        repository_box["value"].list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    )

    without_active["desktop_import"]["assets"][0][
        "active_desktop_correction_id"
    ] = second.operation_id
    manifest_path.write_text(json.dumps(without_active), encoding="ascii")
    current = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert current is not None
    assert current.content_sha256 == second_head.artifact.content_sha256
    assert current.extensions["correction_display_head"]["operation_id"] == (
        second.operation_id
    )
    artifact_box[_display_id()] = current
    resolved = originals.resolve_original_backup(
        ITEM_ID,
        _display_id(),
        current.revision,
    )
    assert resolved is not None
    try:
        assert resolved.stream.read() == original
        assert resolved.content_sha256 == _sha256(original)
        assert resolved.revision == current.revision
    finally:
        resolved.stream.close()

    receipt = originals.restore_original_backup(
        ITEM_ID,
        _display_id(),
        current.revision,
        "restore-op-1",
    )
    assert receipt["replayed"] is False
    assert receipt["backup_sha256"] == _sha256(original)
    head_path = correction_display_head_path(output, ITEM_ID, _display_id())
    assert not head_path.exists()
    assert transforms.project_item(ITEM_ID).display_heads == ()
    projected = next(
        artifact
        for artifact in repository_box["value"].list_raster_artifacts(ITEM_ID)
        if artifact.key.artifact_id == _display_id()
    )
    assert receipt["after_revision"] == projected.revision
    restored = json.loads((capture_dir / "photo_assets.json").read_text("ascii"))
    restored_import = restored["desktop_import"]["assets"][0]
    assert "active_desktop_correction_id" not in restored_import
    assert restored_import["original_backup"] == marker
    assert (capture_dir / restored_import["display_ref"]).read_bytes() == original
    assert backup_path.read_bytes() == original
    assert restored["assets"][0]["display"]["orientation"] == 0
    assert isinstance(restored["assets"][0]["lifecycle"]["updated_at"], int)
    assert restored["assets"][0]["lifecycle"]["updated_at"] > first_updated_at
    assert json.loads(catalogue_path.read_text("utf-8"))["updated_at"] == "item-r3"

    replay = originals.restore_original_backup(
        ITEM_ID,
        _display_id(),
        current.revision,
        "restore-op-1",
    )
    assert replay == {**receipt, "replayed": True}
    with pytest.raises(ConflictError) as already_restored:
        originals.restore_original_backup(
            ITEM_ID,
            _display_id(),
            current.revision,
            "restore-op-2",
        )
    assert already_restored.value.code == "capture_display_already_original"
    assert not any(
        child.is_dir()
        for child in (root / ".transactions").iterdir()
    )


def test_transform_and_restore_receipts_are_terminal_host_publications(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}
    published: list[Path] = []
    root = tmp_path / "library"
    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        publish_hook=lambda _index, target: published.append(target),
    )
    _write_capture(capture_dir, original, display)

    transforms.commit_transform(_draft(source, "terminal-transform"))

    transform_receipt = (
        output
        / ".engine"
        / "receipts"
        / "correction-transforms"
        / f"{_sha256(b'terminal-transform')}.json"
    )
    manifest_path = capture_dir / "photo_assets.json"
    head_path = correction_display_head_path(output, ITEM_ID, _display_id())
    assert published[-1] == transform_receipt
    assert published.index(head_path) < published.index(manifest_path)
    assert published.index(manifest_path) < published.index(transform_receipt)

    head = transforms.project_item(ITEM_ID).display_heads[0]
    artifact_box[_display_id()] = replace(
        source.artifact,
        revision=head.artifact.revision,
    )
    published.clear()
    originals.restore_original_backup(
        ITEM_ID,
        _display_id(),
        artifact_box[_display_id()].revision,
        "terminal-restore",
    )

    restore_receipt = (
        output
        / ".engine"
        / "receipts"
        / "original-restores"
        / f"{_sha256(b'terminal-restore')}.json"
    )
    assert published[-1] == restore_receipt
    assert published.index(head_path) < published.index(manifest_path)
    assert published.index(manifest_path) < published.index(restore_receipt)


def test_promotion_and_restore_invalidate_cached_hints_without_cold_reads(
    tmp_path,
    monkeypatch,
):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}
    root = tmp_path / "library"
    coordination = RecoverableWriteSet(root / "output")
    repository_box = {}
    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        revision_for_publication=lambda *args: repository_box[
            "value"
        ].capture_display_revision_for_publication(*args),
        coordination_write_set=coordination,
    )
    repository = FilesystemCorrectionsArtifactRepository(
        coordination,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    repository_box["value"] = repository
    _write_capture(capture_dir, original, display)

    builds = {"count": 0}
    build_hints = repository._capture_index_hints

    def count_hint_builds(*args, **kwargs):
        builds["count"] += 1
        return build_hints(*args, **kwargs)

    monkeypatch.setattr(repository, "_capture_index_hints", count_hint_builds)

    def display_detail():
        value = repository.get_raster_artifact(
            RasterArtifactKey(ITEM_ID, _display_id())
        )
        assert value is not None
        return value

    before_hints = repository.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    before_detail = display_detail()
    assert builds["count"] == 1

    transforms.commit_transform(_draft(source, "cache-promotion"))
    promoted_manifest = json.loads(
        (capture_dir / "photo_assets.json").read_text("ascii")
    )
    marker = promoted_manifest["desktop_import"]["assets"][0][
        "original_backup"
    ]
    backup_root = output / "backups" / "originals"
    backup_path = _backup_path(output, original)
    guard = {"enabled": True}
    real_lstat = Path.lstat
    real_path_open = Path.open
    real_os_open = os.open

    def cold_target(value) -> bool:
        if isinstance(value, int):
            return False
        candidate = Path(os.fsdecode(value))
        if candidate.name in {"orig_1.jpg", "phone-original.jpg"}:
            return True
        try:
            candidate.relative_to(backup_root)
        except ValueError:
            return False
        return True

    def reject_cold_lstat(path, *args, **kwargs):
        if guard["enabled"] and cold_target(path):
            raise AssertionError("a cached projection statted cold original data")
        return real_lstat(Path(path), *args, **kwargs)

    def reject_cold_path_open(path, *args, **kwargs):
        if guard["enabled"] and cold_target(path):
            raise AssertionError("a cached projection opened cold original data")
        return real_path_open(Path(path), *args, **kwargs)

    def reject_cold_os_open(path, flags, *args, **kwargs):
        if guard["enabled"] and cold_target(path):
            raise AssertionError("a cached projection opened cold original data")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", reject_cold_lstat)
    monkeypatch.setattr(Path, "open", reject_cold_path_open)
    monkeypatch.setattr(os, "open", reject_cold_os_open)

    promoted_cold = repository.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    promoted_warm = repository.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    promoted_detail = display_detail()
    assert builds["count"] == 2
    assert promoted_warm == promoted_cold
    assert promoted_cold[0]["revision"] != before_hints[0]["revision"]
    assert promoted_detail.revision != before_detail.revision

    guard["enabled"] = False
    artifact_box[_display_id()] = promoted_detail
    originals.restore_original_backup(
        ITEM_ID,
        _display_id(),
        promoted_detail.revision,
        "cache-restore",
    )
    guard["enabled"] = True

    restored_cold = repository.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    restored_warm = repository.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    restored_detail = display_detail()
    assert builds["count"] == 3
    assert restored_warm == restored_cold
    assert restored_cold[0]["revision"] != promoted_cold[0]["revision"]
    assert restored_detail.revision != promoted_detail.revision

    for hints, detail in (
        (promoted_cold, promoted_detail),
        (restored_cold, restored_detail),
    ):
        encoded = json.dumps(
            {"hints": hints, "detail": detail.as_dict()},
            sort_keys=True,
        )
        assert marker["store"] not in encoded
        assert marker["key"] not in encoded
        assert "orig_1.jpg" not in encoded
        assert "phone-original.jpg" not in encoded
        assert str(backup_path) not in encoded.replace("\\\\", "\\")


def test_restore_receipt_revision_includes_durable_human_overlays(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}
    projection_box = {}
    root = tmp_path / "library"
    coordination = RecoverableWriteSet(root / "output")

    def public_revision(*args):
        value, annotations = projection_box[
            "base"
        ].capture_display_projection_for_publication(*args)
        return projection_box["service"].raster_revision_for_publication(
            value,
            annotations,
        )

    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        revision_for_publication=public_revision,
        coordination_write_set=coordination,
    )
    _write_capture(capture_dir, original, display)
    manifest = json.loads((capture_dir / "photo_assets.json").read_text("utf-8"))
    manifest["assets"][0]["geometry"] = [
        {
            "asset_id": ASSET_ID,
            "source_sha256": _sha256(original),
            "source_revision": 3,
            "display_revision": 4,
            "coordinate_space": "display_normalized",
            "width": 24,
            "height": 18,
            "orientation": 0,
            "display_sha256": _sha256(display),
            "engine": "test-ocr",
            "model": "test-model",
            "regions": [
                {
                    "id": "restore-region",
                    "type": "text",
                    "text": "Original-source geometry",
                    "polygon": [
                        [0.1, 0.1],
                        [0.9, 0.1],
                        [0.9, 0.9],
                        [0.1, 0.9],
                    ],
                }
            ],
        }
    ]
    (capture_dir / "photo_assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    base = FilesystemCorrectionsArtifactRepository(
        coordination,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    composed = _CorrectionProjectionUnion(
        base,
        transforms,
        write_set=coordination,
        lock_context_for=_lock,
        original_backups=originals,
    )
    aggregate_projector = CorrectionAggregateProjector(composed, composed)
    correction_repository = FilesystemCorrectionRepository(
        coordination,
        load_aggregate=aggregate_projector.project,
        reconcile_aggregate=reconcile_correction_aggregates,
        lock_context_for=_lock,
        recover=False,
    )
    commands = CorrectionService(correction_repository)
    projection = CorrectionProjectionService(
        composed,
        composed,
        correction_repository,
    )
    projection_box.update(base=base, service=projection)

    current = projection.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert current is not None
    commands.assign_category(
        AssignImageCategoryCommand(
            ITEM_ID,
            _display_id(),
            current.revision,
            "content_specimen",
            "assign-category-before-restore",
        )
    )
    current = projection.get_raster_artifact(current.key)
    assert current is not None
    commands.set_manual_caption(
        SetManualCaptionCommand(
            ITEM_ID,
            _display_id(),
            current.revision,
            "Retained human caption",
            "caption-before-restore",
        )
    )

    current = projection.get_raster_artifact(current.key)
    assert current is not None and current.resource is not None
    current_geometry = projection.list_spatial_annotations(ITEM_ID)
    assert len(current_geometry) == 1
    transform_source = CorrectionSourceSnapshot(
        current,
        current.resource.revision,
        display,
        annotations=current_geometry,
    )
    source_box["value"] = transform_source
    transforms.commit_transform(
        _draft(transform_source, "transform-before-restore")
    )
    current = projection.get_raster_artifact(current.key)
    assert current is not None
    head = transforms.project_item(ITEM_ID).display_heads[0]
    assert current.content_sha256 == head.artifact.content_sha256
    assert current.extensions["correction_display_head"]["operation_id"] == (
        head.operation_id
    )
    assert len(base.list_spatial_annotations(ITEM_ID)) == 1
    assert current.effective_category == "content_specimen"
    assert current.effective_caption is not None
    assert current.effective_caption.text == "Retained human caption"
    artifact_box[_display_id()] = current

    receipt = originals.restore_original_backup(
        ITEM_ID,
        _display_id(),
        current.revision,
        "restore-with-human-overlays",
    )

    fresh_projection = CorrectionProjectionService(
        composed,
        composed,
        correction_repository,
    )
    fresh = fresh_projection.get_raster_artifact(current.key)
    assert fresh is not None
    assert receipt["after_revision"] == fresh.revision
    assert fresh.effective_category == "content_specimen"
    assert fresh.effective_caption is not None
    assert fresh.effective_caption.text == "Retained human caption"
    assert transforms.project_item(ITEM_ID).display_heads == ()
    assert base.list_spatial_annotations(ITEM_ID) == ()


def test_display_head_maps_annotations_without_rewriting_base_geometry(
    tmp_path,
):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    root = tmp_path / "library"
    output = root / "output"
    capture_dir = root / "captures" / CAPTURE_ID
    output.mkdir(parents=True)
    capture_dir.mkdir(parents=True)
    manifest = _manifest(original, display)
    # The phone display declaration can differ from the effective desktop
    # derivative. The base record must remain byte-authoritative while the
    # mutable display head carries the transform's mapped annotation view.
    manifest["assets"][0]["display"]["sha256"] = _sha256(original)
    manifest["assets"][0]["geometry"] = [
        {
            "asset_id": ASSET_ID,
            "source_sha256": _sha256(original),
            "source_revision": 3,
            "display_revision": 4,
            "coordinate_space": "display_normalized",
            "width": 24,
            "height": 18,
            "orientation": 0,
            "display_sha256": _sha256(display),
            "engine": "provider-engine",
            "model": "provider-model",
            "engine_version": "provider-v7",
            "regions": [
                {
                    "id": "kept-region",
                    "type": "text",
                    "text": "Kept provider text",
                    "confidence": 0.91,
                    "polygon": [
                        [0.1, 0.1],
                        [0.4, 0.1],
                        [0.4, 0.4],
                        [0.1, 0.4],
                    ],
                },
                {
                    "id": "clipped-region",
                    "type": "image",
                    "text": "Outside crop",
                    "confidence": 0.82,
                    "polygon": [
                        [0.7, 0.7],
                        [0.9, 0.7],
                        [0.9, 0.9],
                        [0.7, 0.9],
                    ],
                },
            ],
        }
    ]
    (capture_dir / "orig_1.jpg").write_bytes(original)
    (capture_dir / "photo_1.jpg").write_bytes(display)
    (capture_dir / "photo_assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    repository = FilesystemCorrectionsArtifactRepository(
        RecoverableWriteSet(output),
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    artifact = next(
        value
        for value in repository.list_raster_artifacts(ITEM_ID)
        if value.key.artifact_id == _display_id()
    )
    before = repository.list_spatial_annotations(ITEM_ID)
    kept = next(value for value in before if value.label == "Kept provider text")
    clipped = next(value for value in before if value.label == "Outside crop")
    source = CorrectionSourceSnapshot(
        artifact,
        artifact.resource.revision,
        display,
        annotations=before,
    )
    transforms, _originals, _transaction, _capture_dir, _output = _stores(
        root,
        {"value": source},
        {_display_id(): artifact},
    )
    draft = _draft(
        source,
        "transform-geometry-promotion",
        quad=((0.0, 0.0), (0.55, 0.0), (0.55, 0.55), (0.0, 0.55)),
    )
    mapped = next(
        value
        for value in draft.mapped_annotations
        if value.annotation_id == kept.key.annotation_id
    )
    assert draft.dropped_annotation_ids == (clipped.key.annotation_id,)

    transforms.commit_transform(draft)

    updated = json.loads((capture_dir / "photo_assets.json").read_text("ascii"))
    updated_asset = updated["assets"][0]
    assert updated_asset["display"] == manifest["assets"][0]["display"]
    assert updated_asset["geometry"] == manifest["assets"][0]["geometry"]
    assert (capture_dir / "photo_1.jpg").read_bytes() == display

    head = transforms.project_item(ITEM_ID).display_heads[0]
    assert head.logical_key.artifact_id == _display_id()
    assert len(head.spatial_annotations) == 1
    assert head.spatial_annotations[0].label == kept.label
    assert head.spatial_annotations[0].selector.points == mapped.points
    after = repository.list_spatial_annotations(ITEM_ID)
    assert [value.key.annotation_id for value in after] == [
        value.key.annotation_id for value in before
    ]


def test_head_publication_preserves_a_legacy_blank_base_display_checksum(
    tmp_path,
):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    placeholder = _source(display)
    source_box = {"value": placeholder}
    artifact_box = {_display_id(): placeholder.artifact}
    root = tmp_path / "library"
    coordination = RecoverableWriteSet(root / "output")
    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        coordination_write_set=coordination,
    )
    manifest = _manifest(original, display)
    manifest["assets"][0]["display"]["sha256"] = ""
    manifest["desktop_import"]["assets"][0]["derivative_checksum"] = ""
    (capture_dir / "orig_1.jpg").write_bytes(original)
    (capture_dir / "photo_1.jpg").write_bytes(display)
    (capture_dir / "photo_assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    base = FilesystemCorrectionsArtifactRepository(
        coordination,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    base_artifact = base.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert base_artifact is not None
    assert base_artifact.resource is not None
    source = CorrectionSourceSnapshot(
        base_artifact,
        base_artifact.resource.revision,
        display,
    )
    source_box["value"] = source
    artifact_box[_display_id()] = base_artifact

    result = transforms.commit_transform(
        _draft(source, "transform-blank-display-sha")
    )

    manifest_path = capture_dir / "photo_assets.json"
    updated = json.loads(manifest_path.read_text("ascii"))
    assert updated["assets"][0]["display"]["sha256"] == ""
    assert updated["desktop_import"]["assets"][0]["derivative_checksum"] == ""
    assert (capture_dir / "photo_1.jpg").read_bytes() == display

    # Blank legacy checksums cannot authorize an index head, but the private
    # operation pin still makes matching detail projection possible and a
    # mismatched retained head fail closed.
    authority = base.capture_index_hint_snapshot(ITEM_ID)["authorities"][
        _display_id().casefold()
    ]
    assert authority["source_sha256"] == ""
    assert authority["source_revision"] == ""
    assert authority["active_operation_id"] == result.operation_id
    composed = _CorrectionProjectionUnion(
        base,
        transforms,
        write_set=coordination,
        lock_context_for=_lock,
        original_backups=originals,
    )
    head = transforms.project_item(ITEM_ID).display_heads[0]
    matching = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert matching is not None
    assert matching.content_sha256 == head.artifact.content_sha256
    assert matching.extensions["correction_display_head"]["operation_id"] == (
        result.operation_id
    )
    assert composed.list_capture_index_hints_many([ITEM_ID]) == (
        base.list_capture_index_hints_many([ITEM_ID])
    )

    updated["desktop_import"]["assets"][0][
        "active_desktop_correction_id"
    ] = "different-operation"
    manifest_path.write_text(json.dumps(updated), encoding="ascii")
    mismatched_base = base.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    mismatched = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert mismatched_base is not None
    assert mismatched is not None
    assert mismatched.content_sha256 == mismatched_base.content_sha256
    assert "correction_display_head" not in mismatched.extensions


def test_image_verification_rejects_pillow_decompression_bombs(monkeypatch):
    def bomb(_stream):
        raise Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(Image, "open", bomb)
    with pytest.raises(RepositoryError) as verification:
        FilesystemCaptureOriginalBackupStore._verified_image_media_type(b"jpeg")
    assert verification.value.code == "invalid_capture_original_backup"
    with pytest.raises(RepositoryError) as promotion:
        FilesystemCaptureOriginalBackupStore._jpeg_display(b"png")
    assert promotion.value.code == "invalid_capture_display_promotion"


def test_original_backup_actions_reject_non_capture_artifacts_as_not_found(
    tmp_path,
):
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    foreign = replace(
        source.artifact,
        key=RasterArtifactKey(ITEM_ID, "entry-artifact"),
    )
    _transforms, originals, _transaction, _capture_dir, _output = _stores(
        tmp_path / "library",
        {"value": source},
        {foreign.key.artifact_id: foreign},
    )

    with pytest.raises(NotFoundError) as raised:
        originals.resolve_original_backup(
            ITEM_ID,
            foreign.key.artifact_id,
            foreign.revision,
        )
    assert raised.value.code == "raster_artifact_not_found"


def test_corrupt_existing_content_address_aborts_without_deleting_original(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}
    transforms, _originals, _transaction, capture_dir, output = _stores(
        tmp_path / "library",
        source_box,
        artifact_box,
    )
    manifest_before = _write_capture(capture_dir, original, display)
    backup_path = _backup_path(output, original)
    backup_path.parent.mkdir(parents=True)
    backup_path.write_bytes(_image("JPEG", (200, 10, 10)))

    with pytest.raises(ConflictError) as raised:
        transforms.commit_transform(_draft(source, "transform-op-corrupt"))

    assert raised.value.code == "capture_original_backup_conflict"
    assert (capture_dir / "orig_1.jpg").read_bytes() == original
    assert (capture_dir / "photo_1.jpg").read_bytes() == display
    assert (capture_dir / "photo_assets.json").read_bytes() == manifest_before


def test_conflicting_original_sha_anchors_abort_without_writes(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    transforms, _originals, _transaction, capture_dir, output = _stores(
        tmp_path / "library",
        {"value": source},
        {_display_id(): source.artifact},
    )
    manifest = _manifest(original, display)
    manifest["assets"][0]["original"]["sha256"] = "f" * 64
    manifest_before = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (capture_dir / "orig_1.jpg").write_bytes(original)
    (capture_dir / "photo_1.jpg").write_bytes(display)
    (capture_dir / "photo_assets.json").write_bytes(manifest_before)

    with pytest.raises(ConflictError) as raised:
        transforms.commit_transform(_draft(source, "transform-anchor-conflict"))

    assert raised.value.code == "capture_original_sha256_conflict"
    assert (capture_dir / "orig_1.jpg").read_bytes() == original
    assert (capture_dir / "photo_1.jpg").read_bytes() == display
    assert (capture_dir / "photo_assets.json").read_bytes() == manifest_before
    assert not (output / "backups" / "originals").exists()


def test_non_jpeg_capture_original_is_not_moved_into_the_jpeg_backup_contract(
    tmp_path,
):
    original = _image("PNG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    transforms, _originals, _transaction, capture_dir, output = _stores(
        tmp_path / "library",
        {"value": source},
        {_display_id(): source.artifact},
    )
    manifest_before = _write_capture(capture_dir, original, display)

    with pytest.raises(RepositoryError) as raised:
        transforms.commit_transform(_draft(source, "transform-png-original"))

    assert getattr(raised.value, "code", "") == "invalid_capture_original_backup"
    assert (capture_dir / "orig_1.jpg").read_bytes() == original
    assert (capture_dir / "photo_1.jpg").read_bytes() == display
    assert (capture_dir / "photo_assets.json").read_bytes() == manifest_before
    assert not (output / "backups" / "originals").exists()


def test_backup_marker_rejects_even_an_empty_live_original_reference():
    digest = "a" * 64
    imported = {
        "raw_ref": "",
        "source_checksum": digest,
        "original_backup": {
            "version": 1,
            "store": "output-originals-sha256",
            "key": f"sha256:{digest}",
            "sha256": digest,
            "bytes": 10,
            "media_type": "image/jpeg",
        },
    }
    with pytest.raises(ValueError, match="original_backup"):
        parse_original_backup_marker(imported, {"sha256": digest})


def test_legacy_capture_without_manifest_keeps_transform_history_behavior(tmp_path):
    display = _image("JPEG", (40, 50, 60))
    placeholder = _source(display)
    source_box = {"value": placeholder}
    artifact_box = {_display_id(): placeholder.artifact}
    root = tmp_path / "library"
    coordination = RecoverableWriteSet(root / "output")
    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        coordination_write_set=coordination,
    )
    (capture_dir / "orig_1.jpg").write_bytes(display)
    (capture_dir / "photo_1.jpg").write_bytes(display)
    base = FilesystemCorrectionsArtifactRepository(
        coordination,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    base_artifact = next(
        value
        for value in base.list_raster_artifacts(ITEM_ID)
        if value.key.artifact_id.endswith(":display")
    )
    assert base_artifact.resource is not None
    source = CorrectionSourceSnapshot(
        base_artifact,
        base_artifact.resource.revision,
        display,
    )
    source_box["value"] = source
    artifact_box[base_artifact.key.artifact_id] = base_artifact

    result = transforms.commit_transform(_draft(source, "legacy-transform"))

    assert result.operation_id == "legacy-transform"
    assert (capture_dir / "orig_1.jpg").read_bytes() == display
    assert (capture_dir / "photo_1.jpg").read_bytes() == display
    assert not (output / "backups" / "originals").exists()
    composed = _CorrectionProjectionUnion(
        base,
        transforms,
        write_set=coordination,
        lock_context_for=_lock,
        original_backups=originals,
    )
    projected = composed.get_raster_artifact(base_artifact.key)
    assert projected is not None
    assert projected.content_sha256 != base_artifact.content_sha256
    assert projected.extensions["correction_display_head"]["operation_id"] == (
        result.operation_id
    )


def test_transform_started_from_original_projects_the_logical_display_head(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    display_source = _source(display)
    source_box = {"value": display_source}
    artifact_box = {_display_id(): display_source.artifact}
    root = tmp_path / "library"
    coordination = RecoverableWriteSet(root / "output")
    transforms, originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        coordination_write_set=coordination,
    )
    _write_capture(capture_dir, original, display)
    base = FilesystemCorrectionsArtifactRepository(
        coordination,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda item_id: CAPTURE_ID if item_id == ITEM_ID else None,
        entry_directory_for=lambda item_id: output / "entries" / item_id,
        capture_directory_for=lambda capture_id: capture_dir,
        capture_authority_root=root / "captures",
        representation_revision_for=lambda _item_id, _source_id: None,
        lock_context_for=_lock,
    )
    original_artifact = base.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _original_id())
    )
    display_artifact = base.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert original_artifact is not None and original_artifact.resource is not None
    assert display_artifact is not None
    original_source = CorrectionSourceSnapshot(
        original_artifact,
        original_artifact.resource.revision,
        original,
    )
    source_box["value"] = original_source
    artifact_box[_display_id()] = display_artifact

    result = transforms.commit_transform(
        _draft(original_source, "transform-from-original")
    )

    manifest = json.loads((capture_dir / "photo_assets.json").read_text("ascii"))
    imported = manifest["desktop_import"]["assets"][0]
    assert imported["active_desktop_correction_id"] == result.operation_id
    assert "raw_ref" not in imported
    assert _backup_path(output, original).read_bytes() == original
    assert not (capture_dir / "orig_1.jpg").exists()
    assert (capture_dir / "photo_1.jpg").read_bytes() == display
    assert manifest["assets"][0]["original"] == _manifest(original, display)[
        "assets"
    ][0]["original"]
    assert manifest["assets"][0]["display"] == _manifest(original, display)[
        "assets"
    ][0]["display"]

    heads = transforms.project_item(ITEM_ID).display_heads
    assert len(heads) == 1
    head = heads[0]
    assert head.logical_key == RasterArtifactKey(ITEM_ID, _display_id())
    assert head.root_key == original_artifact.key
    assert head.operation_id == result.operation_id
    assert correction_display_head_path(
        output,
        ITEM_ID,
        _display_id(),
    ).is_file()

    # The original has moved cold, so routine projections cannot resolve it.
    # Its validated backup authority still activates the head without reading
    # the backup, and only the sibling logical display is replaced.
    assert base.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _original_id())
    ) is None
    composed = _CorrectionProjectionUnion(
        base,
        transforms,
        write_set=coordination,
        lock_context_for=_lock,
        original_backups=originals,
    )
    projected = composed.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, _display_id())
    )
    assert projected is not None
    assert projected.resource_state == "available"
    assert projected.content_sha256 == head.artifact.content_sha256
    assert projected.extensions["correction_display_head"]["operation_id"] == (
        result.operation_id
    )
    projected_hints = composed.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    base_hints = base.list_capture_index_hints_many([ITEM_ID])[ITEM_ID]
    assert projected_hints[0]["revision"] != base_hints[0]["revision"]


def test_interrupted_atomic_promotion_recovers_original_display_and_manifest(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}

    def crash_before_manifest(_index: int, target: Path) -> None:
        if target.name == "photo_assets.json":
            raise SystemExit("simulated process loss")

    transforms, _originals, _transaction, capture_dir, output = _stores(
        tmp_path / "library",
        source_box,
        artifact_box,
        publish_hook=crash_before_manifest,
    )
    manifest_before = _write_capture(capture_dir, original, display)

    with pytest.raises(SystemExit, match="simulated process loss"):
        transforms.commit_transform(_draft(source, "transform-op-crash"))

    head_path = correction_display_head_path(output, ITEM_ID, _display_id())
    assert head_path.is_file()
    restarted = RecoverableWriteSet(tmp_path / "library")
    results = restarted.recover_all()
    assert any(result.action == "rolled_back_interrupted" for result in results)
    assert (capture_dir / "orig_1.jpg").read_bytes() == original
    assert (capture_dir / "photo_1.jpg").read_bytes() == display
    assert (capture_dir / "photo_assets.json").read_bytes() == manifest_before
    assert not _backup_path(output, original).exists()
    assert not head_path.exists()


def test_interrupted_restore_rolls_back_display_manifest_and_item_revision(tmp_path):
    original = _image("JPEG", (10, 20, 30))
    display = _image("JPEG", (40, 50, 60))
    source = _source(display)
    source_box = {"value": source}
    artifact_box = {_display_id(): source.artifact}
    root = tmp_path / "library"
    catalogue_path = root / "output" / "catalogue.json"

    def advance_item(_item_id: str):
        catalogue = json.loads(catalogue_path.read_text("utf-8"))
        catalogue["updated_at"] += "-next"
        return (
            catalogue_path,
            json.dumps(catalogue).encode("utf-8"),
            catalogue["updated_at"],
        )

    transforms, _originals, _transaction, capture_dir, output = _stores(
        root,
        source_box,
        artifact_box,
        item_update_for=advance_item,
    )
    catalogue_path.write_text(
        json.dumps({"updated_at": "item-r1"}),
        encoding="utf-8",
    )
    _write_capture(capture_dir, original, display)
    transforms.commit_transform(_draft(source, "transform-before-restore"))
    head_path = correction_display_head_path(output, ITEM_ID, _display_id())
    head_before = head_path.read_bytes()
    current = replace(source.artifact, revision="public-display-r2")
    artifact_box[_display_id()] = current
    manifest_before = (capture_dir / "photo_assets.json").read_bytes()
    display_before = (capture_dir / "photo_1.jpg").read_bytes()
    catalogue_before = catalogue_path.read_bytes()

    def crash_before_manifest(_index: int, target: Path) -> None:
        if target.name == "photo_assets.json":
            raise SystemExit("simulated restore process loss")

    _transforms, originals, _transaction, _capture_dir, _output = _stores(
        root,
        source_box,
        artifact_box,
        publish_hook=crash_before_manifest,
        item_update_for=advance_item,
    )
    with pytest.raises(SystemExit, match="simulated restore process loss"):
        originals.restore_original_backup(
            ITEM_ID,
            _display_id(),
            current.revision,
            "restore-crash",
        )

    assert not head_path.exists()
    results = RecoverableWriteSet(root).recover_all()
    assert any(result.action == "rolled_back_interrupted" for result in results)
    assert (capture_dir / "photo_assets.json").read_bytes() == manifest_before
    assert (capture_dir / "photo_1.jpg").read_bytes() == display_before
    assert catalogue_path.read_bytes() == catalogue_before
    assert head_path.read_bytes() == head_before
