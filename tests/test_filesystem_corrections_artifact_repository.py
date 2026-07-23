from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

import libformat
from librarytool.adapters.filesystem.corrections_artifact_repository import (
    FilesystemCorrectionsArtifactRepository,
    FilesystemRasterResourceResolverPort,
    _windows_path_is_below,
)
from librarytool.adapters.filesystem.recoverable_write_set import RecoverableWriteSet
from librarytool.engine.errors import NotFoundError, RepositoryError
from librarytool.engine.raster_artifacts import (
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
        entry_directory_for=lambda item_id: _entry(root, item_id),
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


def test_partial_legacy_capture_keeps_each_representable_rendition(tmp_path):
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

    assert [
        value.key.artifact_id
        for value in repository.list_raster_artifacts(ITEM_ID)
    ] == [CAPTURE_DISPLAY_ID]

    display_path.unlink()
    (directory / "original_asset-1.jpg").write_bytes(original)
    assert [
        value.key.artifact_id
        for value in repository.list_raster_artifacts(ITEM_ID)
    ] == [CAPTURE_ORIGINAL_ID]


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
    _swap_to_external_hardlink(
        monkeypatch,
        target=figure_path,
        external=external,
        on_open=2,
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
    display_path.write_bytes(_jpeg_bytes((1, 2, 3), (9, 12)))
    assert resolved.stream.read() == display
    resolved.stream.close()
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
