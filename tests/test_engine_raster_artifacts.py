from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from librarytool.engine.errors import ValidationError
from librarytool.engine.raster_artifacts import (
    ArtifactFreshness,
    ArtifactProvenance,
    CategoryAssignment,
    CaptionAssertion,
    CORRECTIONS_WORKBENCH_ID,
    RASTER_ARTIFACTS_READ_CAPABILITY,
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterDimensions,
    RasterLineageRef,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)


SHA = "ab" * 32


def _artifact(**changes) -> RasterArtifactView:
    values = {
        "key": RasterArtifactKey("book-1", "image-1"),
        "revision": "artifact-r1",
        "kind": "captured-image",
        "media_type": "image/jpeg",
        "content_sha256": SHA,
        "dimensions": RasterDimensions(1200, 1800),
        "source": RasterSourceRef("capture", "rep-r1", "photo-1", "canvas-r1"),
        "resource_state": ResourceState.AVAILABLE,
        "resource": RasterResourceRef("resource:image-1", "bytes-r1"),
    }
    values.update(changes)
    return RasterArtifactView(**values)


def test_contract_names_are_reusable_and_workbench_id_remains_logical():
    assert RASTER_ARTIFACTS_READ_CAPABILITY.id == "library.raster-artifacts.read"
    assert RASTER_ARTIFACTS_READ_CAPABILITY.version == 1
    assert CORRECTIONS_WORKBENCH_ID == "corrections"


def test_raster_view_exposes_revisioned_source_lineage_and_assertions():
    machine = CategoryAssignment(
        "cover",
        "suggested",
        "category-machine-r1",
        confidence=0.8,
        provenance=ArtifactProvenance(
            origin="machine",
            provider_id="classifier",
            model="cover-v2",
        ),
    )
    inherited = CategoryAssignment(
        "title_page",
        "inherited",
        "category-inherited-r1",
        inherited_from_artifact_id="source-photo",
    )
    manual = CategoryAssignment("content_specimen", "manual", "category-human-r1")
    machine_caption = CaptionAssertion(
        "A machine caption",
        "machine",
        "caption-machine-r1",
        source_annotation_id="caption-region-1",
    )
    manual_caption = CaptionAssertion(
        "The corrected caption",
        "manual",
        "caption-human-r1",
        language="en",
    )
    extensions = {"future": {"nested": [1, True, None]}}
    view = _artifact(
        kind="processed-image",
        freshness=ArtifactFreshness.STALE,
        lineage=(
            RasterLineageRef("source-photo", "source-r3", "derived_from"),
            RasterLineageRef("earlier-edit", "edit-r2", "rework_of"),
        ),
        category_assignments=(machine, inherited, manual),
        caption_assertions=(machine_caption, manual_caption),
        provenance=ArtifactProvenance(
            origin="transform",
            provider_id="desktop",
            recipe_revision="recipe-r4",
            operation_id="operation-8",
            extensions={"brightness": 47},
        ),
        extensions=extensions,
    )
    extensions["future"]["nested"].append("mutated")

    assert view.effective_category == "content_specimen"
    assert view.effective_caption is manual_caption
    assert view.source.canvas_revision == "canvas-r1"
    assert view.freshness is ArtifactFreshness.STALE
    assert [value.relation for value in view.lineage] == [
        "derived_from",
        "rework_of",
    ]
    public = view.as_dict()
    assert public["extensions"] == {"future": {"nested": [1, True, None]}}
    assert public["effective_caption"]["text"] == "The corrected caption"
    assert public["resource"] == {
        "id": "resource:image-1",
        "revision": "bytes-r1",
        "variant": "display",
    }
    public["extensions"]["future"]["nested"].append("public mutation")
    assert view.as_dict()["extensions"] == {
        "future": {"nested": [1, True, None]}
    }
    json.dumps(view.as_dict(), allow_nan=False)
    with pytest.raises(FrozenInstanceError):
        view.revision = "artifact-r2"


@pytest.mark.parametrize(
    "resource_id",
    (
        "C:\\private\\photo.jpg",
        "C:private-photo.jpg",
        "../private/photo.jpg",
        "file:private-photo",
        "https:private-photo",
        "resource with spaces",
    ),
)
def test_resource_references_are_opaque_and_never_paths_or_urls(resource_id):
    with pytest.raises(ValidationError) as caught:
        RasterResourceRef(resource_id, "bytes-r1")
    assert caught.value.code in {
        "invalid_artifact_identity",
        "unsafe_artifact_resource_ref",
    }


@pytest.mark.parametrize("state", (ResourceState.MISSING, ResourceState.UNAVAILABLE))
def test_non_available_resources_have_explicit_state_and_no_reference(state):
    view = _artifact(resource_state=state, resource=None)
    assert view.as_dict()["resource_state"] == state.value
    assert view.as_dict()["resource"] is None


def test_resource_state_and_reference_must_agree():
    with pytest.raises(ValidationError) as unavailable:
        _artifact(resource_state="unavailable")
    assert unavailable.value.code == "invalid_artifact_resource_state"

    with pytest.raises(ValidationError) as available:
        _artifact(resource_state="available", resource=None)
    assert available.value.code == "invalid_artifact_resource_state"


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"revision": "bad revision"}, "invalid_artifact_revision"),
        ({"revision": "artifact-😀"}, "invalid_artifact_revision"),
        ({"revision": "artifact-\x00r1"}, "invalid_artifact_revision"),
        ({"media_type": "application/octet-stream"}, "invalid_raster_media_type"),
        ({"media_type": "image/svg+xml"}, "invalid_raster_media_type"),
        ({"content_sha256": "not-a-hash"}, "invalid_artifact_checksum"),
        ({"dimensions": RasterDimensions(1, 1, 8), "kind": "bad kind"}, "invalid_artifact_identity"),
    ),
)
def test_raster_contract_rejects_nonportable_public_state(changes, code):
    with pytest.raises(ValidationError) as caught:
        _artifact(**changes)
    assert caught.value.code == code


def test_raster_key_rejects_storage_shaped_identity():
    with pytest.raises(ValidationError) as caught:
        RasterArtifactKey("book-1", "../image")
    assert caught.value.code == "invalid_artifact_identity"


@pytest.mark.parametrize(
    "private_key",
    (
        "local-path",
        "localPath",
        "downloadUrl",
        "downloadURL",
        "resourceRef",
        "storageKey",
        "source_file",
    ),
)
def test_unknown_extensions_are_bounded_frozen_and_cannot_smuggle_storage_refs(
    private_key,
):
    with pytest.raises(ValidationError) as private:
        _artifact(extensions={"future": {private_key: "secret.jpg"}})
    assert private.value.code == "private_artifact_extension"

    with pytest.raises(ValidationError) as large:
        _artifact(extensions={"future": "x" * (32 * 1024)})
    assert large.value.code == "invalid_artifact_extensions"


def test_category_assignments_use_canonical_vocabulary_and_explicit_inheritance():
    with pytest.raises(ValidationError) as category:
        CategoryAssignment("sample", "manual", "category-r1")
    assert category.value.code == "invalid_artifact_assignment"

    with pytest.raises(ValidationError) as inherited:
        CategoryAssignment("cover", "inherited", "category-r1")
    assert inherited.value.code == "invalid_artifact_identity"

    view = _artifact(
        category_assignments=(
            CategoryAssignment("spine", "suggested", "suggested-r1"),
        )
    )
    assert view.effective_category == "spine"


def test_projector_port_is_framework_neutral_and_runtime_checkable():
    class Projector:
        def list_raster_artifacts(self, item_id):
            return (_artifact(),) if item_id == "book-1" else ()

        def get_raster_artifact(self, key):
            return _artifact() if key == RasterArtifactKey("book-1", "image-1") else None

    projector = Projector()
    assert isinstance(projector, RasterArtifactProjectorPort)
    assert projector.list_raster_artifacts("book-1")[0].key.artifact_id == "image-1"
