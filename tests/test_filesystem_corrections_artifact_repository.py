from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

import libformat
from librarytool.adapters.filesystem import (
    corrections_artifact_repository as corrections_artifact_module,
)
from librarytool.adapters.filesystem.corrections_artifact_repository import (
    FilesystemCorrectionsArtifactRepository,
    FilesystemRasterResourceResolverPort,
    _open_authorized_descriptor,
    _windows_path_is_below,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.engine.errors import NotFoundError, RepositoryError
from librarytool.engine.raster_artifacts import (
    MAX_METADATA_ASSERTIONS,
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    ResourceState,
)
from librarytool.engine.spatial_annotations import (
    SpatialAnnotationKey,
    SpatialAnnotationProjectorPort,
)


ITEM_ID = "book-1"
CAPTURE_ID = "capture-1"


def _jpeg_bytes(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


def _png_bytes(color: tuple[int, int, int], size: tuple[int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _digest(value: bytes) -> str:
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


CAPTURE_NAMESPACE = _opaque_identity("capture", CAPTURE_ID, "asset-1")
CAPTURE_DISPLAY_ID = f"{CAPTURE_NAMESPACE}:display"
CAPTURE_ORIGINAL_ID = f"{CAPTURE_NAMESPACE}:original"
FIGURE_ID = _opaque_identity("figure", "p3-fig.png")
FIGURE_BOX_ID = _opaque_identity("figure-box", "primary", 3, "p3-fig.png")
STABLE_REGION_ID = _opaque_identity("region", "stable-region-7")
PIXEL_REGION_ID = _opaque_identity("region", "pixel-region")


def _entry(root: Path, item_id: str = ITEM_ID) -> Path:
    return root / "entries" / item_id


def _capture(root: Path, capture_id: str = CAPTURE_ID) -> Path:
    return root / "captures" / capture_id


def _snapshot(*directories: Path) -> dict[str, tuple[bytes, int]]:
    values: dict[str, tuple[bytes, int]] = {}
    for directory in directories:
        if not directory.exists():
            continue
        for candidate in directory.rglob("*"):
            if candidate.is_file():
                values[str(candidate)] = (
                    candidate.read_bytes(),
                    candidate.stat().st_mtime_ns,
                )
    return values


def _repository(
    root: Path,
    *,
    capture_ids: dict[str, str] | None = None,
    representation_revisions: dict[tuple[str, str], str] | None = None,
    entry_directories: dict[str, Path] | None = None,
) -> FilesystemCorrectionsArtifactRepository:
    captures = capture_ids if capture_ids is not None else {ITEM_ID: CAPTURE_ID}
    revisions = (
        representation_revisions
        if representation_revisions is not None
        else {(ITEM_ID, "primary"): "rep-primary-r1"}
    )
    write_set = RecoverableWriteSet(root)
    # The workspace lease owns a process-level lock file. Prime that global
    # resource before tests compare the managed capture/entry trees.
    with write_set.workspace_lease():
        pass

    @contextmanager
    def lock():
        yield

    return FilesystemCorrectionsArtifactRepository(
        write_set,
        item_exists=lambda item_id: item_id in {ITEM_ID, "book-2"},
        capture_id_for=lambda item_id: captures.get(item_id),
        entry_directory_for=lambda item_id: (
            entry_directories.get(item_id, _entry(root, item_id))
            if entry_directories is not None
            else _entry(root, item_id)
        ),
        capture_directory_for=lambda capture_id: _capture(root, capture_id),
        representation_revision_for=lambda item_id, representation_id: revisions.get(
            (item_id, representation_id)
        ),
        lock_context_for=lock,
    )


def _photo_manifest(
    original: bytes,
    display: bytes,
    *,
    role: dict | None = None,
    geometry: list[dict] | None = None,
) -> dict:
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "legacy_fallback": False,
        "assets": [
            {
                "asset_id": "asset-1",
                "capture_order": 1,
                "capture_file": "photo_1.jpg",
                "original": {
                    "reference": "original_asset-1.jpg",
                    "sha256": _digest(original),
                    "revision": 3,
                    "width": 2,
                    "height": 2,
                    "orientation": 90,
                    "future": {"source": "camera"},
                },
                "display": {
                    "reference": "photo_1.jpg",
                    "sha256": _digest(display),
                    "revision": 4,
                    "width": 2,
                    "height": 2,
                    "orientation": 0,
                    "recipe": "android-standardize",
                    "recipe_version": "1",
                },
                "lifecycle": {"state": "completed"},
                "role": role
                or {
                    "suggested": "title_page",
                    "confidence": 0.8,
                    "algorithm": "android-bibliographic-title-page",
                    "algorithm_version": "1",
                    "manual_override": "cover",
                    "manual_revision": 2,
                    "manual_updated_at": 1234,
                },
                "geometry": geometry or [],
                "future": {"lens": "macro"},
            }
        ],
        "selections": {
            "primary_title": {"asset_id": "asset-1"},
            "thumbnail": {"asset_id": "asset-1"},
        },
        "transport": {"representation": "original", "version": 1},
        "desktop_import": {
            "version": 1,
            "assets": [
                {
                    "order": 0,
                    "asset_id": "asset-1",
                    "raw_ref": "orig_1.jpg",
                    "display_ref": "photo_1.jpg",
                    "source_checksum": _digest(original),
                    "derivative_checksum": _digest(display),
                    "transport_representation": "original",
                    "recipe": "desktop_perspective_standardize_v1",
                    "lifecycle": "completed",
                }
            ],
        },
    }


def _capture_geometry(
    original: bytes,
    *,
    region_id: str = "heading-1",
    text: str = "A Flora",
) -> dict:
    return {
        "asset_id": "asset-1",
        "source_sha256": _digest(original),
        "source_revision": 3,
        "display_revision": 4,
        "coordinate_space": "display_normalized",
        "width": 19,
        "height": 29,
        "orientation": 0,
        "engine": "mistral",
        "model": "mistral-ocr-latest",
        "engine_version": "ocr-4-blocks",
        "regions": [
            {
                "id": region_id,
                "type": "text",
                "text": text,
                "confidence": 0.97,
                "polygon": [
                    [0.1, 0.2],
                    [0.9, 0.2],
                    [0.9, 0.3],
                    [0.1, 0.3],
                ],
            }
        ],
    }


def _write_photo_manifest(root: Path, manifest: dict) -> Path:
    directory = _capture(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "photo_assets.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def _write_layout(root: Path, layout: dict) -> Path:
    directory = _entry(root) / "ocr"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "layout.json"
    path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    return path


def _swap_to_external_hardlink(
    monkeypatch,
    *,
    target: Path,
    external: Path,
    on_open: int = 1,
) -> None:
    real_open = os.open
    matching_opens = 0

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal matching_opens
        candidate = Path(path)
        if candidate == target or (
            not candidate.is_absolute()
            and candidate.name == target.name
            and "dir_fd" in kwargs
        ):
            matching_opens += 1
            if matching_opens == on_open:
                target.replace(target.with_name(f"{target.name}.original"))
                os.link(external, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_windows_authority_comparison_does_not_apply_unicode_casefolding():
    assert _windows_path_is_below(
        r"\\?\C:\workspace\root\book\layout.json",
        r"C:\workspace\root",
    )
    assert not _windows_path_is_below(
        r"\\?\C:\workspace\ROOT\book\layout.json",
        r"C:\workspace\root",
    )
    assert not _windows_path_is_below(
        r"C:\workspace\fooss\book\layout.json",
        r"C:\workspace\fooß",
    )


def test_sidecar_name_swap_cannot_disclose_external_json(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    layout_path = _write_layout(root, _layout("ab" * 32))
    external = tmp_path / "external-layout.json"
    leaked = _layout("cd" * 32)
    leaked["regions"]["primary"]["3"]["items"][0]["text"] = "EXTERNAL SECRET"
    external.write_text(json.dumps(leaked), encoding="utf-8")
    _swap_to_external_hardlink(
        monkeypatch,
        target=layout_path,
        external=external,
    )

    with pytest.raises(RepositoryError) as caught:
        _repository(root, capture_ids={}).list_spatial_annotations(ITEM_ID)

    assert caught.value.code == "invalid_mistral_layout"
    assert "EXTERNAL SECRET" not in str(caught.value)


def test_sidecar_ancestor_redirect_cannot_escape_the_authority_root(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    layout_path = _write_layout(root, _layout("ab" * 32))
    external_entries = tmp_path / "external-entries"
    external_layout = external_entries / ITEM_ID / "ocr" / "layout.json"
    external_layout.parent.mkdir(parents=True)
    leaked = _layout("cd" * 32)
    leaked["regions"]["primary"]["3"]["items"][0]["text"] = "EXTERNAL SECRET"
    external_layout.write_text(json.dumps(leaked), encoding="utf-8")
    repository = _repository(root, capture_ids={})
    real_assert = repository._assert_safe_path
    swapped = False

    def swapping_assert(path, **kwargs):
        nonlocal swapped
        result = real_assert(path, **kwargs)
        if Path(path) == layout_path and not swapped:
            swapped = True
            entries = root / "entries"
            backup = root / "entries.original"
            entries.replace(backup)
            try:
                os.symlink(
                    external_entries,
                    entries,
                    target_is_directory=True,
                )
            except OSError:
                if entries.is_symlink():
                    entries.unlink()
                backup.replace(entries)
                pytest.skip("directory symlinks are unavailable")
        return result

    monkeypatch.setattr(repository, "_assert_safe_path", swapping_assert)

    with pytest.raises(RepositoryError) as caught:
        repository.list_spatial_annotations(ITEM_ID)

    assert caught.value.code == "invalid_mistral_layout"
    assert "EXTERNAL SECRET" not in str(caught.value)


def test_sidecar_ancestor_redirect_cannot_cross_item_authority(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    layout_path = _write_layout(root, _layout("ab" * 32))
    private_layout = _entry(root, "book-2") / "ocr" / "layout.json"
    private_layout.parent.mkdir(parents=True)
    leaked = _layout("cd" * 32)
    leaked["regions"]["primary"]["3"]["items"][0]["text"] = "PRIVATE ITEM SECRET"
    private_layout.write_text(json.dumps(leaked), encoding="utf-8")
    repository = _repository(root, capture_ids={})
    real_assert = repository._assert_safe_path
    swapped = False
    item_directory = _entry(root)
    private_item_directory = _entry(root, "book-2")
    backup = _entry(root, "book-1.original")

    def swapping_assert(path, **kwargs):
        nonlocal swapped
        authority = real_assert(path, **kwargs)
        if Path(path) == layout_path and not swapped:
            swapped = True
            item_directory.replace(backup)
            private_item_directory.replace(item_directory)
        return authority

    monkeypatch.setattr(repository, "_assert_safe_path", swapping_assert)

    try:
        with pytest.raises(RepositoryError) as caught:
            repository.list_spatial_annotations(ITEM_ID)
    finally:
        if swapped:
            item_directory.replace(private_item_directory)
            backup.replace(item_directory)

    assert caught.value.code == "invalid_mistral_layout"
    assert "PRIVATE ITEM SECRET" not in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle semantics")
def test_opened_file_must_remain_under_its_guarded_parent(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    layout_path = _write_layout(root, _layout("ab" * 32))
    private_layout = _entry(root, "book-2") / "ocr" / "layout.json"
    private_layout.parent.mkdir(parents=True)
    private_layout.write_text(
        json.dumps(_layout("cd" * 32)),
        encoding="utf-8",
    )
    repository = _repository(root, capture_ids={})
    authority = repository._assert_safe_path(
        layout_path,
        item_id=ITEM_ID,
        section="layout",
    )
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if Path(path) == layout_path and not swapped:
            swapped = True
            item_directory = _entry(root) / "ocr"
            item_directory.replace(_entry(root) / "ocr.original")
            os.symlink(
                _entry(root, "book-2") / "ocr",
                item_directory,
                target_is_directory=True,
            )
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(OSError, match="escaped its authority parent"):
        _open_authorized_descriptor(
            layout_path,
            authority,
        )

    assert swapped is True


def test_android_capture_projection_is_stable_safe_and_read_only(tmp_path):
    root = tmp_path / "library"
    original = _jpeg_bytes((120, 20, 30), (17, 23))
    display = _jpeg_bytes((20, 120, 30), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = _photo_manifest(original, display)
    path = _write_photo_manifest(root, manifest)
    repository = _repository(root)
    before = _snapshot(directory, _entry(root))

    artifacts = repository.list_raster_artifacts(ITEM_ID)

    assert isinstance(repository, RasterArtifactProjectorPort)
    assert isinstance(repository, SpatialAnnotationProjectorPort)
    assert isinstance(repository, FilesystemRasterResourceResolverPort)
    assert [artifact.key.artifact_id for artifact in artifacts] == [
        CAPTURE_DISPLAY_ID,
        CAPTURE_ORIGINAL_ID,
    ]
    display_view, original_view = artifacts
    assert display_view.kind == "processed-image"
    assert display_view.media_type == "image/jpeg"
    assert display_view.dimensions.as_dict() == {
        "width": 19,
        "height": 29,
        "orientation": 1,
    }
    assert original_view.dimensions.as_dict() == {
        "width": 17,
        "height": 23,
        "orientation": 6,
    }
    assert display_view.effective_category == "cover"
    assert [value.origin.value for value in display_view.category_assignments] == [
        "suggested",
        "manual",
    ]
    assert display_view.lineage[0].artifact_id == original_view.key.artifact_id
    assert display_view.extensions["android"]["future"]["lens"] == "macro"
    assert (
        display_view.extensions["corrections_ui"]["annotation_frame"]
        == "canvas"
    )
    assert original_view.extensions["rendition"]["future"]["source"] == "camera"
    assert display_view.resource is not None
    assert display_view.resource.resource_id.startswith("raster:")
    assert str(root) not in json.dumps([value.as_dict() for value in artifacts])
    resolved = repository.resolve_raster_resource(
        ITEM_ID,
        display_view.resource,
    )
    assert resolved is not None
    assert resolved.stream.read() == display
    resolved.stream.close()
    assert resolved.media_type == "image/jpeg"
    assert resolved.size == len(display)
    assert resolved.content_sha256 == _digest(display)
    assert _snapshot(directory, _entry(root)) == before

    first_ids = [value.key.artifact_id for value in artifacts]
    first_resources = [value.resource for value in artifacts]
    changed = copy.deepcopy(manifest)
    changed["assets"][0]["role"]["manual_override"] = "spine"
    changed["assets"][0]["role"]["manual_revision"] = 3
    path.write_text(json.dumps(changed, indent=2), encoding="utf-8")

    after = repository.list_raster_artifacts(ITEM_ID)
    assert [value.key.artifact_id for value in after] == first_ids
    assert [value.resource for value in after] == first_resources
    assert all(value.effective_category == "spine" for value in after)


def test_android_geometry_projects_only_on_its_revision_pinned_display(tmp_path):
    root = tmp_path / "library"
    original = _jpeg_bytes((120, 20, 30), (17, 23))
    display = _jpeg_bytes((20, 120, 30), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(display)
    geometry = _capture_geometry(original)
    manifest = _photo_manifest(original, display, geometry=[geometry])
    manifest["assets"][0]["display"].update({"width": 19, "height": 29})
    manifest_path = _write_photo_manifest(root, manifest)
    repository = _repository(root)

    artifacts = {
        value.key.artifact_id: value
        for value in repository.list_raster_artifacts(ITEM_ID)
    }
    annotations = repository.list_spatial_annotations(ITEM_ID)

    assert len(annotations) == 1
    annotation = annotations[0]
    first_key = annotation.key
    first_revision = annotation.revision
    display_view = artifacts[CAPTURE_DISPLAY_ID]
    original_view = artifacts[CAPTURE_ORIGINAL_ID]
    assert annotation.source.as_dict() == display_view.source.as_dict()
    assert annotation.source.canvas_revision != original_view.source.canvas_revision
    assert annotation.selector.coordinate_space == "display_normalized"
    assert [point.as_dict() for point in annotation.selector.points] == [
        {"x": 0.1, "y": 0.2},
        {"x": 0.9, "y": 0.2},
        {"x": 0.9, "y": 0.3},
        {"x": 0.1, "y": 0.3},
    ]
    assert annotation.effective_role == "text"
    assert annotation.role_assignments[0].confidence == 0.97
    assert annotation.extensions["text"] == "A Flora"
    assert (
        annotation.extensions["android_geometry"]["region_id"]
        == "heading-1"
    )
    assert annotation.linked_artifact_ids == (CAPTURE_DISPLAY_ID,)
    assert annotation.provenance.provider_id == "mistral"

    changed = copy.deepcopy(manifest)
    changed["assets"][0]["geometry"][0]["regions"][0]["text"] = "A Flora revised"
    changed["assets"][0]["geometry"][0]["regions"][0]["polygon"][0] = [0.2, 0.2]
    manifest_path.write_text(json.dumps(changed), encoding="utf-8")
    revised = repository.list_spatial_annotations(ITEM_ID)
    assert len(revised) == 1
    assert revised[0].key == first_key
    assert revised[0].revision != first_revision

    mismatches = (
        ("asset_id", "other-asset"),
        ("source_revision", 2),
        ("display_revision", 3),
        ("coordinate_space", "original_normalized"),
        # Geometry itself must continue to match the display canvas.
        ("width", 2),
        ("height", 2),
        ("orientation", 90),
    )
    for field, value in mismatches:
        stale = copy.deepcopy(manifest)
        stale["assets"][0]["geometry"][0][field] = value
        manifest_path.write_text(json.dumps(stale), encoding="utf-8")
        assert repository.list_spatial_annotations(ITEM_ID) == ()

    declared_mismatch = copy.deepcopy(manifest)
    declared_mismatch["assets"][0]["display"].update(
        {"width": 2, "height": 2}
    )
    manifest_path.write_text(json.dumps(declared_mismatch), encoding="utf-8")
    assert repository.list_spatial_annotations(ITEM_ID) == ()

    transformed = _jpeg_bytes((1, 2, 3), (19, 29))
    imported = copy.deepcopy(manifest)
    imported["desktop_import"]["assets"][0]["derivative_checksum"] = _digest(
        transformed
    )
    display_path.write_bytes(transformed)
    manifest_path.write_text(json.dumps(imported), encoding="utf-8")
    assert {
        value.key.artifact_id
        for value in repository.list_raster_artifacts(ITEM_ID)
    } == {
        CAPTURE_DISPLAY_ID,
        CAPTURE_ORIGINAL_ID,
    }
    # The phone-frame record no longer describes the granted derivative:
    # nothing projects, and the artifact reports the geometry as
    # unavailable rather than silently empty.
    assert repository.list_spatial_annotations(ITEM_ID) == ()

    # A desktop-remapped record pinned to the derivative's content hash
    # projects over the imported display; the phone-frame record stays
    # superseded without a staleness diagnostic.
    pinned = copy.deepcopy(imported)
    pinned_record = copy.deepcopy(geometry)
    pinned_record["display_sha256"] = _digest(transformed)
    pinned_record["remap_recipe"] = "desktop-geometry-remap-v1"
    pinned_record["regions"][0]["polygon"] = [
        [0.15, 0.25],
        [0.85, 0.25],
        [0.85, 0.35],
        [0.15, 0.35],
    ]
    pinned["assets"][0]["geometry"] = [
        copy.deepcopy(geometry),
        pinned_record,
    ]
    manifest_path.write_text(json.dumps(pinned), encoding="utf-8")
    pinned_annotations = repository.list_spatial_annotations(ITEM_ID)
    assert len(pinned_annotations) == 1
    assert [
        point.as_dict() for point in pinned_annotations[0].selector.points
    ] == [
        {"x": 0.15, "y": 0.25},
        {"x": 0.85, "y": 0.25},
        {"x": 0.85, "y": 0.35},
        {"x": 0.15, "y": 0.35},
    ]
    display_artifact = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert display_artifact is not None
    assert not [
        diagnostic
        for diagnostic in display_artifact.extensions.get("diagnostics", [])
        if diagnostic.get("scope") == "capture_geometry"
    ]

    # A pinned record for some other derivative generation is ignored
    # silently — it is not stale, it is simply not this display.
    foreign_pin = copy.deepcopy(imported)
    foreign_record = copy.deepcopy(pinned_record)
    foreign_record["display_sha256"] = "ab" * 32
    foreign_pin["assets"][0]["geometry"] = [foreign_record]
    manifest_path.write_text(json.dumps(foreign_pin), encoding="utf-8")
    assert repository.list_spatial_annotations(ITEM_ID) == ()


def test_blank_local_display_checksum_keeps_revision_pinned_geometry(
    tmp_path,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((120, 20, 30), (17, 23))
    display = _jpeg_bytes((20, 120, 30), (19, 29))
    replacement = _jpeg_bytes((1, 2, 3), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "original_asset-1.jpg").write_bytes(original)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(display)
    manifest = _photo_manifest(
        original,
        display,
        geometry=[_capture_geometry(original)],
    )
    manifest["assets"][0]["display"].update({"width": 19, "height": 29})
    manifest.pop("desktop_import")
    manifest["assets"][0]["display"]["sha256"] = ""
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    before = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert before is not None
    before_annotations = repository.list_spatial_annotations(ITEM_ID)
    assert len(before_annotations) == 1

    display_path.write_bytes(replacement)
    after = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert after is not None
    assert after.content_sha256 == _digest(replacement)
    assert after.source.canvas_revision != before.source.canvas_revision
    after_annotations = repository.list_spatial_annotations(ITEM_ID)
    assert len(after_annotations) == 1
    assert after_annotations[0].key == before_annotations[0].key
    assert after_annotations[0].revision != before_annotations[0].revision
    assert (
        after_annotations[0].source.canvas_revision
        == after.source.canvas_revision
    )


def test_partial_legacy_capture_keeps_explicit_missing_renditions(tmp_path):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(display)
    manifest = _photo_manifest(original, display)
    manifest.pop("desktop_import")
    for rendition in ("original", "display"):
        manifest["assets"][0][rendition].update(
            {"sha256": "", "width": 0, "height": 0}
        )
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    artifacts = {
        value.key.artifact_id: value
        for value in repository.list_raster_artifacts(ITEM_ID)
    }
    assert set(artifacts) == {CAPTURE_DISPLAY_ID, CAPTURE_ORIGINAL_ID}
    assert artifacts[CAPTURE_DISPLAY_ID].resource_state is ResourceState.AVAILABLE
    assert artifacts[CAPTURE_ORIGINAL_ID].resource_state is ResourceState.MISSING
    assert artifacts[CAPTURE_ORIGINAL_ID].dimensions.as_dict() == {
        "width": 1,
        "height": 1,
        "orientation": 6,
    }

    display_path.unlink()
    (directory / "original_asset-1.jpg").write_bytes(original)
    artifacts = {
        value.key.artifact_id: value
        for value in repository.list_raster_artifacts(ITEM_ID)
    }
    assert set(artifacts) == {CAPTURE_DISPLAY_ID, CAPTURE_ORIGINAL_ID}
    assert artifacts[CAPTURE_DISPLAY_ID].resource_state is ResourceState.MISSING
    assert artifacts[CAPTURE_ORIGINAL_ID].resource_state is ResourceState.AVAILABLE


def test_malformed_rendition_and_geometry_preserve_healthy_capture_artifact(
    tmp_path,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = _photo_manifest(original, display)
    manifest["assets"][0]["original"] = ["invalid", "rendition"]
    manifest["assets"][0]["geometry"] = ["invalid-geometry"]
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    artifacts = {
        value.key.artifact_id: value
        for value in repository.list_raster_artifacts(ITEM_ID)
    }

    assert set(artifacts) == {CAPTURE_DISPLAY_ID, CAPTURE_ORIGINAL_ID}
    original_view = artifacts[CAPTURE_ORIGINAL_ID]
    display_view = artifacts[CAPTURE_DISPLAY_ID]
    assert original_view.resource_state is ResourceState.UNAVAILABLE
    assert original_view.resource is None
    assert original_view.extensions["artifact_diagnostics"] == (
        {
            "scope": "capture_rendition",
            "code": "capture_rendition_invalid",
            "state": "unavailable",
            "component": "original",
        },
    )
    assert display_view.resource_state is ResourceState.AVAILABLE
    assert display_view.resource is not None
    assert display_view.extensions["artifact_diagnostics"] == (
        {
            "scope": "capture_geometry",
            "code": "capture_geometry_invalid",
            "state": "unavailable",
            "component": "display",
        },
    )
    assert repository.list_spatial_annotations(ITEM_ID) == ()
    assert str(directory) not in json.dumps(
        [value.as_dict() for value in artifacts.values()]
    )


def test_pre_contract_capture_projects_and_resolves_without_manifest(tmp_path):
    root = tmp_path / "library"
    original = _jpeg_bytes((13, 23, 33), (17, 23))
    display = _jpeg_bytes((43, 53, 63), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    repository = _repository(root)

    hints = repository.list_capture_index_hints(ITEM_ID)
    assert len(hints) == 1
    assert hints[0]["revision"].startswith("index:")
    assert hints[0]["import_state"] == "legacy"
    assert hints[0]["imported_at"] == ""
    assert hints[0]["resource_state"] == "available"

    artifacts = repository.list_raster_artifacts(ITEM_ID)
    assert len(artifacts) == 2
    shown = next(
        value for value in artifacts
        if value.key.artifact_id.endswith(":display")
    )
    assert shown.extensions["legacy_capture"] is True
    assert shown.resource is not None
    resolved = repository.resolve_raster_resource(ITEM_ID, shown.resource)
    assert resolved is not None
    try:
        assert resolved.stream.read() == display
    finally:
        resolved.stream.close()


def test_capture_hints_do_not_open_or_hash_image_bytes(tmp_path, monkeypatch):
    root = tmp_path / "library"
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "original_asset-1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))
    repository = _repository(root)

    def reject_observation(*_args, **_kwargs):
        raise AssertionError("capture index must not inspect image bytes")

    monkeypatch.setattr(repository, "_observe_resource", reject_observation)
    hints = repository.list_capture_index_hints(ITEM_ID)

    assert len(hints) == 1
    assert hints[0]["resource_state"] == "available"


def test_capture_hints_match_rendition_group_state_and_import_timestamp(tmp_path):
    root = tmp_path / "library"
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = _photo_manifest(original, display)
    manifest["desktop_import"]["imported_at"] = "2026-08-04T12:34:56Z"
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    hint = repository.list_capture_index_hints(ITEM_ID)[0]
    artifacts = repository.list_raster_artifacts(ITEM_ID)
    states = {
        value.resource.variant: value.resource_state.value
        for value in artifacts
        if value.resource is not None
    }
    missing = [
        value
        for value in artifacts
        if value.resource_state is ResourceState.MISSING
    ]

    assert hint["resource_state"] == "available"
    assert hint["import_state"] == "partial"
    assert hint["imported_at"] == "2026-08-04T12:34:56Z"
    assert states == {"display": "available"}
    assert len(missing) == 1
    assert missing[0].key.artifact_id.endswith(":original")


def test_invalid_display_rendition_cannot_preview_imported_display_ref(tmp_path):
    root = tmp_path / "library"
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = _photo_manifest(original, display)
    manifest["assets"][0]["display"] = ["invalid rendition"]
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    hint = repository.list_capture_index_hints(ITEM_ID)[0]

    assert hint["resource_state"] == "unavailable"
    assert hint["import_state"] == "partial"
    assert repository.resolve_capture_preview(
        ITEM_ID,
        CAPTURE_DISPLAY_ID,
    ) is None


def test_malformed_capture_manifest_degrades_to_unavailable_index_hint(tmp_path):
    root = tmp_path / "library"
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "photo_assets.json").write_text("{", encoding="utf-8")

    hints = _repository(root).list_capture_index_hints(ITEM_ID)

    assert len(hints) == 1
    assert hints[0]["capture_order"] == 0
    assert hints[0]["resource_state"] == "unavailable"
    assert hints[0]["import_state"] == "unavailable"
    assert hints[0]["imported_at"] == ""


def test_entry_directory_change_invalidates_projection_and_resource_caches(
    tmp_path,
):
    root = tmp_path / "library"
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))

    figure = _png_bytes((20, 130, 50), (41, 37))
    image_dir = _entry(root) / "ocr" / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "p3-fig.png").write_bytes(figure)
    _write_layout(root, _layout(_digest(figure)))
    manual_entry = root / "entries" / "_corrections_capture_only" / ITEM_ID
    entry_directories = {ITEM_ID: manual_entry}
    repository = _repository(root, entry_directories=entry_directories)

    selected = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert selected is not None and selected.resource is not None
    assert all(
        value.key.artifact_id != FIGURE_ID
        for value in repository.list_raster_artifacts(ITEM_ID)
    )

    entry_directories[ITEM_ID] = _entry(root)

    assert repository.resolve_raster_resource(ITEM_ID, selected.resource) is None
    assert any(
        value.key.artifact_id == FIGURE_ID
        for value in repository.list_raster_artifacts(ITEM_ID)
    )


def test_capture_only_lookup_filters_non_capture_from_warm_union_cache(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    capture_directory = _capture(root)
    capture_directory.mkdir(parents=True)
    (capture_directory / "orig_1.jpg").write_bytes(original)
    (capture_directory / "photo_1.jpg").write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))

    figure = _png_bytes((20, 130, 50), (41, 37))
    image_directory = _entry(root) / "ocr" / "images"
    image_directory.mkdir(parents=True)
    (image_directory / "p3-fig.png").write_bytes(figure)
    _write_layout(root, _layout(_digest(figure)))
    repository = _repository(root)

    union = repository.list_raster_artifacts(ITEM_ID)
    assert any(value.key.artifact_id == FIGURE_ID for value in union)
    monkeypatch.setattr(
        repository,
        "_project_capture",
        lambda *_args, **_kwargs: pytest.fail(
            "a valid warm union cache was unexpectedly reprojected"
        ),
    )

    assert repository.get_capture_raster_artifact(
        RasterArtifactKey(ITEM_ID, FIGURE_ID)
    ) is None
    capture = repository.get_capture_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert capture is not None
    assert capture.source.representation_id == "capture"


def test_projection_cache_coalesces_reads_and_invalidates_on_resource_change(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "original_asset-1.jpg").write_bytes(original)
    (directory / "orig_1.jpg").write_bytes(original)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))
    repository = _repository(root)
    observed = []
    original_observe = repository._observe_resource

    def record_observation(*args, **kwargs):
        observed.append(args[0])
        return original_observe(*args, **kwargs)

    monkeypatch.setattr(repository, "_observe_resource", record_observation)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _index: repository.list_raster_artifacts(ITEM_ID),
            range(8),
        ))
    assert all(len(result) == 2 for result in results)
    assert len(observed) == 2

    replacement = _jpeg_bytes((1, 2, 3), (31, 37))
    display_path.write_bytes(replacement)
    repository.list_raster_artifacts(ITEM_ID)
    assert len(observed) > 2


def test_projection_changed_during_cache_publication_is_not_cached(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    replacement = _jpeg_bytes((1, 2, 3), (31, 37))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "original_asset-1.jpg").write_bytes(original)
    (directory / "orig_1.jpg").write_bytes(original)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))
    repository = _repository(root)
    original_project = repository._project_locked
    original_path_stamp = repository._path_stamp
    project_count = 0
    display_stamp_count = 0
    changed = False

    def count_projection(*args, **kwargs):
        nonlocal project_count
        project_count += 1
        return original_project(*args, **kwargs)

    def replace_before_second_display_stamp(path):
        nonlocal changed, display_stamp_count
        if path == display_path:
            display_stamp_count += 1
        if path == display_path and display_stamp_count == 2:
            changed = True
            display_path.write_bytes(replacement)
        return original_path_stamp(path)

    monkeypatch.setattr(repository, "_project_locked", count_projection)
    monkeypatch.setattr(repository, "_path_stamp", replace_before_second_display_stamp)
    first = repository.list_raster_artifacts(ITEM_ID)
    second = repository.list_raster_artifacts(ITEM_ID)

    first_display = next(
        value for value in first
        if value.key.artifact_id == CAPTURE_DISPLAY_ID
    )
    second_display = next(
        value for value in second
        if value.key.artifact_id == CAPTURE_DISPLAY_ID
    )
    assert changed is True
    assert project_count == 2
    assert first_display.resource_state is ResourceState.AVAILABLE
    assert second_display.resource_state is ResourceState.UNAVAILABLE


def test_keyed_capture_detail_and_preview_do_not_observe_sibling_images(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    directory = _capture(root)
    directory.mkdir(parents=True)
    manifest = _photo_manifest(
        _jpeg_bytes((1, 2, 3), (17, 23)),
        _jpeg_bytes((4, 5, 6), (19, 29)),
    )
    manifest["assets"] = []
    manifest["desktop_import"]["assets"] = []
    displays: dict[int, bytes] = {}
    for index in range(1, 13):
        original = _jpeg_bytes((index, 20, 30), (17 + index, 23))
        display = _jpeg_bytes((40, index, 60), (19 + index, 29))
        displays[index] = display
        (directory / f"orig_{index}.jpg").write_bytes(original)
        (directory / f"photo_{index}.jpg").write_bytes(display)
        asset = copy.deepcopy(_photo_manifest(original, display)["assets"][0])
        asset["asset_id"] = f"asset-{index}"
        asset["capture_order"] = index
        asset["capture_file"] = f"photo_{index}.jpg"
        asset["original"]["reference"] = f"original_asset-{index}.jpg"
        asset["display"]["reference"] = f"photo_{index}.jpg"
        manifest["assets"].append(asset)
        manifest["desktop_import"]["assets"].append(
            {
                "order": index - 1,
                "asset_id": f"asset-{index}",
                "raw_ref": f"orig_{index}.jpg",
                "display_ref": f"photo_{index}.jpg",
                "source_checksum": _digest(original),
                "derivative_checksum": _digest(display),
                "transport_representation": "original",
                "recipe": "desktop_perspective_standardize_v1",
                "lifecycle": "completed",
            }
        )
    _write_photo_manifest(root, manifest)
    selected_index = 9
    namespace = _opaque_identity(
        "capture",
        CAPTURE_ID,
        f"asset-{selected_index}",
    )
    selected_id = f"{namespace}:display"
    repository = _repository(root)
    observed: list[str] = []
    observe = repository._observe_resource

    def record_observation(_item_id, _directory, reference, **kwargs):
        observed.append(reference)
        return observe(_item_id, _directory, reference, **kwargs)

    monkeypatch.setattr(repository, "_observe_resource", record_observation)
    selected = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, selected_id)
    )
    assert selected is not None
    assert observed == [f"orig_{selected_index}.jpg", f"photo_{selected_index}.jpg"]

    full = _repository(root).list_raster_artifacts(ITEM_ID)
    assert selected == next(value for value in full if value.key.artifact_id == selected_id)

    opened_images: list[str] = []
    open_regular = corrections_artifact_module._open_verified_regular

    def record_open(path, *args, **kwargs):
        if Path(path).suffix.casefold() in {".jpg", ".jpeg"}:
            opened_images.append(Path(path).name)
        return open_regular(path, *args, **kwargs)

    monkeypatch.setattr(
        corrections_artifact_module,
        "_open_verified_regular",
        record_open,
    )
    preview = repository.resolve_capture_preview(ITEM_ID, selected_id)
    assert preview is not None
    try:
        assert preview.stream.read() == displays[selected_index]
    finally:
        preview.stream.close()
    assert opened_images == [f"photo_{selected_index}.jpg"]

    monkeypatch.setattr(
        repository,
        "_project",
        lambda _item_id: pytest.fail("keyed candidate cache fell back to full projection"),
    )
    resolved = repository.resolve_raster_resource(ITEM_ID, selected.resource)
    assert resolved is not None
    try:
        assert resolved.stream.read() == displays[selected_index]
    finally:
        resolved.stream.close()

    (directory / f"photo_{selected_index}.jpg").write_bytes(
        _jpeg_bytes((200, 1, 2), (41, 43))
    )
    assert repository.resolve_raster_resource(ITEM_ID, selected.resource) is None


def test_keyed_resource_candidate_revalidates_capture_manifest(tmp_path, monkeypatch):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest_path = _write_photo_manifest(
        root,
        _photo_manifest(original, display),
    )
    repository = _repository(root)
    selected = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert selected is not None and selected.resource is not None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"] = []
    manifest["desktop_import"]["assets"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    project = repository._project
    projected = 0

    def count_projection(item_id):
        nonlocal projected
        projected += 1
        return project(item_id)

    monkeypatch.setattr(repository, "_project", count_projection)
    assert repository.resolve_raster_resource(ITEM_ID, selected.resource) is None
    assert projected == 0, "the stale keyed candidate must not be re-authorized"


def test_keyed_resource_candidate_rejects_changed_representation_revision(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "library"
    figure = _png_bytes((20, 130, 50), (41, 37))
    image_dir = _entry(root) / "ocr" / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "p3-fig.png").write_bytes(figure)
    _write_layout(root, _layout(_digest(figure)))
    revisions = {(ITEM_ID, "primary"): "rep-primary-r1"}
    repository = _repository(
        root,
        capture_ids={},
        representation_revisions=revisions,
    )
    selected = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, FIGURE_ID)
    )
    assert selected is not None and selected.resource is not None

    revisions[(ITEM_ID, "primary")] = "rep-primary-r2"
    monkeypatch.setattr(
        repository,
        "_project",
        lambda _item_id: pytest.fail(
            "a changed representation must not re-authorize a stale resource"
        ),
    )

    assert repository.resolve_raster_resource(ITEM_ID, selected.resource) is None


def test_unsafe_optional_recipe_revision_is_omitted_from_public_provenance(
    tmp_path,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = _photo_manifest(original, display)
    manifest["assets"][0]["display"]["recipe_version"] = "v😀"
    _write_photo_manifest(root, manifest)

    display_view = _repository(root).get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )

    assert display_view is not None
    assert display_view.provenance.recipe_revision == ""


def test_non_ascii_authority_revision_is_rejected_as_repository_state(tmp_path):
    root = tmp_path / "library"
    _write_layout(root, _layout("ab" * 32))
    repository = _repository(
        root,
        capture_ids={},
        representation_revisions={(ITEM_ID, "primary"): "rep😀"},
    )

    with pytest.raises(RepositoryError) as caught:
        repository.list_spatial_annotations(ITEM_ID)

    assert caught.value.code == "invalid_corrections_authority_snapshot"


def test_available_grants_require_verified_bytes_and_matching_media_type(
    tmp_path,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    corrupt = b"not actually an image\x00<script>"
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(corrupt)
    manifest = _photo_manifest(original, corrupt)
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    corrupt_view = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert corrupt_view is not None
    assert corrupt_view.resource_state is ResourceState.UNAVAILABLE
    assert corrupt_view.media_type == "image/unknown"
    assert corrupt_view.resource is None

    png = _png_bytes((90, 80, 70), (2, 2))
    display_path.write_bytes(png)
    mismatch = _photo_manifest(original, png)
    _write_photo_manifest(root, mismatch)
    mismatch_view = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert mismatch_view is not None
    assert mismatch_view.resource_state is ResourceState.UNAVAILABLE
    assert mismatch_view.media_type == "image/png"
    assert mismatch_view.resource is None


def test_raster_observation_rejects_a_name_swap_to_external_bytes(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    original = _png_bytes((20, 130, 50), (41, 37))
    external_bytes = _png_bytes((200, 10, 10), (3, 5))
    image_directory = _entry(root) / "ocr" / "images"
    image_directory.mkdir(parents=True)
    figure_path = image_directory / "p3-fig.png"
    figure_path.write_bytes(original)
    external = tmp_path / "external-figure.png"
    external.write_bytes(external_bytes)
    _write_layout(root, _layout(_digest(original)))
    _swap_to_external_hardlink(
        monkeypatch,
        target=figure_path,
        external=external,
    )

    figure = _repository(root, capture_ids={}).list_raster_artifacts(ITEM_ID)[0]

    assert figure.resource_state is ResourceState.UNAVAILABLE
    assert figure.content_sha256 == _digest(original)
    assert figure.content_sha256 != _digest(external_bytes)
    assert figure.resource is None


def test_raster_observation_rejects_an_external_ancestor_redirect(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    original = _png_bytes((20, 130, 50), (41, 37))
    external_bytes = _png_bytes((200, 10, 10), (3, 5))
    image_directory = _entry(root) / "ocr" / "images"
    image_directory.mkdir(parents=True)
    figure_path = image_directory / "p3-fig.png"
    figure_path.write_bytes(original)
    _write_layout(root, _layout(_digest(original)))
    external_entries = tmp_path / "external-entries"
    external_figure = (
        external_entries / ITEM_ID / "ocr" / "images" / "p3-fig.png"
    )
    external_figure.parent.mkdir(parents=True)
    external_figure.write_bytes(external_bytes)
    repository = _repository(root, capture_ids={})
    real_assert = repository._assert_safe_path
    swapped = False

    def swapping_assert(path, **kwargs):
        nonlocal swapped
        result = real_assert(path, **kwargs)
        if Path(path) == figure_path and not swapped:
            swapped = True
            entries = root / "entries"
            backup = root / "entries.original"
            entries.replace(backup)
            try:
                os.symlink(
                    external_entries,
                    entries,
                    target_is_directory=True,
                )
            except OSError:
                if entries.is_symlink():
                    entries.unlink()
                backup.replace(entries)
                pytest.skip("directory symlinks are unavailable")
        return result

    monkeypatch.setattr(repository, "_assert_safe_path", swapping_assert)

    figure = repository.list_raster_artifacts(ITEM_ID)[0]

    assert figure.resource_state is ResourceState.UNAVAILABLE
    assert figure.content_sha256 == _digest(original)
    assert figure.content_sha256 != _digest(external_bytes)
    assert figure.resource is None


def test_raster_grant_rejects_a_name_swap_after_projection(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    original = _png_bytes((20, 130, 50), (41, 37))
    external_bytes = _png_bytes((200, 10, 10), (3, 5))
    image_directory = _entry(root) / "ocr" / "images"
    image_directory.mkdir(parents=True)
    figure_path = image_directory / "p3-fig.png"
    figure_path.write_bytes(original)
    external = tmp_path / "external-figure.png"
    external.write_bytes(external_bytes)
    _write_layout(root, _layout(_digest(original)))
    repository = _repository(root, capture_ids={})
    figure = repository.list_raster_artifacts(ITEM_ID)[0]
    assert figure.resource is not None
    # The grant opens the target exactly once (it serves the verified
    # descriptor instead of snapshotting a copy), so the swap must be
    # caught at that single open.
    _swap_to_external_hardlink(
        monkeypatch,
        target=figure_path,
        external=external,
        on_open=1,
    )

    assert (
        repository.resolve_raster_resource(ITEM_ID, figure.resource)
        is None
    )


def test_raster_grant_rejects_an_external_ancestor_redirect(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "library"
    original = _png_bytes((20, 130, 50), (41, 37))
    external_bytes = _png_bytes((200, 10, 10), (3, 5))
    image_directory = _entry(root) / "ocr" / "images"
    image_directory.mkdir(parents=True)
    figure_path = image_directory / "p3-fig.png"
    figure_path.write_bytes(original)
    _write_layout(root, _layout(_digest(original)))
    external_entries = tmp_path / "external-entries"
    external_figure = (
        external_entries / ITEM_ID / "ocr" / "images" / "p3-fig.png"
    )
    external_figure.parent.mkdir(parents=True)
    external_figure.write_bytes(external_bytes)
    repository = _repository(root, capture_ids={})
    figure = repository.list_raster_artifacts(ITEM_ID)[0]
    assert figure.resource is not None
    real_assert = repository._assert_safe_path
    target_checks = 0

    def swapping_assert(path, **kwargs):
        nonlocal target_checks
        result = real_assert(path, **kwargs)
        if Path(path) == figure_path:
            target_checks += 1
            if target_checks == 3:
                entries = root / "entries"
                backup = root / "entries.original"
                entries.replace(backup)
                try:
                    os.symlink(
                        external_entries,
                        entries,
                        target_is_directory=True,
                    )
                except OSError:
                    if entries.is_symlink():
                        entries.unlink()
                    backup.replace(entries)
                    pytest.skip("directory symlinks are unavailable")
        return result

    monkeypatch.setattr(repository, "_assert_safe_path", swapping_assert)

    assert (
        repository.resolve_raster_resource(ITEM_ID, figure.resource)
        is None
    )


def test_capture_projection_supports_an_explicit_external_authority_root(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    capture_root = tmp_path / "phone-captures"
    directory = capture_root / CAPTURE_ID
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    (directory / "photo_assets.json").write_text(
        json.dumps(_photo_manifest(original, display)),
        encoding="utf-8",
    )
    write_set = RecoverableWriteSet(workspace)

    @contextmanager
    def lock():
        yield

    repository = FilesystemCorrectionsArtifactRepository(
        write_set,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda _item_id: CAPTURE_ID,
        entry_directory_for=lambda item_id: _entry(workspace, item_id),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _representation_id: None,
        lock_context_for=lock,
    )

    artifacts = repository.list_raster_artifacts(ITEM_ID)

    assert {value.key.artifact_id for value in artifacts} == {
        CAPTURE_DISPLAY_ID,
        CAPTURE_ORIGINAL_ID,
    }
    assert all(value.resource_state is ResourceState.AVAILABLE for value in artifacts)


def test_capture_authority_root_replacement_is_rejected(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    capture_root = tmp_path / "phone-captures"
    original_directory = capture_root / CAPTURE_ID
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    original_directory.mkdir(parents=True)
    (original_directory / "orig_1.jpg").write_bytes(original)
    (original_directory / "photo_1.jpg").write_bytes(display)
    (original_directory / "photo_assets.json").write_text(
        json.dumps(_photo_manifest(original, display)),
        encoding="utf-8",
    )

    replacement_root = tmp_path / "replacement-captures"
    replacement_directory = replacement_root / CAPTURE_ID
    external_original = _jpeg_bytes((200, 10, 10), (3, 5))
    external_display = _jpeg_bytes((10, 10, 200), (7, 11))
    replacement_directory.mkdir(parents=True)
    (replacement_directory / "orig_1.jpg").write_bytes(external_original)
    (replacement_directory / "photo_1.jpg").write_bytes(external_display)
    (replacement_directory / "photo_assets.json").write_text(
        json.dumps(_photo_manifest(external_original, external_display)),
        encoding="utf-8",
    )
    write_set = RecoverableWriteSet(workspace)

    @contextmanager
    def lock():
        yield

    repository = FilesystemCorrectionsArtifactRepository(
        write_set,
        item_exists=lambda item_id: item_id == ITEM_ID,
        capture_id_for=lambda _item_id: CAPTURE_ID,
        entry_directory_for=lambda item_id: _entry(workspace, item_id),
        capture_directory_for=lambda capture_id: capture_root / capture_id,
        capture_authority_root=capture_root,
        representation_revision_for=lambda _item_id, _representation_id: None,
        lock_context_for=lock,
    )
    real_assert = repository._assert_safe_path
    swapped = False

    def swapping_assert(path, **kwargs):
        nonlocal swapped
        authority = real_assert(path, **kwargs)
        if Path(path).name == "photo_assets.json" and not swapped:
            swapped = True
            capture_root.replace(tmp_path / "phone-captures.original")
            replacement_root.replace(capture_root)
        return authority

    monkeypatch.setattr(repository, "_assert_safe_path", swapping_assert)

    with pytest.raises(RepositoryError) as caught:
        repository.list_raster_artifacts(ITEM_ID)

    assert caught.value.code == "invalid_capture_photo_assets"
    assert _digest(external_display) not in str(caught.value)


@pytest.mark.parametrize(
    ("payload", "expected_state", "diagnostic_code"),
    (
        (None, ResourceState.MISSING, "capture_manifest_missing"),
        (
            '{"assets": [',
            ResourceState.UNAVAILABLE,
            "invalid_capture_photo_assets",
        ),
        (
            json.dumps(
                {
                    "schema": "future.capture-assets",
                    "version": 99,
                    "capture_id": CAPTURE_ID,
                    "assets": [],
                }
            ),
            ResourceState.UNAVAILABLE,
            "unsupported_capture_photo_assets",
        ),
    ),
)
def test_missing_or_malformed_capture_manifest_has_safe_placeholder(
    tmp_path,
    payload,
    expected_state,
    diagnostic_code,
):
    root = tmp_path / "library"
    directory = _capture(root)
    directory.mkdir(parents=True)
    if payload is not None:
        (directory / "photo_assets.json").write_text(
            payload,
            encoding="utf-8",
        )
    repository = _repository(root)

    artifacts = repository.list_raster_artifacts(ITEM_ID)

    assert len(artifacts) == 1
    placeholder = artifacts[0]
    assert placeholder.key.artifact_id.endswith(":display")
    assert placeholder.resource_state is expected_state
    assert placeholder.resource is None
    assert placeholder.dimensions.as_dict() == {
        "width": 1,
        "height": 1,
        "orientation": 1,
    }
    assert placeholder.extensions["capture_order"] == 0
    assert placeholder.extensions["capture_inventory"] == {
        "state": expected_state.value,
        "diagnostic_code": diagnostic_code,
    }
    assert placeholder.extensions["correction_target_authority"] == {
        "state": "missing",
    }
    assert repository.list_spatial_annotations(ITEM_ID) == ()
    assert str(directory) not in json.dumps(placeholder.as_dict())


def test_capture_resources_report_missing_private_and_stale_states(tmp_path):
    root = tmp_path / "library"
    original = _jpeg_bytes((80, 40, 20), (11, 13))
    display = _jpeg_bytes((20, 40, 80), (7, 9))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "photo_1.jpg").write_bytes(display)
    manifest = _photo_manifest(original, display)
    _write_photo_manifest(root, manifest)
    repository = _repository(root)

    artifacts = {
        value.key.artifact_id: value
        for value in repository.list_raster_artifacts(ITEM_ID)
    }
    assert artifacts[CAPTURE_ORIGINAL_ID].resource_state is ResourceState.MISSING
    assert artifacts[CAPTURE_ORIGINAL_ID].resource is None
    assert artifacts[CAPTURE_DISPLAY_ID].resource_state is ResourceState.AVAILABLE

    unsafe = copy.deepcopy(manifest)
    unsafe["desktop_import"]["assets"][0]["raw_ref"] = "../private.jpg"
    unsafe["desktop_import"]["assets"][0]["display_ref"] = "photo_1.jpg"
    unsafe["desktop_import"]["assets"][0]["derivative_checksum"] = "ab" * 32
    _write_photo_manifest(root, unsafe)
    artifacts = {
        value.key.artifact_id: value
        for value in repository.list_raster_artifacts(ITEM_ID)
    }
    assert (
        artifacts[CAPTURE_ORIGINAL_ID].resource_state
        is ResourceState.UNAVAILABLE
    )
    display_view = artifacts[CAPTURE_DISPLAY_ID]
    assert display_view.resource_state is ResourceState.UNAVAILABLE
    assert display_view.freshness.value == "stale"
    assert display_view.resource is None


def test_opaque_resolver_rejects_stale_and_cross_item_grants(tmp_path):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (8, 10))
    display = _jpeg_bytes((30, 20, 10), (9, 12))
    directory = _capture(root)
    directory.mkdir(parents=True)
    (directory / "orig_1.jpg").write_bytes(original)
    display_path = directory / "photo_1.jpg"
    display_path.write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))
    repository = _repository(root)
    view = repository.get_raster_artifact(
        RasterArtifactKey(ITEM_ID, CAPTURE_DISPLAY_ID)
    )
    assert view is not None and view.resource is not None
    resource = view.resource

    assert repository.resolve_raster_resource("book-2", resource) is None

    resolved = repository.resolve_raster_resource(ITEM_ID, resource)
    assert resolved is not None
    # The grant serves a descriptor pinned to the identity-checked inode
    # rather than a private snapshot copy, so read it before perturbing the
    # source file. Post-grant isolation from in-place writers was the copy's
    # side effect and is deliberately traded for not streaming every image
    # twice; managed stores replace files by rename, which never mutates a
    # granted descriptor's bytes.
    assert resolved.stream.read() == display
    resolved.stream.close()
    display_path.write_bytes(_jpeg_bytes((1, 2, 3), (9, 12)))
    assert repository.resolve_raster_resource(ITEM_ID, resource) is None


def _layout(figure_sha256: str) -> dict:
    return {
        "regions": {
            "primary": {
                "3": {
                    "doc": "compiled.txt",
                    "dims": {"w": 1000, "h": 2000, "dpi": 200},
                    "origin": "machine",
                    "items": [
                        {
                            "id": "r7",
                            "rid": "stable-region-7",
                            "role": "marginalia",
                            "order": 4,
                            "box": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.1},
                            "text": "A gloss ![plant](p3-fig.png)",
                            "norm": "A gloss",
                            "confidence": 0.75,
                            "future": {"provider_block": "block-9"},
                        },
                        {
                            # A read must not call ensure_rids or otherwise
                            # treat this reorderable display id as identity.
                            "id": "r8",
                            "role": "body",
                            "order": 5,
                            "box": {"x": 0.3, "y": 0.4, "w": 0.4, "h": 0.2},
                            "text": "Anonymous legacy region",
                        },
                    ],
                }
            }
        },
        "images": {
            "p3-fig.png": {
                "page": 3,
                "src_key": "primary",
                "x": 0.3,
                "y": 0.4,
                "w": 0.2,
                "h": 0.1,
                "sha256": figure_sha256,
                "caption": "A medicinal plant",
                "ext": {"future": {"palette": "green"}},
            }
        },
        "future": {"provider": "mistral"},
    }


def test_mistral_regions_and_figure_crops_project_without_rewriting(tmp_path):
    root = tmp_path / "library"
    figure = _png_bytes((20, 130, 50), (41, 37))
    image_dir = _entry(root) / "ocr" / "images"
    image_dir.mkdir(parents=True)
    figure_path = image_dir / "p3-fig.png"
    figure_path.write_bytes(figure)
    layout = _layout(_digest(figure))
    layout_path = _write_layout(root, layout)
    repository = _repository(root, capture_ids={})
    before = _snapshot(_entry(root))

    raster = repository.list_raster_artifacts(ITEM_ID)
    spatial = repository.list_spatial_annotations(ITEM_ID)

    assert [value.key.artifact_id for value in raster] == [FIGURE_ID]
    figure_view = raster[0]
    assert figure_view.kind == "extracted-figure"
    assert figure_view.media_type == "image/png"
    assert figure_view.content_sha256 == _digest(figure)
    assert figure_view.dimensions.as_dict() == {
        "width": 41,
        "height": 37,
        "orientation": 1,
    }
    assert figure_view.source.as_dict() == {
        "representation_id": "primary",
        "representation_revision": "rep-primary-r1",
        "canvas_id": "page:3",
        "canvas_revision": figure_view.source.canvas_revision,
    }
    assert figure_view.effective_caption is not None
    assert figure_view.effective_caption.text == "A medicinal plant"
    assert figure_view.effective_metadata == {
        "future": {"palette": "green"},
    }
    assert len(figure_view.metadata_assertions) == 1
    metadata = figure_view.metadata_assertions[0]
    assert metadata.name == "future"
    assert metadata.value == {"palette": "green"}
    assert metadata.origin.value == "imported"
    assert metadata.provenance.origin == "ocr"
    assert metadata.provenance.provider_id == "mistral"
    assert figure_view.extensions["extension_metadata"]["future"]["palette"] == "green"
    assert (
        figure_view.extensions["corrections_ui"]["annotation_frame"]
        == "crop"
    )
    assert figure_view.resource is not None
    resolved = repository.resolve_raster_resource(ITEM_ID, figure_view.resource)
    assert resolved is not None and resolved.stream.read() == figure
    resolved.stream.close()

    assert [value.key.annotation_id for value in spatial] == [
        FIGURE_BOX_ID,
        STABLE_REGION_ID,
    ]
    figure_box, region = spatial
    assert figure_box.effective_role == "figure"
    assert figure_box.linked_artifact_ids == (FIGURE_ID,)
    assert figure_box.selector.points[0].as_dict() == {"x": 0.3, "y": 0.4}
    assert region.effective_role == "marginalia"
    assert region.selector.points[-1].x == pytest.approx(0.1)
    assert region.selector.points[-1].y == pytest.approx(0.3)
    assert region.linked_artifact_ids == (FIGURE_ID,)
    assert region.extensions["legacy"]["future"]["provider_block"] == "block-9"
    assert repository.get_spatial_annotation(region.key) == region
    assert repository.list_spatial_annotations(
        ITEM_ID,
        representation_id="primary",
        canvas_id="page:3",
    ) == tuple(spatial)
    assert _snapshot(_entry(root)) == before

    first_ids = [value.key.annotation_id for value in spatial]
    changed = copy.deepcopy(layout)
    changed["regions"]["primary"]["3"]["items"][0]["text"] = "Corrected gloss"
    layout_path.write_text(json.dumps(changed, indent=2), encoding="utf-8")
    after = repository.list_spatial_annotations(ITEM_ID)
    assert [value.key.annotation_id for value in after] == first_ids
    assert next(
        value for value in after if value.key == SpatialAnnotationKey(
            ITEM_ID,
            STABLE_REGION_ID,
        )
    ).revision != region.revision
    persisted = json.loads(layout_path.read_text(encoding="utf-8"))
    assert "rid" not in persisted["regions"]["primary"]["3"]["items"][1]


def test_mistral_metadata_assertions_are_bounded_with_full_extensions_retained(
    tmp_path,
):
    root = tmp_path / "library"
    figure = _png_bytes((20, 130, 50), (41, 37))
    image_dir = _entry(root) / "ocr" / "images"
    image_dir.mkdir(parents=True)
    (image_dir / "p3-fig.png").write_bytes(figure)
    layout = _layout(_digest(figure))
    extension_metadata = {
        f"field_{index:03d}": index for index in range(129)
    }
    layout["images"]["p3-fig.png"]["ext"] = extension_metadata
    _write_layout(root, layout)

    raster = _repository(root, capture_ids={}).list_raster_artifacts(ITEM_ID)

    assert len(raster) == 1
    figure_view = raster[0]
    assert len(figure_view.metadata_assertions) == (
        MAX_METADATA_ASSERTIONS // 2
    )
    assert [
        assertion.name for assertion in figure_view.metadata_assertions
    ] == sorted(extension_metadata)[: MAX_METADATA_ASSERTIONS // 2]
    assert figure_view.extensions["extension_metadata"] == extension_metadata


def test_region_links_are_bounded_in_source_order(tmp_path):
    root = tmp_path / "library"
    layout = _layout("ab" * 32)
    template = layout["images"].pop("p3-fig.png")
    names = [f"figure-{index:02d}.png" for index in range(65)]
    for name in names:
        layout["images"][name] = copy.deepcopy(template)
    layout["regions"]["primary"]["3"]["items"][0]["text"] = " ".join(
        f"![figure]({name})" for name in names
    )
    _write_layout(root, layout)
    repository = _repository(root, capture_ids={})

    annotations = repository.list_spatial_annotations(ITEM_ID)
    region = next(
        value
        for value in annotations
        if value.key.annotation_id == STABLE_REGION_ID
    )

    assert region.linked_artifact_ids == tuple(
        _opaque_identity("figure", name) for name in names[:64]
    )
    assert _opaque_identity("figure", names[64]) not in region.linked_artifact_ids


def test_unicode_figure_names_keep_private_paths_and_portable_identities(
    tmp_path,
):
    root = tmp_path / "library"
    figure = _png_bytes((20, 130, 50), (41, 37))
    name = "p3-flör.png"
    image_dir = _entry(root) / "ocr" / "images"
    image_dir.mkdir(parents=True)
    (image_dir / name).write_bytes(figure)
    layout = _layout(_digest(figure))
    info = layout["images"].pop("p3-fig.png")
    layout["images"][name] = info
    layout["regions"]["primary"]["3"]["items"][0]["text"] = (
        f"A gloss ![plant]({name})"
    )
    _write_layout(root, layout)
    repository = _repository(root, capture_ids={})

    raster = repository.list_raster_artifacts(ITEM_ID)
    spatial = repository.list_spatial_annotations(ITEM_ID)

    artifact_id = _opaque_identity("figure", name)
    figure_box_id = _opaque_identity("figure-box", "primary", 3, name)
    assert [value.key.artifact_id for value in raster] == [artifact_id]
    assert raster[0].label == name
    assert str(image_dir) not in json.dumps(raster[0].as_dict())
    assert raster[0].resource is not None
    resolved = repository.resolve_raster_resource(ITEM_ID, raster[0].resource)
    assert resolved is not None
    assert resolved.stream.read() == figure
    resolved.stream.close()
    assert {
        value.key.annotation_id
        for value in spatial
    } == {
        figure_box_id,
        STABLE_REGION_ID,
    }
    region = next(
        value
        for value in spatial
        if value.key.annotation_id == STABLE_REGION_ID
    )
    assert region.linked_artifact_ids == (artifact_id,)


def test_opaque_figure_ids_do_not_alias_legacy_hash_shaped_names(tmp_path):
    root = tmp_path / "library"
    unicode_name = "flör.png"
    legacy_hash_name = (
        "f-" + hashlib.sha256(unicode_name.encode("utf-8")).hexdigest()[:40]
    )
    layout = _layout("ab" * 32)
    template = layout["images"].pop("p3-fig.png")
    layout["images"][unicode_name] = copy.deepcopy(template)
    layout["images"][legacy_hash_name] = copy.deepcopy(template)
    layout["regions"]["primary"]["3"]["items"][0]["text"] = (
        f"![first]({unicode_name}) ![second]({legacy_hash_name})"
    )
    _write_layout(root, layout)

    repository = _repository(root, capture_ids={})
    artifacts = repository.list_raster_artifacts(ITEM_ID)
    region = next(
        value
        for value in repository.list_spatial_annotations(ITEM_ID)
        if value.key.annotation_id == STABLE_REGION_ID
    )

    expected = {
        _opaque_identity("figure", unicode_name),
        _opaque_identity("figure", legacy_hash_name),
    }
    assert {value.key.artifact_id for value in artifacts} == expected
    assert len({value.key.artifact_id.casefold() for value in artifacts}) == 2
    assert set(region.linked_artifact_ids) == expected


def test_case_distinct_legacy_names_have_distinct_public_identities(tmp_path):
    root = tmp_path / "library"
    layout = _layout("ab" * 32)
    figure_template = layout["images"].pop("p3-fig.png")
    layout["images"]["Case.png"] = copy.deepcopy(figure_template)
    layout["images"]["case.png"] = copy.deepcopy(figure_template)
    region_template = layout["regions"]["primary"]["3"]["items"][0]
    upper = copy.deepcopy(region_template)
    upper["rid"] = "RID"
    upper["text"] = "Upper"
    lower = copy.deepcopy(region_template)
    lower["rid"] = "rid"
    lower["text"] = "Lower"
    layout["regions"]["primary"]["3"]["items"] = [upper, lower]
    _write_layout(root, layout)

    repository = _repository(root, capture_ids={})
    artifact_ids = [
        value.key.artifact_id
        for value in repository.list_raster_artifacts(ITEM_ID)
    ]
    annotation_ids = [
        value.key.annotation_id
        for value in repository.list_spatial_annotations(ITEM_ID)
    ]

    assert len(artifact_ids) == 2
    assert len({value.casefold() for value in artifact_ids}) == 2
    assert _opaque_identity("region", "RID") in annotation_ids
    assert _opaque_identity("region", "rid") in annotation_ids
    assert len({value.casefold() for value in annotation_ids}) == 4


def test_valid_legacy_extensions_are_quarantined_without_breaking_reads(
    tmp_path,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((120, 20, 30), (17, 23))
    display = _jpeg_bytes((20, 120, 30), (19, 29))
    capture_directory = _capture(root)
    capture_directory.mkdir(parents=True)
    (capture_directory / "orig_1.jpg").write_bytes(original)
    (capture_directory / "photo_1.jpg").write_bytes(display)
    large_value = "x" * (40 * 1024)
    manifest = _photo_manifest(original, display)
    manifest["assets"][0]["future"] = {
        "url": "https://private.invalid/capture",
        "large": large_value,
    }
    manifest["assets"][0]["role"]["algorithm"] = "android\u0000model"
    manifest_path = _write_photo_manifest(root, manifest)

    figure = _png_bytes((20, 130, 50), (41, 37))
    image_directory = _entry(root) / "ocr" / "images"
    image_directory.mkdir(parents=True)
    (image_directory / "p3-fig.png").write_bytes(figure)
    layout = _layout(_digest(figure))
    layout["images"]["p3-fig.png"]["ext"] = {
        "url": "https://private.invalid/figure",
        "large": large_value,
        "caption": "good\u0000bad",
        "escaped_surrogate": "legacy\ud800value",
    }
    layout["images"]["p3-fig.png"].pop("caption")
    layout["regions"]["primary"]["3"]["items"][0]["future"] = {
        "url": "https://private.invalid/region",
        "large": large_value,
    }
    layout["regions"]["primary"]["3"]["items"][0].update(
        {
            "text": "gloss\u0000unsafe",
            "norm": "gloss\u0000normalized",
            "caption": "cap\u0000tion",
        }
    )
    layout_path = _write_layout(root, layout)
    before = _snapshot(capture_directory, _entry(root))
    repository = _repository(root)

    raster = repository.list_raster_artifacts(ITEM_ID)
    spatial = repository.list_spatial_annotations(ITEM_ID)

    assert len(raster) == 3
    for artifact in raster:
        quarantine = artifact.extensions["quarantine"]
        assert quarantine["reason"] == "legacy-extension-not-public"
        assert len(quarantine["sha256"]) == 64
        assert quarantine["encoded_bytes"] > 32 * 1024
        assert "url" not in artifact.extensions
    display_view = next(
        artifact
        for artifact in raster
        if artifact.key.artifact_id == CAPTURE_DISPLAY_ID
    )
    assert (
        display_view.category_assignments[0].provenance.model
        == "androidmodel"
    )
    figure_view = next(
        artifact
        for artifact in raster
        if artifact.key.artifact_id == FIGURE_ID
    )
    assert figure_view.effective_caption is not None
    assert figure_view.effective_caption.text == "goodbad"
    region = next(
        value
        for value in spatial
        if value.key.annotation_id == STABLE_REGION_ID
    )
    assert (
        region.extensions["quarantine"]["reason"]
        == "legacy-extension-not-public"
    )
    assert region.label == "glossunsafe"
    assert region.caption_assertions
    assert region.caption_assertions[0].text == "caption"
    assert _snapshot(capture_directory, _entry(root)) == before
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert json.loads(layout_path.read_text(encoding="utf-8")) == layout


def test_missing_figure_keeps_manifest_identity_and_safe_state(tmp_path):
    root = tmp_path / "library"
    expected_sha256 = "cd" * 32
    _write_layout(root, _layout(expected_sha256))
    repository = _repository(root, capture_ids={})

    artifacts = repository.list_raster_artifacts(ITEM_ID)

    assert len(artifacts) == 1
    figure = artifacts[0]
    assert figure.key.artifact_id == FIGURE_ID
    assert figure.resource_state is ResourceState.MISSING
    assert figure.resource is None
    # The current layout retains the page dimensions and normalized crop, so
    # the absent crop still has a truthful expected pixel extent.
    assert figure.dimensions.as_dict() == {
        "width": 200,
        "height": 200,
        "orientation": 1,
    }
    assert figure.content_sha256 == expected_sha256


def test_invalid_mistral_layout_isolated_from_healthy_capture_artifacts(
    tmp_path,
):
    root = tmp_path / "library"
    original = _jpeg_bytes((10, 20, 30), (17, 23))
    display = _jpeg_bytes((40, 50, 60), (19, 29))
    capture_directory = _capture(root)
    capture_directory.mkdir(parents=True)
    (capture_directory / "orig_1.jpg").write_bytes(original)
    (capture_directory / "photo_1.jpg").write_bytes(display)
    _write_photo_manifest(root, _photo_manifest(original, display))
    layout_path = _entry(root) / "ocr" / "layout.json"
    layout_path.parent.mkdir(parents=True)
    layout_path.write_text(
        json.dumps({"regions": [], "images": {}}),
        encoding="utf-8",
    )
    repository = _repository(root)

    artifacts = repository.list_raster_artifacts(ITEM_ID)

    healthy = [
        value
        for value in artifacts
        if value.key.artifact_id in {CAPTURE_DISPLAY_ID, CAPTURE_ORIGINAL_ID}
    ]
    assert len(healthy) == 2
    assert all(
        value.resource_state is ResourceState.AVAILABLE
        for value in healthy
    )
    diagnostic = next(
        value
        for value in artifacts
        if value.kind == "artifact-diagnostic"
    )
    assert diagnostic.resource_state is ResourceState.UNAVAILABLE
    assert diagnostic.resource is None
    assert diagnostic.label == "Mistral artifacts unavailable"
    assert diagnostic.extensions["artifact_diagnostics"] == (
        {
            "scope": "mistral_layout",
            "code": "invalid_mistral_layout",
            "state": "unavailable",
        },
    )
    assert str(layout_path) not in json.dumps(diagnostic.as_dict())


def test_region_rid_survives_canonical_save_reorder_and_page_move(
    tmp_path,
):
    root = tmp_path / "library"
    layout = _layout("ab" * 32)
    page = layout["regions"]["primary"]["3"]
    stable = copy.deepcopy(page["items"][0])
    other = {
        "id": "old-display-id",
        "rid": "other-stable-region",
        "role": "body",
        "order": 0,
        "box": {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2},
        "text": "Earlier reading-order region",
    }
    stable["order"] = 10
    page["items"] = [stable]
    path = _write_layout(root, layout)
    repository = _repository(root, capture_ids={})

    before = repository.list_spatial_annotations(ITEM_ID)
    stable_before = next(
        value
        for value in before
        if value.key.annotation_id
        == STABLE_REGION_ID
    )

    page["items"] = libformat.sanitize_page_items(
        [stable, other],
        src_type="human",
    )
    saved_stable = next(
        value
        for value in page["items"]
        if value["rid"] == "stable-region-7"
    )
    assert saved_stable["id"] == "r1"
    page["items"] = [
        value
        for value in page["items"]
        if value["rid"] != "stable-region-7"
    ]
    layout["regions"]["primary"]["4"] = {
        **page,
        "items": [saved_stable],
    }
    path.write_text(json.dumps(layout, indent=2), encoding="utf-8")

    after = repository.list_spatial_annotations(ITEM_ID)
    stable_after = next(
        value
        for value in after
        if value.key.annotation_id
        == STABLE_REGION_ID
    )
    assert stable_after.key == stable_before.key
    assert stable_after.selector.points == stable_before.selector.points
    assert stable_before.source.canvas_id == "page:3"
    assert stable_after.source.canvas_id == "page:4"


def test_pixel_legacy_rectangle_is_normalized_and_identity_is_persisted(tmp_path):
    root = tmp_path / "library"
    layout = {
        "regions": {
            "primary": {
                "2": {
                    "doc": "compiled.txt",
                    "dims": {"w": 1000, "h": 2000},
                    "origin": "machine",
                    "items": [
                        {
                            "id": "r0",
                            "rid": "pixel-region",
                            "role": "figure",
                            "box": {"x": 100, "y": 400, "w": 300, "h": 500},
                            "text": "",
                        }
                    ],
                }
            }
        },
        "images": {},
    }
    _write_layout(root, layout)
    repository = _repository(root, capture_ids={})

    annotation = repository.list_spatial_annotations(ITEM_ID)[0]

    assert annotation.key.annotation_id == PIXEL_REGION_ID
    assert [point.as_dict() for point in annotation.selector.points] == [
        {"x": 0.1, "y": 0.2},
        {"x": 0.4, "y": 0.2},
        {"x": 0.4, "y": 0.45},
        {"x": 0.1, "y": 0.45},
    ]


@pytest.mark.parametrize(
    "method",
    (
        "list_raster_artifacts",
        "list_spatial_annotations",
    ),
)
def test_missing_item_is_an_explicit_engine_error(tmp_path, method):
    root = tmp_path / "library"
    repository = _repository(root)

    with pytest.raises(NotFoundError) as caught:
        getattr(repository, method)("missing")
    assert getattr(caught.value, "code", "") == "item_not_found"


def test_traversal_in_a_store_path_is_rejected_by_name(tmp_path):
    """Containment is proved lexically, so ".." must be refused outright.

    The authority walk used to resolve every path to catch traversal, which
    meant a full realpath of the chain on each of thousands of calls per
    Corrections index. Rejecting the segment is both cheaper and stricter, so
    it has to be exercised directly.
    """

    root = tmp_path / "library"
    _write_layout(root, _layout("ab" * 32))
    repository = _repository(root, capture_ids={})
    entries = root / "entries"
    secret = tmp_path / "outside.json"
    secret.write_text(json.dumps({"leaked": True}), encoding="utf-8")

    escaping = entries / ITEM_ID / ".." / ".." / ".." / "outside.json"
    assert escaping.exists(), "the traversal target must really be reachable"

    with pytest.raises(RepositoryError) as caught:
        repository._assert_safe_path(
            escaping,
            item_id=ITEM_ID,
            section="entry",
        )

    assert caught.value.code == "unsafe_corrections_store_path"

    # pathlib drops "." while building the path, so it never reaches the walk
    # and cannot be asserted here; ".." survives precisely because collapsing
    # it would require resolving symlinks.
    assert ".." in (entries / ITEM_ID / ".." / "x").parts
    assert "." not in (entries / ITEM_ID / "." / "x").parts

    # An ordinary path under the root still yields an authority snapshot.
    layout = entries / ITEM_ID / "ocr" / "layout.json"
    authority = repository._assert_safe_path(
        layout,
        item_id=ITEM_ID,
        section="entry",
    )
    assert layout.is_relative_to(authority.root)
    assert [snapshot.path for snapshot in authority.directories] == [
        entries,
        entries / ITEM_ID,
        entries / ITEM_ID / "ocr",
    ]


def _canonical_manifest_bytes(manifest) -> bytes:
    return json.dumps(
        manifest,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _capture_with_manifest(root):
    directory = _capture(root)
    directory.mkdir(parents=True)
    original = _jpeg_bytes((10, 20, 30), (2, 2))
    display = _jpeg_bytes((40, 50, 60), (2, 2))
    (directory / "orig_1.jpg").write_bytes(original)
    (directory / "photo_1.jpg").write_bytes(display)
    (directory / "original_asset-1.jpg").write_bytes(original)
    manifest = _photo_manifest(original, display)
    (directory / "photo_assets.json").write_bytes(_canonical_manifest_bytes(manifest))
    return directory, manifest


def test_capture_manifest_rewritten_in_place_is_not_served_stale(
    tmp_path,
):
    """`_stable_stat_identity` must not be the only thing standing between a
    changed manifest and a cached hint.

    On Windows ``st_ctime`` is CREATION time, so the identity tuple reduces to
    (dev, ino, mode, nlink, size, mtime). An in-place rewrite keeps the inode,
    'cover' and 'spine' are both five bytes so the canonical payload keeps its
    size, and mtime is trivially held (os.utime here; a same-tick write, a
    coarse-granularity volume such as exFAT/SMB, or any mtime-preserving
    restore does it without cooperation).
    """

    directory, manifest = _capture_with_manifest(tmp_path)
    path = directory / "photo_assets.json"
    repository = _repository(tmp_path)

    before = repository.list_capture_index_hints(ITEM_ID)
    assert [hint["effective_category"] for hint in before] == ["cover"]

    stat_before = os.lstat(path)
    changed = json.loads(json.dumps(manifest))
    changed["assets"][0]["role"]["manual_override"] = "spine"
    payload = _canonical_manifest_bytes(changed)
    assert len(payload) == stat_before.st_size

    with open(path, "r+b") as handle:      # in place: inode preserved
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

    after = repository.list_capture_index_hints(ITEM_ID)
    assert [hint["effective_category"] for hint in after] == ["spine"]
    assert after != before


def test_capture_authority_is_revalidated_for_every_asset_in_a_burst(
    tmp_path,
):
    """Pinning one directory must not stop ancestors being rechecked.

    A capture directory swapped for a redirect after any pin is taken must not
    let an asset outside the authority be observed as AVAILABLE. Uses a
    junction on Windows and a symlink elsewhere: both are what
    `_is_redirecting_path` exists to catch.
    """

    import subprocess

    from librarytool.adapters.filesystem import (
        corrections_artifact_repository as module,
    )

    def _redirect(link, target):
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            os.symlink(str(target), str(link), target_is_directory=True)

    directory, _manifest = _capture_with_manifest(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("orig_1.jpg", "photo_1.jpg", "original_asset-1.jpg"):
        (outside / name).write_bytes(_jpeg_bytes((1, 2, 3), (64, 64)))

    repository = _repository(tmp_path)
    repository.list_capture_index_hints(ITEM_ID)

    original = (
        module.FilesystemCorrectionsArtifactRepository._capture_manifest_records
    )
    swapped = {"done": False}

    def hooked(self, item_id, capture_id, manifest):
        result = original(self, item_id, capture_id, manifest)
        if not swapped["done"]:
            swapped["done"] = True
            os.rename(directory, directory.with_name(directory.name + ".stash"))
            _redirect(directory, outside)
        return result

    module.FilesystemCorrectionsArtifactRepository._capture_manifest_records = (
        hooked
    )
    # The hint cache would satisfy this read without re-entering the build,
    # and the attack in `hooked` fires mid-build; clear it so the redirect
    # scenario actually executes.
    repository._capture_hint_cache.clear()
    try:
        hints = repository.list_capture_index_hints(ITEM_ID)
    finally:
        module.FilesystemCorrectionsArtifactRepository._capture_manifest_records = (
            original
        )
    assert all(hint["resource_state"] != "available" for hint in hints)


def test_capture_hint_cache_serves_repeat_reads_without_rebuilding(tmp_path):
    from librarytool.adapters.filesystem import (
        corrections_artifact_repository as module,
    )

    directory, _manifest = _capture_with_manifest(tmp_path)
    repository = _repository(tmp_path)
    builds = {"count": 0}
    original = (
        module.FilesystemCorrectionsArtifactRepository._capture_index_hints
    )

    def counting(self, *args, **kwargs):
        builds["count"] += 1
        return original(self, *args, **kwargs)

    module.FilesystemCorrectionsArtifactRepository._capture_index_hints = (
        counting
    )
    try:
        first = repository.list_capture_index_hints(ITEM_ID)
        second = repository.list_capture_index_hints(ITEM_ID)
    finally:
        module.FilesystemCorrectionsArtifactRepository._capture_index_hints = (
            original
        )
    assert builds["count"] == 1, "the unchanged capture is served from cache"
    assert second == first

    # A rendition content change (new inode via replace) must invalidate even
    # though the manifest is untouched.
    display = directory / "photo_1.jpg"
    replacement = directory / "photo_1.jpg.new"
    replacement.write_bytes(_jpeg_bytes((70, 80, 90), (2, 2)))
    os.replace(replacement, display)
    module.FilesystemCorrectionsArtifactRepository._capture_index_hints = (
        counting
    )
    try:
        third = repository.list_capture_index_hints(ITEM_ID)
    finally:
        module.FilesystemCorrectionsArtifactRepository._capture_index_hints = (
            original
        )
    assert builds["count"] == 2, "a touched rendition forces a rebuild"
    assert isinstance(third, tuple)


def test_capture_hints_many_matches_single_reads_and_shares_one_lease(
    tmp_path,
):
    _capture_with_manifest(tmp_path)
    repository = _repository(tmp_path)

    single = repository.list_capture_index_hints(ITEM_ID)
    repository._capture_hint_cache.clear()
    many = repository.list_capture_index_hints_many([ITEM_ID])
    assert set(many) == {ITEM_ID}
    assert many[ITEM_ID] == single

    with pytest.raises(NotFoundError):
        repository.list_capture_index_hints_many([ITEM_ID, "book-absent"])
