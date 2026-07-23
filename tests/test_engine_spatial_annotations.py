from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from librarytool.engine.errors import ValidationError
from librarytool.engine.raster_artifacts import ArtifactProvenance, CaptionAssertion
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    SPATIAL_ANNOTATIONS_READ_CAPABILITY,
    SpatialAnnotationKey,
    SpatialAnnotationProjectorPort,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
    canonical_spatial_role,
    normalized_polygon_from_legacy_rectangle,
    project_legacy_rectangle_annotation,
)


def _source() -> SpatialSourceRef:
    return SpatialSourceRef("scan", "rep-r3", "page-1", "canvas-r5")


def _selector() -> NormalizedPolygonSelector:
    return NormalizedPolygonSelector(
        "canvas-normalized",
        "canvas-r5",
        (
            NormalizedPoint(0.1, 0.2),
            NormalizedPoint(0.6, 0.2),
            NormalizedPoint(0.6, 0.7),
            NormalizedPoint(0.1, 0.7),
        ),
    )


def test_spatial_capability_uses_the_reusable_library_namespace():
    assert SPATIAL_ANNOTATIONS_READ_CAPABILITY.id == (
        "library.spatial-annotations.read"
    )
    assert SPATIAL_ANNOTATIONS_READ_CAPABILITY.version == 1


def test_polygon_selector_is_normalized_named_and_revision_pinned():
    selector = _selector()
    assert selector.as_dict() == {
        "type": "polygon",
        "coordinate_space": "canvas-normalized",
        "coordinate_space_revision": "canvas-r5",
        "points": [
            {"x": 0.1, "y": 0.2},
            {"x": 0.6, "y": 0.2},
            {"x": 0.6, "y": 0.7},
            {"x": 0.1, "y": 0.7},
        ],
    }
    with pytest.raises(FrozenInstanceError):
        selector.points = ()


def test_polygon_selector_accepts_a_simple_concave_polygon():
    selector = NormalizedPolygonSelector(
        "canvas-normalized",
        "canvas-r1",
        (
            NormalizedPoint(0, 0),
            NormalizedPoint(1, 0),
            NormalizedPoint(1, 1),
            NormalizedPoint(0.5, 0.5),
            NormalizedPoint(0, 1),
        ),
    )

    assert len(selector.points) == 5


@pytest.mark.parametrize(
    "points",
    (
        (NormalizedPoint(0, 0), NormalizedPoint(1, 0)),
        (
            NormalizedPoint(0, 0),
            NormalizedPoint(1, 0),
            NormalizedPoint(0, 0),
        ),
        (
            NormalizedPoint(0, 0),
            NormalizedPoint(0.5, 0.5),
            NormalizedPoint(1, 1),
        ),
    ),
)
def test_polygon_selector_rejects_short_duplicate_and_zero_area_shapes(points):
    with pytest.raises(ValidationError) as caught:
        NormalizedPolygonSelector("canvas-normalized", "canvas-r1", points)
    assert caught.value.code == "invalid_polygon_selector"


@pytest.mark.parametrize(
    "points",
    (
        # This crossing polygon has non-zero signed area, so an area-only
        # validator cannot detect it.
        (
            NormalizedPoint(0, 0),
            NormalizedPoint(1, 0),
            NormalizedPoint(0, 1),
            NormalizedPoint(1, 1),
            NormalizedPoint(0, 0.2),
        ),
        # Non-adjacent bottom edges overlap despite all vertices being unique.
        (
            NormalizedPoint(0, 0),
            NormalizedPoint(1, 0),
            NormalizedPoint(1, 1),
            NormalizedPoint(0, 1),
            NormalizedPoint(0.25, 0),
            NormalizedPoint(0.75, 0),
            NormalizedPoint(0.5, 0.5),
        ),
        # Adjacent edges may share their endpoint, but may not backtrack over
        # one another.
        (
            NormalizedPoint(0, 0),
            NormalizedPoint(1, 0),
            NormalizedPoint(0.5, 0),
            NormalizedPoint(1, 1),
            NormalizedPoint(0, 1),
        ),
    ),
)
def test_polygon_selector_rejects_crossing_and_overlapping_edges(points):
    with pytest.raises(ValidationError) as caught:
        NormalizedPolygonSelector("canvas-normalized", "canvas-r1", points)
    assert caught.value.code == "invalid_polygon_selector"


@pytest.mark.parametrize("point", ((-0.1, 0), (1.1, 0), (float("nan"), 0)))
def test_points_reject_coordinates_outside_the_normalized_space(point):
    with pytest.raises(ValidationError) as caught:
        NormalizedPoint(*point)
    assert caught.value.code == "invalid_polygon_selector"


def test_legacy_normalized_rectangle_projects_four_corners_without_mutation():
    rectangle = {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.3}
    original = deepcopy(rectangle)

    view = project_legacy_rectangle_annotation(
        item_id="book-1",
        annotation_id="region-stable-7",
        annotation_revision="region-r4",
        source=_source(),
        rectangle=rectangle,
        role="MAR",
        extensions={"future": {"provider_block": "block-9"}},
    )

    assert rectangle == original
    assert view.key.annotation_id == "region-stable-7"
    assert view.revision == "region-r4"
    assert view.effective_role == "marginalia"
    assert view.selector.points == (
        NormalizedPoint(0.1, 0.2),
        NormalizedPoint(0.5, 0.2),
        NormalizedPoint(0.5, 0.5),
        NormalizedPoint(0.1, 0.5),
    )
    assert view.extensions["future"]["provider_block"] == "block-9"


def test_pixel_rectangle_requires_extent_and_normalizes_without_clipping():
    selector = normalized_polygon_from_legacy_rectangle(
        {"x": 100, "y": 200, "width": 400, "height": 600},
        coordinate_space="canvas-normalized",
        coordinate_space_revision="canvas-r1",
        canvas_width=1000,
        canvas_height=2000,
    )
    assert selector.points == (
        NormalizedPoint(0.1, 0.1),
        NormalizedPoint(0.5, 0.1),
        NormalizedPoint(0.5, 0.4),
        NormalizedPoint(0.1, 0.4),
    )

    with pytest.raises(ValidationError) as clipped:
        normalized_polygon_from_legacy_rectangle(
            {"x": 900, "y": 0, "w": 200, "h": 100},
            coordinate_space="canvas-normalized",
            coordinate_space_revision="canvas-r1",
            canvas_width=1000,
            canvas_height=1000,
        )
    assert clipped.value.code == "invalid_polygon_selector"


def test_ui_role_aliases_project_to_canonical_values_only():
    assert canonical_spatial_role("MAR") == "marginalia"
    assert canonical_spatial_role("mar") == "marginalia"
    assert canonical_spatial_role("ILL") == "figure"
    assert canonical_spatial_role("body") == "body"
    with pytest.raises(ValidationError) as caught:
        canonical_spatial_role("Marginal Note")
    assert caught.value.code == "invalid_artifact_identity"


def test_annotation_exposes_manual_role_caption_links_and_freshness():
    view = SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", "figure-1"),
        revision="annotation-r8",
        source=_source(),
        selector=_selector(),
        order=4,
        freshness="current",
        role_assignments=(
            SpatialRoleAssignment("ILL", "machine", "role-machine-r1"),
            SpatialRoleAssignment("marginalia", "manual", "role-human-r2"),
        ),
        caption_assertions=(
            CaptionAssertion(
                "An herbal illustration",
                "manual",
                "caption-r1",
                language="en",
            ),
        ),
        linked_artifact_ids=("crop-1",),
        provenance=ArtifactProvenance(
            origin="ocr",
            provider_id="mistral",
            model="ocr-latest",
        ),
    )
    assert view.effective_role == "marginalia"
    assert view.as_dict()["linked_artifact_ids"] == ["crop-1"]
    assert view.as_dict()["caption_assertions"][0]["text"] == (
        "An herbal illustration"
    )
    assert view.as_dict()["freshness"] == "current"


def test_annotation_selector_must_pin_the_source_canvas_revision():
    with pytest.raises(ValidationError) as caught:
        SpatialAnnotationView(
            key=SpatialAnnotationKey("book-1", "region-1"),
            revision="region-r1",
            source=_source(),
            selector=NormalizedPolygonSelector(
                "canvas-normalized",
                "older-canvas",
                (
                    NormalizedPoint(0, 0),
                    NormalizedPoint(1, 0),
                    NormalizedPoint(1, 1),
                ),
            ),
        )
    assert caught.value.code == "spatial_coordinate_revision_mismatch"


def test_legacy_projection_requires_caller_owned_stable_identity():
    with pytest.raises(ValidationError) as caught:
        project_legacy_rectangle_annotation(
            item_id="book-1",
            annotation_id="",
            annotation_revision="region-r1",
            source=_source(),
            rectangle={"x": 0, "y": 0, "w": 1, "h": 1},
        )
    assert caught.value.code == "invalid_artifact_identity"


def test_spatial_projector_port_is_framework_neutral_and_runtime_checkable():
    class Projector:
        def list_spatial_annotations(
            self,
            item_id,
            *,
            representation_id="",
            canvas_id="",
        ):
            return ()

        def get_spatial_annotation(self, key):
            return None

    assert isinstance(Projector(), SpatialAnnotationProjectorPort)
