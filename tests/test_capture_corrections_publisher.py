"""Desktop capture-corrections publisher (docs/capture-corrections-sync.md)."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import threading
from pathlib import Path

import libcommon as lib
import pytest
import server
from librarytool.adapters.filesystem.corrections_artifact_repository import (
    _opaque_identity,
)
from librarytool.engine.correction_transforms import CorrectionTransformCommand
from PIL import Image


OWNER_ID = "0f0e0d0c-0b0a-4a09-8807-060504030201"
_NO_ACTIVE_CORRECTION = object()


@pytest.fixture()
def capture_workspace(monkeypatch, tmp_path):
    """Isolated stores + engine session, as in test_capture_archive_import."""

    workspace = tmp_path / "output"
    workspace.mkdir()
    monkeypatch.setattr(
        lib,
        "MANUAL_ENTRIES_PATH",
        workspace / "manual_entries.json",
    )
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


def _png(width: int, height: int, color=(200, 120, 40)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (width, height), color).save(stream, format="PNG")
    return stream.getvalue()


def _jpeg(seed: str) -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), tuple(digest[:3])).save(
        stream,
        format="JPEG",
        quality=92,
    )
    return stream.getvalue()


def _photo_assets(capture_id: str, assets) -> dict:
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": capture_id,
        "assets": [],
        "desktop_import": {
            "version": 1,
            "imported_at": "2026-08-01T00:00:00+00:00",
            "assets": [{
                "order": index,
                "asset_id": asset_id,
                "raw_ref": f"orig_{index + 1}.jpg",
                "display_ref": f"photo_{index + 1}.jpg",
                "source_checksum": checksum,
                "derivative_checksum": "e" * 64,
                "transport_representation": "original",
                "recipe": "desktop_perspective_standardize_v1",
                "lifecycle": lifecycle,
            } for index, (asset_id, checksum, lifecycle) in enumerate(assets)],
        },
    }


def _mark_original_backed_up(
        photo_assets: dict, asset_id: str, source_sha256: str, *,
        active_operation_id=_NO_ACTIVE_CORRECTION) -> None:
    imported = next(
        row for row in photo_assets["desktop_import"]["assets"]
        if row["asset_id"] == asset_id
    )
    imported.pop("raw_ref")
    imported["original_backup"] = {
        "version": 1,
        "store": "output-originals-sha256",
        "key": f"sha256:{source_sha256}",
        "sha256": source_sha256,
        "bytes": 321,
        "media_type": "image/jpeg",
    }
    if active_operation_id is not _NO_ACTIVE_CORRECTION:
        imported["active_desktop_correction_id"] = active_operation_id


def _publish_transform(engine_root, item_id: str, operation_id: str,
                       source_artifact_id: str, display_artifact_id: str,
                       png: bytes, *, generated_at: str = "",
                       pointer_mtime: float | None = None,
                       source_revision: str = "", source_sha256: str = "",
                       output_revision: str = "", committed: bool = True,
                       display_head: bool = False,
                       display_head_artifact_id: str = "",
                       legacy_missing_pins: bool = False) -> str:
    """Fabricate one committed v2 publication the way the store lays it out."""

    assert bool(source_revision) is bool(source_sha256)
    assert committed or not display_head
    transforms = engine_root / ".engine" / "correction-transforms"
    operation_digest = hashlib.sha256(
        operation_id.encode("utf-8")).hexdigest()
    image = Image.open(io.BytesIO(png))
    if not source_revision:
        parent_outputs = [
            output
            for path in (transforms / "publications").glob("*.json")
            for publication in (json.loads(path.read_text("utf-8")),)
            for output in publication.get("outputs", ())
            if output.get("artifact_id") == source_artifact_id
        ] if (transforms / "publications").is_dir() else []
        if len(parent_outputs) == 1:
            source_revision = parent_outputs[0]["artifact_revision"]
            source_sha256 = parent_outputs[0]["content_sha256"]
    source_revision = source_revision or (
        "source:" + hashlib.sha256(
            f"source-revision:{operation_id}".encode("utf-8")
        ).hexdigest()
    )
    source_sha256 = source_sha256 or hashlib.sha256(
        f"source-content:{operation_id}".encode("utf-8")
    ).hexdigest()
    command_value = CorrectionTransformCommand(
        item_id=item_id,
        artifact_id=source_artifact_id,
        artifact_revision=(
            "artifact:" + hashlib.sha256(
                f"artifact-revision:{operation_id}".encode("utf-8")
            ).hexdigest()
        ),
        source_revision=source_revision,
        source_sha256=source_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        operation_id=operation_id,
        rerun_ocr=False,
    )
    command = command_value.as_dict()
    if legacy_missing_pins:
        command.pop("source_revision")
        command.pop("source_sha256")
    command_payload = server._correction_transform_canonical_json(command)
    correction_id = hashlib.sha256(command_payload).hexdigest()
    output_revision = output_revision or (
        "ctr:" + hashlib.sha256(
            f"revision:{operation_id}".encode("utf-8")
        ).hexdigest()
    )
    output_sha256 = hashlib.sha256(png).hexdigest()
    output_descriptor = {
        "kind": "corrected-display",
        "artifact_id": display_artifact_id,
        "artifact_revision": output_revision,
        "content_sha256": output_sha256,
    }
    publication = {
        "schema": "librarytool.correction-transform-publication",
        "version": 2,
        "operation_id": operation_id,
        "command_sha256": correction_id,
        "command": command,
        "outputs": [{
            "kind": "corrected-display",
            "artifact_id": display_artifact_id,
            "artifact_revision": output_revision,
            "content_sha256": output_sha256,
            "bytes": len(png),
            "media_type": "image/png",
            "dimensions": {
                "width": image.width,
                "height": image.height,
                "orientation": 1,
            },
            "provenance": {
                "origin": "transform",
                "generated_at": generated_at,
            },
            "storage": "immutable-object-v1",
        }],
    }
    publications = transforms / "publications"
    publications.mkdir(parents=True, exist_ok=True)
    publication_payload = server._correction_transform_canonical_json(
        publication)
    publication_sha256 = hashlib.sha256(publication_payload).hexdigest()
    (publications / f"{operation_digest}.json").write_bytes(
        publication_payload)
    pointer_dir = transforms / "by-item" / hashlib.sha256(
        item_id.encode("utf-8")).hexdigest()
    pointer_dir.mkdir(parents=True, exist_ok=True)
    pointer_path = pointer_dir / f"{operation_digest}.json"
    pointer_path.write_bytes(server._correction_transform_canonical_json({
        "schema": "librarytool.correction-transform-item-pointer",
        "version": 1,
        "item_id": item_id,
        "operation_id": operation_id,
        "command_sha256": correction_id,
        "publication_sha256": publication_sha256,
    }))
    if pointer_mtime is not None:
        os.utime(pointer_path, (pointer_mtime, pointer_mtime))
    if committed:
        receipt_dir = (
            engine_root / ".engine" / "receipts" / "correction-transforms"
        )
        receipt_dir.mkdir(parents=True, exist_ok=True)
        (receipt_dir / f"{operation_digest}.json").write_bytes(
            server._correction_transform_canonical_json({
                "schema": "librarytool.correction-transform-receipt",
                "version": 1,
                "operation_id": operation_id,
                "command_sha256": correction_id,
                "publication_sha256": publication_sha256,
                "result": {
                    "operation_id": operation_id,
                    "outputs": [output_descriptor],
                },
            })
        )
    if display_head:
        head_artifact_id = display_head_artifact_id or source_artifact_id
        head_dir = (
            transforms / "display-heads"
            / hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        )
        head_dir.mkdir(parents=True, exist_ok=True)
        head_path = head_dir / (
            hashlib.sha256(head_artifact_id.encode("utf-8")).hexdigest()
            + ".json"
        )
        head_path.write_bytes(server._correction_transform_canonical_json({
            "schema": "librarytool.correction-display-head",
            "version": 1,
            "item_id": item_id,
            "artifact_id": head_artifact_id,
            "operation_id": operation_id,
            "command_sha256": correction_id,
            "publication_sha256": publication_sha256,
        }))
    objects = transforms / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    (objects / (hashlib.sha256(
        display_artifact_id.encode("utf-8")).hexdigest() + ".bin")
     ).write_bytes(png)
    return correction_id


def test_capture_artifact_namespace_matches_repository_math():
    capture_id = "c1111111-1111-4111-8111-111111111111"
    asset_id = "asset-01"
    expected = hashlib.sha256(json.dumps(
        [capture_id, asset_id],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    namespace = server._capture_artifact_namespace(capture_id, asset_id)

    assert namespace == f"capture:{expected[:40]}"
    assert namespace == _opaque_identity("capture", capture_id, asset_id)


def test_mapping_resolves_chains_by_ancestry_and_skips_failed(tmp_path):
    """Production publications carry no ``generated_at`` — chain ancestry is
    the ordering signal, not fabricated stamps."""

    capture_id = "c2222222-2222-4222-8222-222222222222"
    item_id = "b-" + "1" * 32
    good_sha = "a" * 64
    failed_sha = "b" * 64
    photo_assets = _photo_assets(capture_id, [
        ("asset-good", good_sha, "completed"),
        ("asset-failed", failed_sha, "failed"),
    ])
    namespace = server._capture_artifact_namespace(capture_id, "asset-good")
    failed_namespace = server._capture_artifact_namespace(
        capture_id, "asset-failed")
    base_png = _png(8, 6)
    chained_png = _png(8, 6, color=(10, 20, 30))
    base_id = _publish_transform(
        tmp_path, item_id, "op-base", f"{namespace}:display",
        "ctr-" + "a" * 40, base_png)
    chained_id = _publish_transform(
        tmp_path, item_id, "op-chained", "ctr-" + "a" * 40,
        "ctr-" + "b" * 40, chained_png)
    _publish_transform(
        tmp_path, item_id, "op-failed", f"{failed_namespace}:original",
        "ctr-" + "c" * 40, _png(4, 4))

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert set(targets) == {"asset-good"}
    winner = targets["asset-good"]
    assert winner["correction_id"] == chained_id
    assert winner["source_original_sha256"] == good_sha
    assert winner["display_sha256"] == hashlib.sha256(chained_png).hexdigest()
    assert winner["display_object"].read_bytes() == chained_png
    assert winner["ancestors"] == frozenset({base_id})
    assert set(winner["candidates"]) == {base_id, chained_id}


def test_mapping_descendant_beats_ancestor_despite_newer_ancestor_mtime(
        tmp_path):
    """Disturbed pointer mtimes (index backfill, restore) must not resurrect
    the chain ancestor."""

    capture_id = "c5555555-5555-4555-8555-555555555555"
    item_id = "b-" + "3" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "d" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    base_png = _png(8, 6)
    chained_png = _png(8, 6, color=(60, 70, 80))
    _publish_transform(
        tmp_path, item_id, "op-base", f"{namespace}:display",
        "ctr-" + "a" * 40, base_png, pointer_mtime=2_000_000.0)
    chained_id = _publish_transform(
        tmp_path, item_id, "op-chained", "ctr-" + "a" * 40,
        "ctr-" + "b" * 40, chained_png, pointer_mtime=1_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == chained_id
    assert targets["asset-01"]["display_sha256"] == \
        hashlib.sha256(chained_png).hexdigest()


def test_mapping_resolves_same_slot_descendant_by_exact_source_pin(tmp_path):
    """A rerun keeps the capture display id, so its immutable source pin is
    the ancestry edge and must outrank disturbed pointer mtimes."""

    capture_id = "c7777777-7777-4777-8777-777777777777"
    item_id = "b-" + "5" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "f" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    base_png = _png(8, 6)
    base_revision = "ctr:" + "1" * 64
    base_sha = hashlib.sha256(base_png).hexdigest()
    base_id = _publish_transform(
        tmp_path, item_id, "op-base", display_slot,
        "ctr-" + "a" * 40, base_png,
        output_revision=base_revision, pointer_mtime=2_000_000.0)
    rerun_png = _png(8, 6, color=(30, 40, 50))
    rerun_id = _publish_transform(
        tmp_path, item_id, "op-rerun", display_slot,
        "ctr-" + "b" * 40, rerun_png,
        source_revision=base_revision, source_sha256=base_sha,
        pointer_mtime=1_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    winner = targets["asset-01"]
    assert winner["correction_id"] == rerun_id
    assert winner["ancestors"] == frozenset({base_id})
    assert winner["display_sha256"] == hashlib.sha256(rerun_png).hexdigest()


def test_mapping_rejects_ambiguous_same_slot_source_pin(tmp_path):
    capture_id = "c8888888-8888-4888-8888-888888888888"
    item_id = "b-" + "6" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "1" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    shared_png = _png(7, 5)
    shared_revision = "ctr:" + "2" * 64
    shared_sha = hashlib.sha256(shared_png).hexdigest()
    first_id = _publish_transform(
        tmp_path, item_id, "op-first", display_slot,
        "ctr-" + "c" * 40, shared_png,
        output_revision=shared_revision, pointer_mtime=2_000_000.0)
    second_id = _publish_transform(
        tmp_path, item_id, "op-second", display_slot,
        "ctr-" + "d" * 40, shared_png,
        output_revision=shared_revision, pointer_mtime=3_000_000.0)
    ambiguous_id = _publish_transform(
        tmp_path, item_id, "op-ambiguous", display_slot,
        "ctr-" + "e" * 40, _png(7, 5, color=(1, 2, 3)),
        source_revision=shared_revision, source_sha256=shared_sha,
        pointer_mtime=1_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    candidates = targets["asset-01"]["candidates"]
    assert set(candidates) == {first_id, second_id}
    assert ambiguous_id not in candidates


def test_mapping_rejects_same_slot_source_pin_cycles(tmp_path):
    capture_id = "c9999999-9999-4999-8999-999999999999"
    item_id = "b-" + "7" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "2" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    first_png = _png(9, 5)
    second_png = _png(9, 5, color=(4, 5, 6))
    first_revision = "ctr:" + "3" * 64
    second_revision = "ctr:" + "4" * 64
    _publish_transform(
        tmp_path, item_id, "op-cycle-a", display_slot,
        "ctr-" + "f" * 40, first_png,
        source_revision=second_revision,
        source_sha256=hashlib.sha256(second_png).hexdigest(),
        output_revision=first_revision)
    _publish_transform(
        tmp_path, item_id, "op-cycle-b", display_slot,
        "ctr-" + "0" * 40, second_png,
        source_revision=first_revision,
        source_sha256=hashlib.sha256(first_png).hexdigest(),
        output_revision=second_revision)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets == {}


def test_mapping_validated_display_head_beats_restored_newer_sibling(tmp_path):
    capture_id = "c1010101-1010-4010-8010-101010101010"
    item_id = "b-" + "8" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "3" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    parent_output = "ctr-" + "0" * 40
    _publish_transform(
        tmp_path, item_id, "op-head-parent", display_slot,
        parent_output, _png(10, 6, color=(5, 6, 7)),
        pointer_mtime=500_000.0, source_sha256="e" * 64,
        source_revision="capture-display-r1")
    head_png = _png(10, 6, color=(10, 11, 12))
    head_id = _publish_transform(
        tmp_path, item_id, "op-head", parent_output,
        "ctr-" + "1" * 40, head_png,
        pointer_mtime=1_000_000.0, display_head=True,
        display_head_artifact_id=display_slot)
    sibling_id = _publish_transform(
        tmp_path, item_id, "op-restored-sibling", display_slot,
        "ctr-" + "2" * 40, _png(10, 6, color=(20, 21, 22)),
        pointer_mtime=3_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    winner = targets["asset-01"]
    assert winner["correction_id"] == head_id
    assert winner["correction_id"] != sibling_id
    assert winner["display_sha256"] == hashlib.sha256(head_png).hexdigest()


def test_mapping_validated_original_root_head_uses_current_original_authority(
    tmp_path,
):
    capture_id = "c1212121-1212-4212-8212-121212121212"
    item_id = "b-" + "d" * 32
    original_sha = "6" * 64
    photo_assets = _photo_assets(
        capture_id,
        [("asset-01", original_sha, "completed")],
    )
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    original_slot = f"{namespace}:original"
    head_png = _png(10, 6, color=(12, 13, 14))
    head_id = _publish_transform(
        tmp_path,
        item_id,
        "op-original-head",
        original_slot,
        "ctr-" + "3" * 40,
        head_png,
        source_revision="capture-original-r1",
        source_sha256=original_sha,
        pointer_mtime=1_000_000.0,
        display_head=True,
        display_head_artifact_id=display_slot,
    )
    sibling_id = _publish_transform(
        tmp_path,
        item_id,
        "op-restored-display-sibling",
        display_slot,
        "ctr-" + "4" * 40,
        _png(10, 6, color=(22, 23, 24)),
        pointer_mtime=3_000_000.0,
    )

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path
    )

    winner = targets["asset-01"]
    assert winner["correction_id"] == head_id
    assert winner["correction_id"] != sibling_id
    assert winner["authoritative_display_head"] is True

    photo_assets["desktop_import"]["assets"][0]["source_checksum"] = (
        "7" * 64
    )
    assert server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path
    ) == {}


def test_mapping_rejects_display_head_from_replaced_capture_authority(tmp_path):
    capture_id = "c1111111-2222-4333-8444-555555555555"
    item_id = "b-" + "c" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "7" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    parent_output = "ctr-" + "9" * 40
    _publish_transform(
        tmp_path, item_id, "op-stale-parent", display_slot,
        parent_output, _png(10, 6), source_revision="capture-display-r1",
        source_sha256="e" * 64)
    _publish_transform(
        tmp_path, item_id, "op-stale-head", parent_output,
        "ctr-" + "a" * 40, _png(10, 6, color=(31, 32, 33)),
        display_head=True, display_head_artifact_id=display_slot)
    replacement_id = _publish_transform(
        tmp_path, item_id, "op-current-unheaded", display_slot,
        "ctr-" + "b" * 40, _png(10, 6, color=(41, 42, 43)),
        source_revision="capture-display-r2", source_sha256="d" * 64,
        pointer_mtime=3_000_000.0)
    photo_assets["desktop_import"]["assets"][0][
        "derivative_checksum"
    ] = "d" * 64

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets == {}
    assert replacement_id


def test_mapping_skips_in_flight_publication_without_receipt(tmp_path):
    capture_id = "c2020202-2020-4020-8020-202020202020"
    item_id = "b-" + "9" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "4" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    committed_id = _publish_transform(
        tmp_path, item_id, "op-committed", display_slot,
        "ctr-" + "3" * 40, _png(11, 6),
        pointer_mtime=1_000_000.0)
    in_flight_id = _publish_transform(
        tmp_path, item_id, "op-in-flight", display_slot,
        "ctr-" + "4" * 40, _png(11, 6, color=(30, 31, 32)),
        pointer_mtime=3_000_000.0, committed=False)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    winner = targets["asset-01"]
    assert winner["correction_id"] == committed_id
    assert in_flight_id not in winner["candidates"]


def test_mapping_skips_oversized_sparse_item_pointer(tmp_path):
    capture_id = "c1414141-1414-4414-8414-141414141414"
    item_id = "b-" + "4" * 32
    photo_assets = _photo_assets(
        capture_id,
        [("asset-01", "5" * 64, "completed")],
    )
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    operation_id = "op-oversized-pointer"
    _publish_transform(
        tmp_path,
        item_id,
        operation_id,
        f"{namespace}:display",
        "ctr-" + "7" * 40,
        _png(12, 6),
    )
    pointer = (
        tmp_path / ".engine" / "correction-transforms" / "by-item"
        / hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        / (
            hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
            + ".json"
        )
    )
    with pointer.open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b"{}")

    targets = server._capture_correction_targets(
        capture_id,
        photo_assets,
        item_id,
        tmp_path,
    )

    assert targets == {}


def test_mapping_legacy_id_edge_rejects_present_mismatched_pins(tmp_path):
    capture_id = "c3030303-3030-4030-8030-303030303030"
    item_id = "b-" + "a" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "5" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    parent_output = "ctr-" + "5" * 40
    parent_id = _publish_transform(
        tmp_path, item_id, "op-parent", display_slot,
        parent_output, _png(12, 6), pointer_mtime=1_000_000.0)
    invalid_child_id = _publish_transform(
        tmp_path, item_id, "op-invalid-child", parent_output,
        "ctr-" + "6" * 40, _png(12, 6, color=(40, 41, 42)),
        source_revision="ctr:" + "f" * 64,
        source_sha256="e" * 64,
        pointer_mtime=3_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    winner = targets["asset-01"]
    assert winner["correction_id"] == parent_id
    assert invalid_child_id not in winner["candidates"]


def test_mapping_legacy_id_edge_allows_historical_missing_pins(tmp_path):
    capture_id = "c4040404-4040-4040-8040-404040404040"
    item_id = "b-" + "b" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "6" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    display_slot = f"{namespace}:display"
    parent_output = "ctr-" + "7" * 40
    parent_id = _publish_transform(
        tmp_path, item_id, "op-legacy-parent", display_slot,
        parent_output, _png(13, 6))
    child_id = _publish_transform(
        tmp_path, item_id, "op-legacy-child", parent_output,
        "ctr-" + "8" * 40, _png(13, 6, color=(50, 51, 52)),
        legacy_missing_pins=True)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    winner = targets["asset-01"]
    assert winner["correction_id"] == child_id
    assert winner["ancestors"] == frozenset({parent_id})


def test_mapping_honors_recorded_stamps_if_ever_present(tmp_path):
    """The engine writes no stamps today, but a recorded ``generated_at``
    must keep outranking pointer mtimes if it ever appears."""

    capture_id = "c6666666-6666-4666-8666-666666666666"
    item_id = "b-" + "4" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "e" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    older_png = _png(6, 6)
    newer_png = _png(6, 6, color=(120, 10, 10))
    _publish_transform(
        tmp_path, item_id, "op-older", f"{namespace}:display",
        "ctr-" + "a" * 40, older_png,
        generated_at="2026-07-01T00:00:00+00:00", pointer_mtime=2_000_000.0)
    newer_id = _publish_transform(
        tmp_path, item_id, "op-newer", f"{namespace}:display",
        "ctr-" + "b" * 40, newer_png,
        generated_at="2026-07-02T00:00:00+00:00", pointer_mtime=1_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == newer_id


def test_mapping_latest_wins_by_pointer_mtime_without_stamps(tmp_path):
    capture_id = "c4444444-4444-4444-8444-444444444444"
    item_id = "b-" + "2" * 32
    photo_assets = _photo_assets(
        capture_id, [("asset-01", "c" * 64, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    older_png = _png(6, 6)
    newer_png = _png(6, 6, color=(90, 90, 90))
    _publish_transform(
        tmp_path, item_id, "op-older", f"{namespace}:display",
        "ctr-" + "d" * 40, older_png, pointer_mtime=1_000_000.0)
    newer_id = _publish_transform(
        tmp_path, item_id, "op-newer", f"{namespace}:display",
        "ctr-" + "e" * 40, newer_png, pointer_mtime=2_000_000.0)

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == newer_id
    assert targets["asset-01"]["display_sha256"] == \
        hashlib.sha256(newer_png).hexdigest()


def test_mapping_backed_up_asset_publishes_only_active_operation(tmp_path):
    """The display manifest, not retained transform history, is authoritative
    once the immutable original has entered the backup store."""

    capture_id = "c7777777-7777-4777-8777-777777777777"
    item_id = "b-" + "5" * 32
    source_sha = "f" * 64
    photo_assets = _photo_assets(
        capture_id, [("asset-01", source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    active_png = _png(6, 6, color=(20, 40, 60))
    later_png = _png(6, 6, color=(80, 100, 120))
    active_id = _publish_transform(
        tmp_path, item_id, "op-active", f"{namespace}:display",
        "ctr-" + "a" * 40, active_png, pointer_mtime=1_000_000.0,
        source_revision="capture-display-r1", source_sha256="e" * 64,
        display_head=True,
        display_head_artifact_id=f"{namespace}:display")
    later_id = _publish_transform(
        tmp_path, item_id, "op-later", "ctr-" + "a" * 40,
        "ctr-" + "b" * 40, later_png, pointer_mtime=2_000_000.0)
    _mark_original_backed_up(
        photo_assets,
        "asset-01",
        source_sha,
        active_operation_id="op-active",
    )

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets["asset-01"]["correction_id"] == active_id
    assert targets["asset-01"]["display_sha256"] == \
        hashlib.sha256(active_png).hexdigest()
    assert targets["asset-01"]["manifest_authoritative"] is True
    assert set(targets["asset-01"]["candidates"]) == {active_id, later_id}


def test_publish_backed_up_original_root_head_without_physical_promotion(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    asset_id = seeded["asset_id"]
    source_sha = seeded["source_sha"]
    namespace = seeded["namespace"]
    item_id = seeded["item_id"]
    engine_root = server._ensure_engine_session().write_set.root
    corrected_png = _png(63, 47, color=(33, 66, 99))
    operation_id = "op-original-head"
    correction_id = _publish_transform(
        engine_root,
        item_id,
        operation_id,
        f"{namespace}:original",
        "ctr-" + "3" * 40,
        corrected_png,
        source_revision="capture-original-r1",
        source_sha256=source_sha,
        display_head=True,
        display_head_artifact_id=f"{namespace}:display",
    )
    manifest_path = server.CAPTURES_DIR / capture_id / "photo_assets.json"
    photo_assets = json.loads(manifest_path.read_text("utf-8"))
    physical_display_sha = "e" * 64
    photo_assets["assets"] = [{
        "asset_id": asset_id,
        "original": {"sha256": source_sha},
        "display": {"sha256": physical_display_sha},
    }]
    _mark_original_backed_up(
        photo_assets,
        asset_id,
        source_sha,
        active_operation_id=operation_id,
    )
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    assert server._capture_correction_promoted_display_sha256(
        corrected_png
    ) != physical_display_sha

    targets = server._capture_correction_targets(
        capture_id,
        photo_assets,
        item_id,
        engine_root,
    )

    target = targets[asset_id]
    assert target["correction_id"] == correction_id
    assert target["authoritative_display_head"] is True
    assert target["promoted_display_sha256"] == ""
    assert server._capture_correction_source_bytes(
        target,
        engine_root=engine_root,
    ) == corrected_png

    calls = _stub_cloud(
        monkeypatch,
        capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[],
    )
    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"}
    )

    assert outcome["pushed"] == 1
    assert outcome["errors"] == []
    assert len(calls["uploads"]) == 2
    assert calls["published"][0][0]["correction_id"] == correction_id


def test_mapping_backed_up_original_source_allows_pinned_physical_promotion(
        tmp_path):
    capture_id = "c7878787-7878-4878-8878-787878787878"
    item_id = "b-" + "d" * 32
    asset_id = "asset-01"
    source_sha = "a" * 64
    photo_assets = _photo_assets(
        capture_id, [(asset_id, source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    promoted_png = _png(7, 5, color=(33, 66, 99))
    promoted_sha = server._capture_correction_promoted_display_sha256(
        promoted_png)
    correction_id = _publish_transform(
        tmp_path,
        item_id,
        "op-promoted-original",
        f"{namespace}:original",
        "ctr-" + "3" * 40,
        promoted_png,
        source_revision="capture-original-r1",
        source_sha256=source_sha,
    )
    imported = photo_assets["desktop_import"]["assets"][0]
    imported["derivative_checksum"] = promoted_sha
    photo_assets["assets"] = [{
        "asset_id": asset_id,
        "display": {"sha256": promoted_sha},
    }]
    _mark_original_backed_up(
        photo_assets,
        asset_id,
        source_sha,
        active_operation_id="op-promoted-original",
    )

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets[asset_id]["correction_id"] == correction_id
    assert targets[asset_id]["manifest_authoritative"] is True
    assert targets[asset_id]["authoritative_display_head"] is False
    assert server._capture_correction_source_bytes(
        targets[asset_id], engine_root=tmp_path) == promoted_png


@pytest.mark.parametrize("mismatch", ["desktop", "public"])
def test_mapping_backed_up_original_source_requires_both_current_display_pins(
        tmp_path, mismatch):
    capture_id = "c7979797-7979-4979-8979-797979797979"
    item_id = "b-" + "e" * 32
    asset_id = "asset-01"
    source_sha = "b" * 64
    photo_assets = _photo_assets(
        capture_id, [(asset_id, source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    promoted_png = _png(7, 5, color=(44, 77, 100))
    promoted_sha = server._capture_correction_promoted_display_sha256(
        promoted_png)
    _publish_transform(
        tmp_path,
        item_id,
        "op-promoted-original",
        f"{namespace}:original",
        "ctr-" + "4" * 40,
        promoted_png,
        source_revision="capture-original-r1",
        source_sha256=source_sha,
    )
    imported = photo_assets["desktop_import"]["assets"][0]
    imported["derivative_checksum"] = (
        "c" * 64 if mismatch == "desktop" else promoted_sha
    )
    photo_assets["assets"] = [{
        "asset_id": asset_id,
        "display": {
            "sha256": "d" * 64 if mismatch == "public" else promoted_sha,
        },
    }]
    _mark_original_backed_up(
        photo_assets,
        asset_id,
        source_sha,
        active_operation_id="op-promoted-original",
    )

    assert server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path) == {}


def test_mapping_backed_up_display_source_requires_matching_display_head(
        tmp_path):
    capture_id = "c7070707-7070-4070-8070-707070707070"
    item_id = "b-" + "f" * 32
    asset_id = "asset-01"
    source_sha = "1" * 64
    photo_assets = _photo_assets(
        capture_id, [(asset_id, source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    _publish_transform(
        tmp_path,
        item_id,
        "op-unheaded-display",
        f"{namespace}:display",
        "ctr-" + "5" * 40,
        _png(7, 5),
    )
    _mark_original_backed_up(
        photo_assets,
        asset_id,
        source_sha,
        active_operation_id="op-unheaded-display",
    )

    assert server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path) == {}


def test_ocr_source_resolution_requires_active_marker_and_display_head(
        monkeypatch, tmp_path):
    capture_id = "c7171717-7171-4171-8171-717171717171"
    item_id = "b-" + "7" * 32
    asset_id = "asset-01"
    source_sha = "2" * 64
    photo_assets = _photo_assets(
        capture_id, [(asset_id, source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    display_id = "ctr-" + "6" * 40
    _publish_transform(
        tmp_path,
        item_id,
        "op-active-display",
        f"{namespace}:display",
        display_id,
        _png(7, 5),
        source_revision="capture-display-r1",
        source_sha256="e" * 64,
        display_head=True,
        display_head_artifact_id=f"{namespace}:display",
    )
    _mark_original_backed_up(
        photo_assets,
        asset_id,
        source_sha,
        active_operation_id="op-active-display",
    )
    captures = tmp_path / "captures"
    capture_dir = captures / capture_id
    capture_dir.mkdir(parents=True)
    manifest_path = capture_dir / "photo_assets.json"
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    monkeypatch.setattr(server, "CAPTURES_DIR", captures)

    located = server._ocr_apply_capture_asset(
        item_id, capture_id, display_id, tmp_path)

    assert located is not None
    assert located[:3] == (asset_id, 1, "display")
    assert len(located[3]) == 1

    photo_assets["desktop_import"]["assets"][0].pop(
        "active_desktop_correction_id")
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    assert server._ocr_apply_capture_asset(
        item_id, capture_id, display_id, tmp_path) is None


def test_ocr_source_resolution_accepts_pinned_original_physical_promotion(
        monkeypatch, tmp_path):
    capture_id = "c7272727-7272-4272-8272-727272727272"
    item_id = "b-" + "8" * 32
    asset_id = "asset-01"
    source_sha = "3" * 64
    photo_assets = _photo_assets(
        capture_id, [(asset_id, source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    display_id = "ctr-" + "7" * 40
    promoted_png = _png(7, 5, color=(55, 88, 111))
    promoted_sha = server._capture_correction_promoted_display_sha256(
        promoted_png)
    _publish_transform(
        tmp_path,
        item_id,
        "op-active-original",
        f"{namespace}:original",
        display_id,
        promoted_png,
        source_revision="capture-original-r1",
        source_sha256=source_sha,
    )
    photo_assets["desktop_import"]["assets"][0][
        "derivative_checksum"
    ] = promoted_sha
    photo_assets["assets"] = [{
        "asset_id": asset_id,
        "display": {"sha256": promoted_sha},
    }]
    _mark_original_backed_up(
        photo_assets,
        asset_id,
        source_sha,
        active_operation_id="op-active-original",
    )
    captures = tmp_path / "captures"
    capture_dir = captures / capture_id
    capture_dir.mkdir(parents=True)
    (capture_dir / "photo_assets.json").write_text(
        json.dumps(photo_assets), encoding="utf-8")
    monkeypatch.setattr(server, "CAPTURES_DIR", captures)

    assert server._ocr_apply_capture_asset(
        item_id, capture_id, display_id, tmp_path) == (
            asset_id,
            1,
            "display",
            [],
        )


@pytest.mark.parametrize(
    "active_operation_id",
    [_NO_ACTIVE_CORRECTION, ""],
    ids=["missing", "empty"],
)
def test_mapping_backed_up_restored_asset_suppresses_history(
        tmp_path, active_operation_id):
    capture_id = "c8888888-8888-4888-8888-888888888888"
    item_id = "b-" + "6" * 32
    source_sha = "1" * 64
    photo_assets = _photo_assets(
        capture_id, [("asset-01", source_sha, "completed")])
    namespace = server._capture_artifact_namespace(capture_id, "asset-01")
    _publish_transform(
        tmp_path, item_id, "op-retained", f"{namespace}:display",
        "ctr-" + "c" * 40, _png(6, 6))
    _mark_original_backed_up(
        photo_assets,
        "asset-01",
        source_sha,
        active_operation_id=active_operation_id,
    )

    targets = server._capture_correction_targets(
        capture_id, photo_assets, item_id, tmp_path)

    assert targets == {}


def test_correction_jpeg_transcode_caps_edges_without_upscaling():
    png = _png(3200, 1600)

    display = server._capture_correction_jpeg(png, long_edge=1600, quality=90)
    thumbnail = server._capture_correction_jpeg(png, long_edge=512, quality=80)
    small = server._capture_correction_jpeg(
        _png(100, 50), long_edge=1600, quality=90)

    image = Image.open(io.BytesIO(display["data"]))
    assert image.format == "JPEG"
    assert image.mode == "RGB"
    assert image.size == (1600, 800)
    assert "exif" not in image.info
    assert (display["width"], display["height"]) == (1600, 800)
    assert display["sha256"] == hashlib.sha256(display["data"]).hexdigest()
    assert display["bytes"] == len(display["data"])
    assert (thumbnail["width"], thumbnail["height"]) == (512, 256)
    assert (small["width"], small["height"]) == (100, 50)


@pytest.mark.parametrize("pixel_limit", [1, 3])
def test_transcode_rejects_pillow_decompression_bombs(
        monkeypatch, pixel_limit):
    payload = _png(2, 2)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", pixel_limit)

    with pytest.raises(ValueError, match="decoded image safety limit"):
        server._capture_correction_jpeg(
            payload,
            long_edge=1600,
            quality=90,
        )


def _seed_corrected_capture(monkeypatch, *, lifecycle: str = "completed",
                            pointer_mtime: float | None = None,
                            transport: str = "cloud",
                            display_head: bool = False):
    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda _entry: {})
    monkeypatch.setattr(server, "activity", lambda *_args, **_kwargs: None)
    capture_id = "c3333333-3333-4333-8333-333333333333"
    entry_id, errors = server.ingest_capture(
        {
            "id": capture_id,
            "ocr": {"photo_1.jpg": "Garden sage."},
            "meta": {"title": "Corrections Herbal"},
        },
        [_jpeg(capture_id)],
        "",
        ["photo_1.jpg"],
        transport=transport,
    )
    assert entry_id
    assert errors == []
    item_id = server._capture_archive_association(capture_id).book_id
    asset_id = "asset-01"
    source_sha = hashlib.sha256(_jpeg(capture_id)).hexdigest()
    (server.CAPTURES_DIR / capture_id / "photo_assets.json").write_text(
        json.dumps(_photo_assets(
            capture_id, [(asset_id, source_sha, lifecycle)])),
        encoding="utf-8",
    )
    png = _png(64, 48)
    namespace = server._capture_artifact_namespace(capture_id, asset_id)
    correction_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        item_id, "op-seeded", f"{namespace}:display", "ctr-" + "f" * 40,
        png, pointer_mtime=pointer_mtime,
        source_revision="capture-display-r1" if display_head else "",
        source_sha256="e" * 64 if display_head else "",
        display_head=display_head,
        display_head_artifact_id=(
            f"{namespace}:display" if display_head else ""
        ))
    return {
        "capture_id": capture_id,
        "asset_id": asset_id,
        "item_id": item_id,
        "namespace": namespace,
        "source_sha": source_sha,
        "png": png,
        "correction_id": correction_id,
    }


def _strip_transport_field(capture_id: str) -> None:
    """Mimic an import that predates ``capture_transport`` (field absent)."""

    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {}) or {}
    matched = False
    for entry in entries.values():
        if isinstance(entry, dict) and entry.get("capture_id") == capture_id:
            entry.pop("capture_transport", None)
            matched = True
    assert matched
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)


def _stub_cloud(monkeypatch, capture_id: str, *, owner_rows, existing):
    calls = {
        "rest": [],
        "uploads": [],
        "published": [],
        "publish_expected": [],
        "listed": [],
        "photo_deletes": [],
    }

    def rest(cfg, method, path, payload=None, prefer=""):
        calls["rest"].append((method, path))
        assert method == "GET"
        assert path.startswith("captures?id=in.(")
        return owner_rows

    monkeypatch.setattr(server.sbase, "_rest", rest)
    monkeypatch.setattr(
        server.sbase,
        "list_capture_corrections",
        lambda cfg, ids: calls["listed"].append(sorted(ids)) or existing,
        raising=False,
    )
    monkeypatch.setattr(
        server.sbase,
        "upload_object",
        lambda cfg, bucket, path, data, content_type="application/octet-stream",
        upsert=True: calls["uploads"].append(
            (bucket, path, hashlib.sha256(data).hexdigest(), content_type)
        ) or path,
    )
    def publish(cfg, rows, *, expected_existing=None):
        del cfg
        calls["published"].append(rows)
        calls["publish_expected"].append(expected_existing)
        return len(rows)

    monkeypatch.setattr(
        server.sbase,
        "publish_capture_corrections",
        publish,
        raising=False,
    )
    monkeypatch.setattr(
        server.sbase,
        "delete_photos",
        lambda cfg, paths: calls["photo_deletes"].append(tuple(paths)),
    )
    return calls


def test_publish_uploads_objects_and_rows_per_contract(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    calls = _stub_cloud(
        monkeypatch, capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 1, "up_to_date": 0,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    thumbnail = server._capture_correction_jpeg(
        seeded["png"], long_edge=512, quality=80)
    base = (f"{OWNER_ID}/{capture_id}/{seeded['asset_id']}/"
            f"desktop-{seeded['correction_id'][:20]}")
    display_path = f"{base}/display-{display['sha256'][:20]}.jpg"
    thumbnail_path = f"{base}/thumbnail-{thumbnail['sha256'][:20]}.jpg"
    assert calls["uploads"] == [
        ("capture-derivatives", display_path, display["sha256"],
         "image/jpeg"),
        ("capture-derivatives", thumbnail_path, thumbnail["sha256"],
         "image/jpeg"),
    ]
    assert calls["listed"] == [[capture_id]]
    (rows,) = calls["published"]
    (row,) = rows
    assert calls["publish_expected"] == [{
        (capture_id, seeded["asset_id"]): None,
    }]
    assert row["capture_id"] == capture_id
    assert row["asset_id"] == seeded["asset_id"]
    assert row["correction_id"] == seeded["correction_id"]
    assert row["source_original_sha256"] == seeded["source_sha"]
    doc = row["result"]
    assert doc["schema"] == "org.whl.capture-correction-result"
    assert doc["version"] == 1
    assert doc["processor"] == "whl-desktop-corrections"
    assert doc["recipe"] == "whl-desktop-correction-v1"
    assert doc["correction_id"] == seeded["correction_id"]
    assert doc["geometry_strategy"] == "replace_and_reocr"
    assert doc["source"] == {
        "original_sha256": seeded["source_sha"],
        "desktop_display_sha256":
            hashlib.sha256(seeded["png"]).hexdigest(),
    }
    assert doc["artifacts"]["display"] == {
        "bucket": "capture-derivatives",
        "path": display_path,
        "sha256": display["sha256"],
        "bytes": display["bytes"],
        "width": 64,
        "height": 48,
        "content_type": "image/jpeg",
    }
    assert doc["artifacts"]["thumbnail"]["path"] == thumbnail_path
    assert doc["artifacts"]["thumbnail"]["content_type"] == "image/jpeg"
    assert doc["generated_at"]


def test_publish_includes_every_corrected_asset_in_one_capture_refresh(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    second_asset_id = "asset-02"
    second_source_sha = hashlib.sha256(b"second original").hexdigest()
    (server.CAPTURES_DIR / seeded["capture_id"] / "photo_assets.json").write_text(
        json.dumps(_photo_assets(seeded["capture_id"], [
            (seeded["asset_id"], seeded["source_sha"], "completed"),
            (second_asset_id, second_source_sha, "completed"),
        ])),
        encoding="utf-8",
    )
    second_namespace = server._capture_artifact_namespace(
        seeded["capture_id"], second_asset_id)
    second_correction_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"],
        "op-second-asset",
        f"{second_namespace}:display",
        "ctr-" + "a" * 40,
        _png(48, 64, color=(40, 120, 200)),
    )
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["candidates"] == 1
    assert outcome["pushed"] == 2
    assert len(calls["uploads"]) == 4
    (rows,) = calls["published"]
    assert {
        (row["asset_id"], row["correction_id"],
         row["source_original_sha256"])
        for row in rows
    } == {
        (seeded["asset_id"], seeded["correction_id"], seeded["source_sha"]),
        (second_asset_id, second_correction_id, second_source_sha),
    }


def test_publish_holds_local_authority_through_cloud_row_cas(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch, display_head=True)
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[],
    )
    capture_depth = 0
    authority_depth = 0
    capture_guard = server._capture_archive_backfill_capture_guard
    authority_context = server._corrections_index_authority_context

    @contextlib.contextmanager
    def observed_capture_guard(capture_ids):
        nonlocal capture_depth
        with capture_guard(capture_ids):
            capture_depth += 1
            try:
                yield
            finally:
                capture_depth -= 1

    @contextlib.contextmanager
    def observed_authority_context():
        nonlocal authority_depth
        with authority_context():
            authority_depth += 1
            try:
                yield
            finally:
                authority_depth -= 1

    def publish(_cfg, rows, *, expected_existing=None):
        assert expected_existing == {
            (seeded["capture_id"], seeded["asset_id"]): None,
        }
        assert capture_depth == 1
        assert authority_depth == 1
        calls["published"].append(rows)
        calls["publish_expected"].append(expected_existing)
        return len(rows)

    monkeypatch.setattr(
        server,
        "_capture_archive_backfill_capture_guard",
        observed_capture_guard,
    )
    monkeypatch.setattr(
        server,
        "_corrections_index_authority_context",
        observed_authority_context,
    )
    monkeypatch.setattr(
        server.sbase,
        "publish_capture_corrections",
        publish,
    )

    result = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert result["pushed"] == 1
    assert result["errors"] == []
    assert len(calls["published"]) == 1
    assert capture_depth == 0
    assert authority_depth == 0


def test_publish_revalidates_local_authority_after_uploads(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch, display_head=True)
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[],
    )
    upload = server.sbase.upload_object
    changed = False

    def upload_then_replace_authority(*args, **kwargs):
        nonlocal changed
        outcome = upload(*args, **kwargs)
        if not changed:
            changed = True
            path = (
                server.CAPTURES_DIR / seeded["capture_id"]
                / "photo_assets.json"
            )
            photo_assets = json.loads(path.read_text("utf-8"))
            photo_assets["desktop_import"]["assets"][0][
                "derivative_checksum"
            ] = "d" * 64
            path.write_text(json.dumps(photo_assets), encoding="utf-8")
        return outcome

    monkeypatch.setattr(
        server.sbase, "upload_object", upload_then_replace_authority)

    result = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert changed is True
    assert len(calls["uploads"]) == 2
    assert calls["published"] == []
    assert result["pushed"] == 0
    assert result["errors"] == []
    assert len(result["notices"]) == 1
    assert "authority changed before publication" in result["notices"][0]


def test_publish_revalidation_detects_removed_display_head_policy(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(
        monkeypatch,
        pointer_mtime=1_000_000.0,
        display_head=True,
    )
    sibling_png = _png(64, 48, color=(5, 100, 200))
    sibling_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"],
        "op-a-restored-sibling",
        f"{seeded['namespace']}:display",
        "ctr-" + "2" * 40,
        sibling_png,
        pointer_mtime=1_000_000.0,
    )
    sibling_display = server._capture_correction_jpeg(
        sibling_png,
        long_edge=1600,
        quality=90,
    )
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[_cloud_row(
            seeded,
            correction_id=sibling_id,
            display_sha256=sibling_display["sha256"],
        )],
    )
    engine_root = server._ensure_engine_session().write_set.root
    display_slot = f"{seeded['namespace']}:display"
    head_path = (
        engine_root / ".engine" / "correction-transforms" / "display-heads"
        / hashlib.sha256(seeded["item_id"].encode("utf-8")).hexdigest()
        / (hashlib.sha256(display_slot.encode("utf-8")).hexdigest() + ".json")
    )
    assert head_path.is_file()
    upload = server.sbase.upload_object
    removed = False

    def upload_then_remove_head(*args, **kwargs):
        nonlocal removed
        outcome = upload(*args, **kwargs)
        if not removed:
            head_path.unlink()
            removed = True
        return outcome

    monkeypatch.setattr(
        server.sbase,
        "upload_object",
        upload_then_remove_head,
    )

    result = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert removed is True
    assert len(calls["uploads"]) == 2
    assert calls["published"] == []
    assert result["pushed"] == 0
    assert result["errors"] == []
    assert len(result["notices"]) == 1
    assert "authority changed before publication" in result["notices"][0]
def test_publish_rejects_oversized_transform_object_before_reading_or_upload(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[],
    )
    monkeypatch.setattr(
        server,
        "_CAPTURE_CORRECTION_SOURCE_MAX_BYTES",
        len(seeded["png"]) - 1,
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["candidates"] == 1
    assert outcome["pushed"] == 0
    assert len(outcome["errors"]) == 1
    assert "invalid or too large" in outcome["errors"][0]
    assert calls["uploads"] == []
    assert calls["published"] == []


@pytest.mark.parametrize(
    ("document", "limit_name"),
    [
        ("pointer", "_CAPTURE_CORRECTION_POINTER_MAX_BYTES"),
        ("publication", "_CAPTURE_CORRECTION_PUBLICATION_MAX_BYTES"),
    ],
)
def test_publish_bounds_transform_documents_before_json_decode(
        monkeypatch, capture_workspace, document, limit_name):
    seeded = _seed_corrected_capture(monkeypatch)
    engine_root = server._ensure_engine_session().write_set.root
    transforms = engine_root / ".engine" / "correction-transforms"
    if document == "pointer":
        document_path = next((transforms / "by-item").rglob("*.json"))
    else:
        document_path = next((transforms / "publications").glob("*.json"))
    monkeypatch.setattr(
        server,
        limit_name,
        len(document_path.read_bytes()) - 1,
    )
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["candidates"] == 0
    assert outcome["pushed"] == 0
    assert calls["rest"] == []
    assert calls["uploads"] == []
    assert calls["published"] == []


@pytest.mark.parametrize("target_kind", ["pointer", "publication", "object"])
def test_publish_rejects_transform_parent_replacement_with_preserved_leaf(
        monkeypatch, capture_workspace, target_kind):
    seeded = _seed_corrected_capture(monkeypatch)
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[],
    )
    original_open = server._open_verified_regular
    replaced = []

    def kind(path):
        if path.parent.parent.name == "by-item":
            return "pointer"
        if path.parent.name == "publications":
            return "publication"
        if path.parent.name == "objects":
            return "object"
        return ""

    def replace_parent_before_open(path, named_before, *, authority):
        if not replaced and kind(path) == target_kind:
            parent = path.parent
            displaced = parent.with_name(parent.name + ".displaced")
            parent.rename(displaced)
            parent.mkdir()
            (displaced / path.name).replace(path)
            replaced.append(path)
        return original_open(path, named_before, authority=authority)

    monkeypatch.setattr(
        server,
        "_open_verified_regular",
        replace_parent_before_open,
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert len(replaced) == 1
    assert outcome["pushed"] == 0
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_confined_transform_read_rejects_escape_and_redirected_ancestor(
        monkeypatch, tmp_path):
    engine_root = tmp_path / "output"
    transforms = engine_root / ".engine" / "correction-transforms"
    objects = transforms / "objects"
    objects.mkdir(parents=True)
    payload = b"confined"
    object_path = objects / "object.bin"
    object_path.write_bytes(payload)
    outside = engine_root / "outside.bin"
    outside.write_bytes(payload)

    with pytest.raises(ValueError, match="could not be read safely"):
        server._capture_correction_confined_bytes(
            outside,
            authority_root=engine_root,
            confined_root=transforms,
            maximum=len(payload),
            artifact="test transform object",
        )

    original_redirect = server._capture_path_is_redirecting
    monkeypatch.setattr(
        server,
        "_capture_path_is_redirecting",
        lambda path: Path(path) == objects or original_redirect(path),
    )
    with pytest.raises(ValueError, match="could not be read safely"):
        server._capture_correction_confined_bytes(
            object_path,
            authority_root=engine_root,
            confined_root=transforms,
            maximum=len(payload),
            artifact="test transform object",
        )


def test_publish_restored_backed_up_original_is_cloud_noop(
        monkeypatch, capture_workspace):
    """Restore is deliberately desktop-local in v1: retained transform
    history cannot recreate a correction row or disturb source photos."""

    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    manifest_path = server.CAPTURES_DIR / capture_id / "photo_assets.json"
    photo_assets = json.loads(manifest_path.read_text(encoding="utf-8"))
    _mark_original_backed_up(
        photo_assets,
        seeded["asset_id"],
        seeded["source_sha"],
    )
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    calls = _stub_cloud(
        monkeypatch,
        capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {
        "candidates": 0,
        "pushed": 0,
        "up_to_date": 0,
        "no_cloud_row": 0,
        "unreadable_capture": 0,
        "notices": [],
        "errors": [],
    }
    assert calls["rest"] == []
    assert calls["listed"] == []
    assert calls["uploads"] == []
    assert calls["published"] == []
    assert calls["photo_deletes"] == []


def test_publish_revalidates_capture_state_before_cloud_row_publication(
        monkeypatch, capture_workspace):
    """A restore after discovery suppresses the row, not safe object uploads."""

    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    calls = _stub_cloud(
        monkeypatch,
        capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[],
    )
    snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    restore_acquired = threading.Event()
    upload_started = threading.Event()
    original_targets = server._capture_correction_targets

    def paused_targets(*args, **kwargs):
        snapshot_started.set()
        assert release_snapshot.wait(5)
        return original_targets(*args, **kwargs)

    monkeypatch.setattr(server, "_capture_correction_targets", paused_targets)
    original_list = server.sbase.list_capture_corrections

    def listed_after_restore(cfg, ids):
        assert restore_acquired.wait(5)
        return original_list(cfg, ids)

    monkeypatch.setattr(
        server.sbase,
        "list_capture_corrections",
        listed_after_restore,
    )
    original_upload = server.sbase.upload_object

    def observed_upload(*args, **kwargs):
        upload_started.set()
        return original_upload(*args, **kwargs)

    monkeypatch.setattr(server.sbase, "upload_object", observed_upload)
    publish_result = {}
    publish_errors = []

    def publish():
        try:
            publish_result.update(server._publish_capture_corrections(
                {"url": "cloud", "key": "service"}))
        except BaseException as exc:  # surface worker failures in this thread
            publish_errors.append(exc)

    manifest_path = server.CAPTURES_DIR / capture_id / "photo_assets.json"

    def restore_manifest():
        session = server._ensure_engine_session()
        with session.write_set.workspace_lease():
            restore_acquired.set()
            photo_assets = json.loads(manifest_path.read_text("utf-8"))
            _mark_original_backed_up(
                photo_assets, seeded["asset_id"], seeded["source_sha"])
            manifest_path.write_text(
                json.dumps(photo_assets), encoding="utf-8")
            # Storage uploads must not wait for the output/corrections
            # authority held by this restore. Only the final row CAS does.
            assert upload_started.wait(5)

    publisher = threading.Thread(target=publish)
    restorer = threading.Thread(target=restore_manifest)
    publisher.start()
    assert snapshot_started.wait(5)
    restorer.start()
    assert not restore_acquired.wait(0.1)
    release_snapshot.set()
    publisher.join(5)
    restorer.join(5)

    assert not publisher.is_alive()
    assert not restorer.is_alive()
    assert publish_errors == []
    assert restore_acquired.is_set()
    assert upload_started.is_set()
    assert publish_result["pushed"] == 0
    assert publish_result["errors"] == []
    assert any(
        "authority changed before publication" in notice
        for notice in publish_result["notices"]
    )
    assert len(calls["uploads"]) == 2
    assert calls["published"] == []
    assert calls["photo_deletes"] == []
    first_call_counts = {
        key: len(calls[key])
        for key in ("rest", "listed", "uploads", "published",
                    "photo_deletes")
    }

    restored = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert restored["candidates"] == 0
    assert {
        key: len(calls[key])
        for key in first_call_counts
    } == first_call_counts


def _complete_cloud_row(seeded) -> dict:
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    thumbnail = server._capture_correction_jpeg(
        seeded["png"], long_edge=512, quality=80)
    base = (
        f"{OWNER_ID}/{seeded['capture_id']}/{seeded['asset_id']}/"
        f"desktop-{seeded['correction_id'][:20]}"
    )
    target = {
        "correction_id": seeded["correction_id"],
        "source_original_sha256": seeded["source_sha"],
        "display_sha256": hashlib.sha256(seeded["png"]).hexdigest(),
    }
    return {
        "capture_id": seeded["capture_id"],
        "asset_id": seeded["asset_id"],
        "correction_id": seeded["correction_id"],
        "source_original_sha256": seeded["source_sha"],
        "revision": 1,
        "result": server._capture_correction_result_doc(
            target,
            display=display,
            thumbnail=thumbnail,
            display_path=f"{base}/display-{display['sha256'][:20]}.jpg",
            thumbnail_path=(
                f"{base}/thumbnail-{thumbnail['sha256'][:20]}.jpg"
            ),
        ),
    }


def test_publish_diff_short_circuits_matching_rows(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    existing_row = _complete_cloud_row(seeded)
    existing_row["result"]["generated_at"] = "2026-08-01T00:00:00Z"
    calls = _stub_cloud(
        monkeypatch, capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[existing_row],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 0, "up_to_date": 1,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_repairs_same_id_row_with_incomplete_result_contract(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    # Same winner and display digest, but Android cannot validate or install
    # this incomplete result. The publisher must not call it up to date.
    incomplete = {
        "capture_id": capture_id,
        "asset_id": seeded["asset_id"],
        "correction_id": seeded["correction_id"],
        "source_original_sha256": seeded["source_sha"],
        "revision": 1,
        "result": {
            "schema": "org.whl.capture-correction-result",
            "version": 1,
            "processor": "whl-desktop-corrections",
            "recipe": "whl-desktop-correction-v1",
            "correction_id": seeded["correction_id"],
            "source": {"original_sha256": seeded["source_sha"]},
            "geometry_strategy": "replace_and_reocr",
            "artifacts": {"display": {"sha256": display["sha256"]}},
            "generated_at": "2026-08-01T00:00:00Z",
        },
    }
    calls = _stub_cloud(
        monkeypatch,
        capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[incomplete],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["up_to_date"] == 0
    assert len(calls["uploads"]) == 2
    (rows,) = calls["published"]
    assert rows[0]["result"]["artifacts"]["display"]["sha256"] == \
        display["sha256"]
    assert "thumbnail" in rows[0]["result"]["artifacts"]


def test_publish_conflicts_when_cloud_row_changes_after_initial_guard(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    initial = _cloud_row(
        seeded, correction_id="0" * 64, display_sha256="1" * 64)
    initial.update({"revision": 4, "updated_at": "2026-08-01T00:00:00Z"})
    newer = _cloud_row(
        seeded, correction_id="f" * 64, display_sha256="2" * 64)
    newer.update({"revision": 5, "updated_at": "2026-08-01T00:01:00Z"})
    correction_reads = 0
    writes = []
    uploads = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        nonlocal correction_reads
        del prefer
        if path.startswith("captures?id=in.("):
            return [{"id": seeded["capture_id"], "created_by": OWNER_ID}]
        if method == "GET" and path.startswith("capture_corrections?"):
            correction_reads += 1
            return [initial if correction_reads == 1 else newer]
        writes.append((method, path, payload))
        raise AssertionError("a changed guarded row must not be written")

    monkeypatch.setattr(server.sbase, "_rest", rest)
    monkeypatch.setattr(
        server.sbase,
        "upload_object",
        lambda _cfg, bucket, path, data, content_type, upsert=True:
            uploads.append((bucket, path, len(data), content_type, upsert))
            or path,
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert correction_reads == 2
    assert len(uploads) == 2
    assert writes == []
    assert outcome["pushed"] == 0
    assert len(outcome["errors"]) == 1
    assert "changed since candidate selection" in outcome["errors"][0]


def test_publish_skips_captures_without_a_cloud_row(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 0, "up_to_date": 0,
                       "no_cloud_row": 1, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_includes_legacy_entries_without_transport_field(
        monkeypatch, capture_workspace):
    """Imports that predate ``capture_transport`` carry no field; the cloud
    row, not the transport value, gates publication."""

    seeded = _seed_corrected_capture(monkeypatch)
    _strip_transport_field(seeded["capture_id"])
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 1, "up_to_date": 0,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]


def test_publish_lan_entry_without_cloud_row_skips_as_no_cloud_row(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch, transport="lan")
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 1, "pushed": 0, "up_to_date": 0,
                       "no_cloud_row": 1, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_skips_lifecycle_failed_assets(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch, lifecycle="failed")
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome == {"candidates": 0, "pushed": 0, "up_to_date": 0,
                       "no_cloud_row": 0, "unreadable_capture": 0,
                       "notices": [], "errors": []}
    assert calls["rest"] == []
    assert calls["uploads"] == []
    assert calls["published"] == []


def _cloud_row(
        seeded, *, correction_id: str, display_sha256: str,
        revision: int = 1) -> dict:
    return {
        "capture_id": seeded["capture_id"],
        "asset_id": seeded["asset_id"],
        "correction_id": correction_id,
        "source_original_sha256": seeded["source_sha"],
        "result": {"artifacts": {"display": {"sha256": display_sha256}}},
        "revision": revision,
    }


def test_publish_skips_unreadable_capture_with_notice_not_error(
        monkeypatch, capture_workspace):
    """A permanently corrupt photo_assets.json is a per-capture skip with a
    notice; it must never flip every future sync run to ok=false, and the
    remaining candidates still publish."""

    seeded = _seed_corrected_capture(monkeypatch)
    corrupt_id = "c7777777-7777-4777-8777-777777777777"
    entry_id, errors = server.ingest_capture(
        {
            "id": corrupt_id,
            "ocr": {"photo_1.jpg": "Corrupted sage."},
            "meta": {"title": "Corrupt Herbal"},
        },
        [_jpeg(corrupt_id)],
        "",
        ["photo_1.jpg"],
        transport="cloud",
    )
    assert entry_id
    assert errors == []
    (server.CAPTURES_DIR / corrupt_id / "photo_assets.json").write_text(
        "{not json", encoding="utf-8")
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["errors"] == []
    assert outcome["unreadable_capture"] == 1
    (notice,) = outcome["notices"]
    assert notice.startswith(f"capture {corrupt_id[:8]}: ")
    assert "unreadable" in notice
    assert outcome["candidates"] == 1
    assert outcome["pushed"] == 1
    assert calls["listed"] == [[seeded["capture_id"]]]
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]


def test_publish_holds_asset_when_cloud_row_ties_on_disturbed_mtimes(
        monkeypatch, capture_workspace):
    """Equal pointer mtimes (backfill, restore, exFAT) between the cloud
    row's own publication and a sibling must skip, never downgrade."""

    seeded = _seed_corrected_capture(monkeypatch, pointer_mtime=1_000_000.0)
    sibling_png = _png(64, 48, color=(5, 100, 200))
    sibling_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"], "op-sibling", f"{seeded['namespace']}:display",
        "ctr-" + "1" * 40, sibling_png, pointer_mtime=1_000_000.0)
    assert sibling_id != seeded["correction_id"]
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id=seeded["correction_id"],
            display_sha256=display["sha256"])],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 0
    assert outcome["up_to_date"] == 0
    assert outcome["errors"] == []
    (notice,) = outcome["notices"]
    assert seeded["asset_id"] in notice
    assert "does not sort strictly newer" in notice
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_display_head_replaces_restored_newer_cloud_sibling(
        monkeypatch, capture_workspace):
    """The mutable display head, not restored pointer mtimes, owns the slot."""

    seeded = _seed_corrected_capture(
        monkeypatch,
        pointer_mtime=1_000_000.0,
        display_head=True,
    )
    sibling_png = _png(64, 48, color=(5, 100, 200))
    sibling_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"],
        "op-restored-sibling",
        f"{seeded['namespace']}:display",
        "ctr-" + "1" * 40,
        sibling_png,
        pointer_mtime=3_000_000.0,
    )
    sibling_display = server._capture_correction_jpeg(
        sibling_png,
        long_edge=1600,
        quality=90,
    )
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[_cloud_row(
            seeded,
            correction_id=sibling_id,
            display_sha256=sibling_display["sha256"],
        )],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["notices"] == []
    assert outcome["errors"] == []
    (rows,) = calls["published"]
    (row,) = rows
    assert row["correction_id"] == seeded["correction_id"]
    assert row["correction_id"] != sibling_id


def test_publish_manifest_active_operation_overrides_local_mtime_tie(
        monkeypatch, capture_workspace):
    """The backed-up manifest is authoritative over retained local history."""

    seeded = _seed_corrected_capture(
        monkeypatch,
        pointer_mtime=1_000_000.0,
        display_head=True,
    )
    sibling_png = _png(64, 48, color=(5, 100, 200))
    sibling_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"], "op-sibling", f"{seeded['namespace']}:display",
        "ctr-" + "1" * 40, sibling_png, pointer_mtime=1_000_000.0)
    manifest_path = (
        server.CAPTURES_DIR / seeded["capture_id"] / "photo_assets.json"
    )
    photo_assets = json.loads(manifest_path.read_text("utf-8"))
    _mark_original_backed_up(
        photo_assets,
        seeded["asset_id"],
        seeded["source_sha"],
        active_operation_id="op-seeded",
    )
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    sibling_display = server._capture_correction_jpeg(
        sibling_png, long_edge=1600, quality=90)
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[_cloud_row(
            seeded,
            correction_id=sibling_id,
            display_sha256=sibling_display["sha256"],
        )],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["notices"] == []
    assert outcome["errors"] == []
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]
    assert calls["photo_deletes"] == []


@pytest.mark.parametrize(
    "invalid_state",
    [
        "malformed-marker",
        "missing-raw",
        "whitespace-raw",
        "portable-original-mismatch",
    ],
)
def test_publish_fails_closed_for_invalid_original_authority(
        monkeypatch, capture_workspace, invalid_state):
    seeded = _seed_corrected_capture(monkeypatch)
    capture_id = seeded["capture_id"]
    manifest_path = server.CAPTURES_DIR / capture_id / "photo_assets.json"
    photo_assets = json.loads(manifest_path.read_text(encoding="utf-8"))
    imported = photo_assets["desktop_import"]["assets"][0]
    if invalid_state == "malformed-marker":
        imported["original_backup"] = {"version": 1}
    elif invalid_state == "missing-raw":
        imported.pop("raw_ref")
    elif invalid_state == "whitespace-raw":
        imported["raw_ref"] = "   "
    else:
        _mark_original_backed_up(
            photo_assets,
            seeded["asset_id"],
            seeded["source_sha"],
            active_operation_id="op-seeded",
        )
        photo_assets["assets"] = [{
            "asset_id": seeded["asset_id"],
            "original": {"sha256": "f" * 64},
        }]
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    calls = _stub_cloud(
        monkeypatch,
        capture_id,
        owner_rows=[{"id": capture_id, "created_by": OWNER_ID}],
        existing=[],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["candidates"] == 0
    assert outcome["pushed"] == 0
    assert outcome["unreadable_capture"] == 1
    assert outcome["errors"] == []
    assert len(outcome["notices"]) == 1
    assert "unreadable local capture" in outcome["notices"][0]
    assert calls["rest"] == []
    assert calls["listed"] == []
    assert calls["uploads"] == []
    assert calls["published"] == []
    assert calls["photo_deletes"] == []


def test_publish_manifest_active_operation_keeps_greater_foreign_row(
        monkeypatch, capture_workspace):
    """Manifest authority does not erase another desktop's cloud winner."""

    seeded = _seed_corrected_capture(monkeypatch, display_head=True)
    manifest_path = (
        server.CAPTURES_DIR / seeded["capture_id"] / "photo_assets.json"
    )
    photo_assets = json.loads(manifest_path.read_text("utf-8"))
    _mark_original_backed_up(
        photo_assets,
        seeded["asset_id"],
        seeded["source_sha"],
        active_operation_id="op-seeded",
    )
    manifest_path.write_text(json.dumps(photo_assets), encoding="utf-8")
    assert seeded["correction_id"] < "f" * 64
    calls = _stub_cloud(
        monkeypatch,
        seeded["capture_id"],
        owner_rows=[{
            "id": seeded["capture_id"],
            "created_by": OWNER_ID,
        }],
        existing=[_cloud_row(
            seeded, correction_id="f" * 64, display_sha256="1" * 64)],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 0
    assert outcome["errors"] == []
    assert len(outcome["notices"]) == 1
    assert "another desktop" in outcome["notices"][0]
    assert calls["uploads"] == []
    assert calls["published"] == []
    assert calls["photo_deletes"] == []


def test_publish_replaces_local_row_when_winner_descends_from_it(
        monkeypatch, capture_workspace):
    """Chain ancestry overrides the cloud row even when the row's own
    publication carries the newer pointer mtime."""

    seeded = _seed_corrected_capture(monkeypatch, pointer_mtime=2_000_000.0)
    chained_png = _png(64, 48, color=(9, 9, 9))
    chained_id = _publish_transform(
        server._ensure_engine_session().write_set.root,
        seeded["item_id"], "op-chained", "ctr-" + "f" * 40,
        "ctr-" + "2" * 40, chained_png, pointer_mtime=1_000_000.0)
    display = server._capture_correction_jpeg(
        seeded["png"], long_edge=1600, quality=90)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id=seeded["correction_id"],
            display_sha256=display["sha256"])],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["notices"] == []
    assert outcome["errors"] == []
    (rows,) = calls["published"]
    (row,) = rows
    assert row["correction_id"] == chained_id


def test_publish_defers_to_greater_foreign_correction_id(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    assert seeded["correction_id"] < "f" * 64
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id="f" * 64, display_sha256="1" * 64)],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 0
    assert outcome["errors"] == []
    (notice,) = outcome["notices"]
    assert "another desktop" in notice
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_replaces_lesser_foreign_correction_id(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    assert seeded["correction_id"] > "0" * 64
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[_cloud_row(
            seeded, correction_id="0" * 64, display_sha256="1" * 64,
            revision=7)],
    )

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["pushed"] == 1
    assert outcome["notices"] == []
    assert outcome["errors"] == []
    (rows,) = calls["published"]
    assert rows[0]["correction_id"] == seeded["correction_id"]
    assert calls["publish_expected"] == [{
        (seeded["capture_id"], seeded["asset_id"]): 7,
    }]


_MISSING_RELATION_ERROR = (
    "HTTP 404 on GET https://cloud.example/rest/v1/capture_corrections: "
    '{"code":"PGRST205","message":"Could not find the table '
    "'public.capture_corrections' in the schema cache\"}")


def test_publish_skips_stage_when_migration_023_is_missing(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    calls = _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def missing(_cfg, _ids):
        raise server.sbase.SyncError(_MISSING_RELATION_ERROR)

    monkeypatch.setattr(
        server.sbase, "list_capture_corrections", missing, raising=False)

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["errors"] == []
    assert outcome["skipped"] == server._CAPTURE_CORRECTION_MISSING_NOTICE
    assert outcome["pushed"] == 0
    assert calls["uploads"] == []
    assert calls["published"] == []


def test_publish_row_write_tolerates_missing_migration_too(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def missing(_cfg, _rows, *, expected_existing=None):
        del expected_existing
        raise server.sbase.SyncError(_MISSING_RELATION_ERROR)

    monkeypatch.setattr(
        server.sbase, "publish_capture_corrections", missing, raising=False)

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert outcome["errors"] == []
    assert outcome["skipped"] == server._CAPTURE_CORRECTION_MISSING_NOTICE
    assert outcome["pushed"] == 0


def test_publish_still_reports_other_sync_errors(
        monkeypatch, capture_workspace):
    seeded = _seed_corrected_capture(monkeypatch)
    _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def broken(_cfg, _ids):
        raise server.sbase.SyncError(
            "HTTP 500 on GET https://cloud.example/rest/v1/"
            "capture_corrections: upstream unavailable")

    monkeypatch.setattr(
        server.sbase, "list_capture_corrections", broken, raising=False)

    outcome = server._publish_capture_corrections(
        {"url": "cloud", "key": "service"})

    assert "skipped" not in outcome
    (error,) = outcome["errors"]
    assert error.startswith("corrections read: HTTP 500")


def test_missing_migration_keeps_the_cloud_run_ok(
        monkeypatch, capture_workspace):
    """The Android consumer tolerates a pre-023 project; the desktop run
    must too — every other family still syncs and ok stays true."""

    seeded = _seed_corrected_capture(monkeypatch)
    _stub_cloud(
        monkeypatch, seeded["capture_id"],
        owner_rows=[{"id": seeded["capture_id"], "created_by": OWNER_ID}],
        existing=[],
    )

    def missing(_cfg, _ids):
        raise server.sbase.SyncError(_MISSING_RELATION_ERROR)

    monkeypatch.setattr(
        server.sbase, "list_capture_corrections", missing, raising=False)
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(
        server, "_reconcile_cloud_capture_associations",
        lambda *_args: {"observed": 0, "bootstrapped": 0, "published": 0,
                        "queued": 0, "quarantined": 0, "errors": []})
    monkeypatch.setattr(
        server, "_publish_pending_cloud_capture_associations",
        lambda *_args: {"pushed": 0, "pending": 0, "errors": []})
    monkeypatch.setattr(server.store_sync, "sync_stores",
                        lambda *_args, **_kwargs: {"builds": {}})
    monkeypatch.setattr(server, "_books_mirror_rows", lambda: [])
    monkeypatch.setattr(server.sbase, "push_books", lambda *_args: 0)
    monkeypatch.setattr(server, "_lease_r2_cfg",
                        lambda: contextlib.nullcontext({}))
    monkeypatch.setattr(server.r2, "configured", lambda _cfg: False)
    with server._cloudsync_lock:
        before = copy.deepcopy(server._cloudsync)
    try:
        result = server._cloud_sync_run_with_configs(
            {"url": "cloud", "key": "service"}, None)
    finally:
        with server._cloudsync_lock:
            server._cloudsync.clear()
            server._cloudsync.update(before)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["capture_corrections"]["skipped"] == \
        server._CAPTURE_CORRECTION_MISSING_NOTICE
    assert result["capture_corrections"]["errors"] == []
