"""Focused contracts for projecting legacy captures into portable lib/3 seeds."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image

import capture_lib
import libformat
from librarytool.adapters.capture_lib import Lib3CaptureArchiveMaterializer


CAPTURE_ID = "a1111111-1111-4111-8111-111111111111"


def _jpeg(width: int = 11, height: int = 7, color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="JPEG")
    return output.getvalue()


def _png(width: int = 3, height: int = 2, color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def _write_pair(directory, original: bytes, display: bytes) -> None:
    directory.mkdir()
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)


def _write_pairs(directory, pairs: list[tuple[bytes, bytes]]) -> None:
    directory.mkdir()
    for index, (original, display) in enumerate(pairs, start=1):
        (directory / f"orig_{index}.jpg").write_bytes(original)
        (directory / f"photo_{index}.jpg").write_bytes(display)


def _entry(**updates) -> dict:
    value = {
        "capture_id": CAPTURE_ID,
        "title": "A Capture Herbal",
        "created_at": "2026-07-23T12:00:00+00:00",
    }
    value.update(updates)
    return value


def _photo_assets(
    original: bytes,
    display: bytes,
    *,
    original_dimensions: tuple[int, int] = (11, 7),
    display_dimensions: tuple[int, int] = (13, 9),
) -> dict:
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "assets": [
            {
                "asset_id": "asset-1",
                "capture_order": 1,
                "capture_file": "photo_1.jpg",
                "original": {
                    "reference": r"C:\private\camera\original.jpg",
                    "sha256": hashlib.sha256(original).hexdigest(),
                    "revision": 3,
                    "width": original_dimensions[0],
                    "height": original_dimensions[1],
                    "orientation": 1,
                },
                "display": {
                    "reference": "/private/cache/display.jpg",
                    "sha256": hashlib.sha256(display).hexdigest(),
                    "revision": 4,
                    "width": display_dimensions[0],
                    "height": display_dimensions[1],
                    "orientation": 1,
                    "recipe": "desktop-standardize",
                },
                "role": {
                    "suggested": "cover",
                    "confidence": 0.9,
                    "workspacePath": r"C:\private\roles.json",
                },
                "geometry": [],
            }
        ],
        "selections": {
            "primary_title": {"asset_id": "asset-1"},
            "thumbnail": {"asset_id": None},
        },
        "transport": {"representation": "original", "version": 1},
        "local_path": r"C:\private\capture",
        "provenance": {
            "homepage": "https://example.test/capture",
            "source_path": "/private/capture.json",
        },
    }


def _write_photo_assets(directory, value: dict) -> None:
    (directory / "photo_assets.json").write_text(
        json.dumps(value),
        encoding="utf-8",
    )


def _resource_json(source, member: str) -> dict:
    return json.loads(source.resources[member])


def test_source_projects_metadata_and_photo_assets_without_local_locators(
    tmp_path,
):
    original = _jpeg()
    display = _jpeg(13, 9, "gray")
    directory = tmp_path / "capture"
    _write_pair(directory, original, display)
    _write_photo_assets(directory, _photo_assets(original, display))
    (directory / "capture_notes.json").write_text(
        json.dumps({
            "schema": "org.whl.bookcapture.capture-notes",
            "version": 1,
            "capture_id": CAPTURE_ID,
            "notes": [{
                "id": "note-1",
                "transcript": "Useful shelf note",
                "local_path": r"C:\private\voice.m4a",
                "rows": [{
                    "field": "remark",
                    "value": "file:///private/transcript.txt",
                }],
            }],
        }),
        encoding="utf-8",
    )

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(
            extra={
                "series": "Library studies",
                "workspace_path": r"C:\private\workspace",
                "pdf_sources": [
                    {
                        "id": "scan",
                        "path": "/private/source.pdf",
                        "label": "Bound scan",
                    }
                ],
                "private_note": r"C:\private\loose-note.txt",
            },
            notes=r"C:\private\cataloguer-note.txt",
            category_ids=["herbals", "reference"],
        ),
        directory,
    )

    metadata = _resource_json(source, "artifacts/generated-metadata.json")
    assert metadata["extra"] == {
        "series": "Library studies",
        "pdf_sources": [{"id": "scan", "label": "Bound scan"}],
    }
    assert metadata["category_ids"] == ["herbals", "reference"]
    assert "notes" not in metadata
    assert "notes" not in source.manifest["meta"]

    assets = _resource_json(source, "artifacts/photo-assets.json")
    asset = assets["assets"][0]
    assert "capture_file" not in asset
    assert "reference" not in asset["original"]
    assert "reference" not in asset["display"]
    assert asset["original"]["representation_id"] == "capture-original-1"
    assert asset["display"]["representation_id"] == "capture-display-1"
    assert asset["original"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert asset["display"]["sha256"] == hashlib.sha256(display).hexdigest()
    assert asset["original"]["width"] == 11
    assert asset["display"]["height"] == 9
    assert asset["role"] == {"suggested": "cover", "confidence": 0.9}
    assert assets["provenance"] == {
        "homepage": "https://example.test/capture"
    }
    serialized = json.dumps(assets, sort_keys=True)
    assert "C:\\\\private" not in serialized
    assert "/private/" not in serialized
    assert "local_path" not in serialized
    notes = _resource_json(source, "artifacts/capture-notes.json")
    notes_serialized = json.dumps(notes, sort_keys=True)
    assert notes["notes"][0]["transcript"] == "Useful shelf note"
    assert "local_path" not in notes_serialized
    assert "file://" not in notes_serialized


def test_archive_keeps_deleted_capture_evidence_with_its_portable_tombstone(
    tmp_path,
):
    """A rebuilt archive remains recoverable without resurrecting the page.

    Capture deletion is membership lifecycle, not byte destruction. Both image
    representations therefore remain immutable evidence in lib/3 while the
    exact portable lifecycle marker tells every current consumer to hide them.
    """

    original = _jpeg()
    display = _jpeg(13, 9, "gray")
    directory = tmp_path / "capture-deleted"
    _write_pair(directory, original, display)
    assets = _photo_assets(original, display)
    assets["assets"][0]["desktop_lifecycle"] = {
        "state": "deleted",
        "revision": 1,
        "updated_at": 1_786_320_000_000,
    }
    _write_photo_assets(directory, assets)

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(),
        directory,
    )

    portable = _resource_json(source, "artifacts/photo-assets.json")
    assert portable["assets"][0]["desktop_lifecycle"] == {
        "state": "deleted",
        "revision": 1,
        "updated_at": 1_786_320_000_000,
    }
    assert source.resources["representations/capture-original-1.jpg"]
    assert source.resources["representations/capture-display-1.jpg"]


def test_archive_build_reads_a_corrected_capture_original_from_cold_backup(
    tmp_path,
):
    original = _jpeg()
    display = _jpeg(13, 9, "gray")
    directory = tmp_path / "library" / "captures" / CAPTURE_ID
    directory.parent.mkdir(parents=True)
    _write_pair(directory, original, display)
    assets = _photo_assets(
        original,
        display,
        original_dimensions=(11, 7),
    )
    digest = hashlib.sha256(original).hexdigest()
    assets["desktop_import"] = {
        "version": 1,
        "assets": [
            {
                "order": 0,
                "asset_id": "asset-1",
                "display_ref": "photo_1.jpg",
                "source_checksum": digest,
                "derivative_checksum": hashlib.sha256(display).hexdigest(),
                "transport_representation": "original",
                "recipe": "desktop-correction-transform-v1",
                "lifecycle": "completed",
                "original_backup": {
                    "version": 1,
                    "store": "output-originals-sha256",
                    "key": f"sha256:{digest}",
                    "sha256": digest,
                    "bytes": len(original),
                    "media_type": "image/jpeg",
                },
                "active_desktop_correction_id": "transform-op-1",
            }
        ],
    }
    _write_photo_assets(directory, assets)
    (directory / "orig_1.jpg").unlink()
    backup = (
        tmp_path
        / "library"
        / "output"
        / "backups"
        / "originals"
        / "v1"
        / "sha256"
        / digest[:2]
        / digest[2:]
    )
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original)

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(),
        directory,
    )

    assert source.resources["representations/capture-original-1.jpg"] == original
    assert source.resources["representations/capture-display-1.jpg"] == display
    portable = _resource_json(source, "artifacts/photo-assets.json")
    portable_import = portable["desktop_import"]["assets"][0]
    assert "original_backup" not in portable_import
    assert "active_desktop_correction_id" not in portable_import
    assert "output-originals-sha256" not in json.dumps(portable, sort_keys=True)


def test_cold_backup_read_rejects_parent_replacement_between_snapshot_and_open(
    tmp_path,
    monkeypatch,
):
    original = _jpeg()
    display = _jpeg(13, 9, "gray")
    directory = tmp_path / "library" / "captures" / CAPTURE_ID
    directory.parent.mkdir(parents=True)
    _write_pair(directory, original, display)
    assets = _photo_assets(original, display)
    digest = hashlib.sha256(original).hexdigest()
    assets["desktop_import"] = {
        "version": 1,
        "assets": [
            {
                "order": 0,
                "asset_id": "asset-1",
                "display_ref": "photo_1.jpg",
                "source_checksum": digest,
                "derivative_checksum": hashlib.sha256(display).hexdigest(),
                "transport_representation": "original",
                "recipe": "desktop-correction-transform-v1",
                "lifecycle": "completed",
                "original_backup": {
                    "version": 1,
                    "store": "output-originals-sha256",
                    "key": f"sha256:{digest}",
                    "sha256": digest,
                    "bytes": len(original),
                    "media_type": "image/jpeg",
                },
                "active_desktop_correction_id": "transform-op-1",
            }
        ],
    }
    _write_photo_assets(directory, assets)
    (directory / "orig_1.jpg").unlink()
    backup = (
        tmp_path
        / "library"
        / "output"
        / "backups"
        / "originals"
        / "v1"
        / "sha256"
        / digest[:2]
        / digest[2:]
    )
    backup.parent.mkdir(parents=True)
    backup.write_bytes(original)

    open_verified = capture_lib._open_verified_regular
    displaced = backup.parent.with_name(f"{backup.parent.name}-displaced")
    replaced = False

    def replace_parent(path, named_before, *, authority):
        nonlocal replaced
        if path == backup and not replaced:
            replaced = True
            backup.parent.rename(displaced)
            backup.parent.mkdir()
            (displaced / backup.name).rename(backup)
        return open_verified(path, named_before, authority=authority)

    monkeypatch.setattr(
        capture_lib,
        "_open_verified_regular",
        replace_parent,
    )

    with pytest.raises(ValueError, match="could not be read safely"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )

    assert replaced is True


@pytest.mark.parametrize(
    "mutation",
    [
        "original_checksum",
        "display_checksum",
        "original_width",
        "display_width",
    ],
)
def test_supplied_asset_evidence_must_match_embedded_image_bytes(
    tmp_path,
    mutation,
):
    original = _jpeg()
    display = _jpeg(13, 9, "gray")
    directory = tmp_path / "capture"
    _write_pair(directory, original, display)
    assets = _photo_assets(original, display)
    asset = assets["assets"][0]
    if mutation == "original_checksum":
        asset["original"]["sha256"] = "0" * 64
    elif mutation == "display_checksum":
        asset["display"]["sha256"] = "0" * 64
    elif mutation == "original_width":
        asset["original"]["width"] = 12
    else:
        asset["display"]["width"] = 14
    _write_photo_assets(directory, assets)

    with pytest.raises(ValueError, match="contradict"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )


def test_desktop_import_disambiguates_an_unembedded_phone_display(tmp_path):
    original = _jpeg()
    display = _jpeg(13, 9, "gray")
    upstream_display = _jpeg(17, 15, "blue")
    directory = tmp_path / "capture"
    _write_pair(directory, original, display)
    assets = _photo_assets(
        original,
        upstream_display,
        display_dimensions=(17, 15),
    )
    assets["desktop_import"] = {
        "version": 1,
        "imported_at": "2026-07-23T12:00:00+00:00",
        "assets": [
            {
                "order": 0,
                "asset_id": "asset-1",
                "raw_ref": "orig_1.jpg",
                "display_ref": "photo_1.jpg",
                "source_checksum": hashlib.sha256(original).hexdigest(),
                "derivative_checksum": hashlib.sha256(display).hexdigest(),
                "transport_representation": "original",
                "recipe": "desktop_perspective_standardize_v1",
                "lifecycle": "completed",
            }
        ],
    }
    _write_photo_assets(directory, assets)

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(),
        directory,
    )

    projected = _resource_json(source, "artifacts/photo-assets.json")
    display_record = projected["assets"][0]["display"]
    assert display_record["sha256"] == hashlib.sha256(display).hexdigest()
    assert (display_record["width"], display_record["height"]) == (13, 9)
    assert display_record["recipe"] == "desktop_perspective_standardize_v1"
    imported = projected["desktop_import"]["assets"][0]
    assert imported["source_sha256"] == hashlib.sha256(original).hexdigest()
    assert imported["derivative_sha256"] == hashlib.sha256(display).hexdigest()
    assert "raw_ref" not in imported
    assert "display_ref" not in imported


def test_directory_child_set_must_remain_stable_during_projection(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "capture"
    _write_pair(directory, _jpeg(), _jpeg(13, 9, "gray"))
    read_regular = capture_lib._read_regular
    changed = False

    def racing_read(path, *, maximum, artifact):
        nonlocal changed
        payload = read_regular(path, maximum=maximum, artifact=artifact)
        if artifact == "capture original 1" and not changed:
            changed = True
            (directory / "late-sidecar.json").write_text("{}", encoding="utf-8")
        return payload

    monkeypatch.setattr(capture_lib, "_read_regular", racing_read)

    with pytest.raises(ValueError, match="changed while it was archived"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )


def test_directory_snapshot_retries_one_settling_child_publication(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "capture"
    _write_pair(directory, _jpeg(), _jpeg(13, 9, "gray"))
    scandir = capture_lib.os.scandir
    calls = 0
    directory_mtime = directory.stat().st_mtime_ns

    def racing_scandir(path):
        nonlocal calls
        if path != directory:
            return scandir(path)
        calls += 1
        if calls == 1:
            (directory / "settled-sidecar.json").write_text(
                "{}",
                encoding="utf-8",
            )
            capture_lib.os.utime(
                directory,
                ns=(
                    directory.stat().st_atime_ns,
                    directory_mtime + 1_000_000_000,
                ),
            )
        return scandir(path)

    monkeypatch.setattr(capture_lib.os, "scandir", racing_scandir)

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(),
        directory,
    )

    assert calls == 3
    assert source.resources[
        "representations/capture-original-1.jpg"
    ] == _jpeg()
    assert source.resources[
        "representations/capture-display-1.jpg"
    ] == _jpeg(13, 9, "gray")


def test_directory_snapshot_rejects_continuous_generation_churn(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "capture"
    _write_pair(directory, _jpeg(), _jpeg(13, 9, "gray"))
    scandir = capture_lib.os.scandir
    calls = 0
    directory_mtime = directory.stat().st_mtime_ns

    def racing_scandir(path):
        nonlocal calls
        nonlocal directory_mtime
        if path != directory:
            return scandir(path)
        calls += 1
        (directory / f"sidecar-{calls}.json").write_text(
            "{}",
            encoding="utf-8",
        )
        directory_mtime += 1_000_000_000
        capture_lib.os.utime(
            directory,
            ns=(directory.stat().st_atime_ns, directory_mtime),
        )
        return scandir(path)

    monkeypatch.setattr(capture_lib.os, "scandir", racing_scandir)

    with pytest.raises(ValueError, match="changed while it was listed"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )

    assert calls == capture_lib._CAPTURE_DIRECTORY_SNAPSHOT_ATTEMPTS


def test_directory_snapshot_does_not_retry_authority_replacement(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "capture"
    _write_pair(directory, _jpeg(), _jpeg(13, 9, "gray"))
    replacement = tmp_path / "replacement"
    _write_pair(replacement, _jpeg(), _jpeg(13, 9, "gray"))
    displaced = tmp_path / "displaced"
    scandir = capture_lib.os.scandir
    calls = 0

    def racing_scandir(path):
        nonlocal calls
        if path != directory:
            return scandir(path)
        calls += 1
        if calls == 1:
            directory.rename(displaced)
            replacement.rename(directory)
        return scandir(path)

    monkeypatch.setattr(capture_lib.os, "scandir", racing_scandir)

    with pytest.raises(ValueError, match="changed while it was listed"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )

    assert calls == 1


def test_directory_snapshot_pins_authority_across_retry(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "capture"
    _write_pair(directory, _jpeg(), _jpeg(13, 9, "gray"))
    replacement = tmp_path / "replacement"
    _write_pair(replacement, _jpeg(color="blue"), _jpeg(13, 9, "green"))
    displaced = tmp_path / "displaced"
    scandir = capture_lib.os.scandir
    snapshot_attempt = capture_lib._capture_directory_snapshot_attempt
    scandir_calls = 0
    attempt_calls = 0
    directory_mtime = directory.stat().st_mtime_ns

    def settling_scandir(path):
        nonlocal scandir_calls
        if path != directory:
            return scandir(path)
        scandir_calls += 1
        if scandir_calls == 1:
            (directory / "settled-sidecar.json").write_text(
                "{}",
                encoding="utf-8",
            )
            capture_lib.os.utime(
                directory,
                ns=(
                    directory.stat().st_atime_ns,
                    directory_mtime + 1_000_000_000,
                ),
            )
        return scandir(path)

    def replacing_attempt(path, *, expected_authority):
        nonlocal attempt_calls
        attempt_calls += 1
        result = snapshot_attempt(
            path,
            expected_authority=expected_authority,
        )
        if attempt_calls == 1:
            directory.rename(displaced)
            replacement.rename(directory)
        return result

    monkeypatch.setattr(capture_lib.os, "scandir", settling_scandir)
    monkeypatch.setattr(
        capture_lib,
        "_capture_directory_snapshot_attempt",
        replacing_attempt,
    )

    with pytest.raises(ValueError, match="changed while it was listed"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )

    assert attempt_calls == 2
    assert scandir_calls == 1


@pytest.mark.parametrize(
    "payload",
    [
        b"opaque-image-bytes",
        b"\xff\xd8\xff\xe0truncated-jpeg",
        pytest.param(_png(), id="valid-png"),
    ],
)
def test_legacy_capture_rejects_opaque_non_jpeg_and_corrupt_images(
    tmp_path,
    payload,
):
    directory = tmp_path / "capture"
    _write_pair(directory, payload, _jpeg())

    with pytest.raises(ValueError, match="JPEG image"):
        capture_lib.build_capture_archive_source(
            CAPTURE_ID,
            _entry(),
            directory,
        )


def test_multi_photo_aggregate_artifacts_cover_every_representation_and_materialize(
    tmp_path,
):
    directory = tmp_path / "capture"
    pairs = [
        (_jpeg(11, 7, "white"), _jpeg(13, 9, "gray")),
        (_jpeg(17, 11, "blue"), _jpeg(19, 13, "green")),
    ]
    _write_pairs(directory, pairs)
    (directory / "ocr.txt").write_text(
        "First capture page.\n\nSecond capture page.",
        encoding="utf-8",
    )

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(),
        directory,
    )

    representations = {
        representation["id"]: representation
        for representation in source.manifest["representations"]
    }
    groups = [
        representation
        for representation in representations.values()
        if representation["role"] == "capture-group"
    ]
    assert len(groups) == 1
    aggregate = groups[0]
    assert {
        link["representation_id"] for link in aggregate["lineage"]
    } == {"capture-display-1", "capture-display-2"}
    assert all(len(record["lineage"]) <= 64 for record in groups)
    assert representations["capture-display-1"]["lineage"][0][
        "representation_id"
    ] == "capture-original-1"
    assert representations["capture-display-2"]["lineage"][0][
        "representation_id"
    ] == "capture-original-2"

    aggregate_artifact_ids = {
        "capture-photo-assets",
        "capture-generated-metadata",
        "capture-geometry",
        "capture-notes",
        "capture-provenance",
        "capture-ocr",
    }
    aggregate_sources = {
        artifact["id"]: artifact["source"]
        for artifact in source.manifest["artifacts"]
        if artifact["id"] in aggregate_artifact_ids
    }
    assert set(aggregate_sources) == aggregate_artifact_ids
    assert set(
        (value["representation_id"], value["representation_revision"])
        for value in aggregate_sources.values()
    ) == {(aggregate["id"], aggregate["revision"])}

    archive = Lib3CaptureArchiveMaterializer(libformat).materialize(
        source,
        book_id="b-" + "1" * 32,
    )
    opened = libformat.read_lib(archive)
    assert [
        issue.as_dict()
        for issue in libformat.validate(opened)
        if issue.level == "error"
    ] == []


def test_large_capture_group_tree_never_exceeds_lib3_lineage_limit(tmp_path):
    directory = tmp_path / "capture"
    original = _jpeg(3, 2, "white")
    display = _jpeg(4, 3, "gray")
    _write_pairs(directory, [(original, display)] * 65)

    source = capture_lib.build_capture_archive_source(
        CAPTURE_ID,
        _entry(),
        directory,
    )

    groups = [
        representation
        for representation in source.manifest["representations"]
        if representation["role"] == "capture-group"
    ]
    assert len(groups) == 3
    assert max(len(group["lineage"]) for group in groups) == 64
    artifact = next(
        record
        for record in source.manifest["artifacts"]
        if record["id"] == "capture-generated-metadata"
    )
    root = next(
        group
        for group in groups
        if group["id"] == artifact["source"]["representation_id"]
    )
    assert root["revision"] == artifact["source"]["representation_revision"]
    assert len(root["lineage"]) == 2
