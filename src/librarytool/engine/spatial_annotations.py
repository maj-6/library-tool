"""Portable spatial-annotation contracts and legacy rectangle projection.

Selectors are polygons in normalized ``0..1`` coordinates.  The named
coordinate space and its revision make the selector meaningful without
leaking a backing image path.  The legacy adapter is pure: it retains the
caller's annotation ID and never writes or invents persistence identifiers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .capabilities import CapabilityRef
from .raster_artifacts import (
    ArtifactFreshness,
    ArtifactProvenance,
    CaptionAssertion,
    JsonMapping,
    MAX_ASSERTIONS,
    MAX_PORTABLE_INTEGER,
    _EMPTY_MAPPING,
    _confidence,
    _enum,
    _extensions,
    _identifier,
    _revision,
    _safe_text,
    _thaw,
    _typed_values,
    _validation,
)


SPATIAL_ANNOTATIONS_READ_CAPABILITY = CapabilityRef(
    "library.spatial-annotations.read",
    1,
)
MAX_POLYGON_POINTS = 64
ROLE_ALIASES = {"MAR": "marginalia", "ILL": "figure"}


def canonical_spatial_role(value: Any) -> str:
    if not isinstance(value, str):
        raise _validation(
            "role must be a canonical role or supported UI alias",
            code="invalid_spatial_role",
            field_name="role",
        )
    role = ROLE_ALIASES.get(value.upper(), value)
    if role not in {"marginalia", "figure"}:
        role = _identifier(role, "role")
        if role != role.casefold():
            raise _validation(
                "stored roles must be canonical lower-case values",
                code="invalid_spatial_role",
                field_name="role",
            )
    return role


def _normalized_number(value: Any, field_name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _validation(
            f"{field_name} must be a normalized coordinate",
            code="invalid_polygon_selector",
            field_name=field_name,
        )
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1:
        raise _validation(
            f"{field_name} must be between zero and one",
            code="invalid_polygon_selector",
            field_name=field_name,
        )
    if number.is_integer():
        return int(number)
    return number


def _positive_extent(value: Any, field_name: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _validation(
            f"{field_name} must be a positive extent",
            code="invalid_legacy_rectangle",
            field_name=field_name,
        )
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise _validation(
            f"{field_name} must be a positive extent",
            code="invalid_legacy_rectangle",
            field_name=field_name,
        )
    if number.is_integer():
        return int(number)
    return number


def _cross(
    first: "NormalizedPoint",
    second: "NormalizedPoint",
    third: "NormalizedPoint",
) -> float:
    return float(
        (second.x - first.x) * (third.y - first.y)
        - (second.y - first.y) * (third.x - first.x)
    )


def _on_segment(
    first: "NormalizedPoint",
    second: "NormalizedPoint",
    point: "NormalizedPoint",
) -> bool:
    tolerance = 1e-12
    return (
        math.isclose(_cross(first, second, point), 0.0, abs_tol=tolerance)
        and min(first.x, second.x) - tolerance
        <= point.x
        <= max(first.x, second.x) + tolerance
        and min(first.y, second.y) - tolerance
        <= point.y
        <= max(first.y, second.y) + tolerance
    )


def _segments_intersect(
    first_start: "NormalizedPoint",
    first_end: "NormalizedPoint",
    second_start: "NormalizedPoint",
    second_end: "NormalizedPoint",
) -> bool:
    tolerance = 1e-12
    orientations = (
        _cross(first_start, first_end, second_start),
        _cross(first_start, first_end, second_end),
        _cross(second_start, second_end, first_start),
        _cross(second_start, second_end, first_end),
    )
    first_crosses = (
        orientations[0] > tolerance and orientations[1] < -tolerance
    ) or (orientations[0] < -tolerance and orientations[1] > tolerance)
    second_crosses = (
        orientations[2] > tolerance and orientations[3] < -tolerance
    ) or (orientations[2] < -tolerance and orientations[3] > tolerance)
    if first_crosses and second_crosses:
        return True
    return (
        math.isclose(orientations[0], 0.0, abs_tol=tolerance)
        and _on_segment(first_start, first_end, second_start)
    ) or (
        math.isclose(orientations[1], 0.0, abs_tol=tolerance)
        and _on_segment(first_start, first_end, second_end)
    ) or (
        math.isclose(orientations[2], 0.0, abs_tol=tolerance)
        and _on_segment(second_start, second_end, first_start)
    ) or (
        math.isclose(orientations[3], 0.0, abs_tol=tolerance)
        and _on_segment(second_start, second_end, first_end)
    )


def _adjacent_edges_overlap(
    before: "NormalizedPoint",
    shared: "NormalizedPoint",
    after: "NormalizedPoint",
) -> bool:
    """Return whether adjacent edges share more than their common vertex."""

    if not math.isclose(_cross(before, shared, after), 0.0, abs_tol=1e-12):
        return False
    before_from_shared = (before.x - shared.x, before.y - shared.y)
    after_from_shared = (after.x - shared.x, after.y - shared.y)
    return (
        before_from_shared[0] * after_from_shared[0]
        + before_from_shared[1] * after_from_shared[1]
    ) > 0


def _is_simple_polygon(points: tuple["NormalizedPoint", ...]) -> bool:
    edge_count = len(points)
    for first_index in range(edge_count):
        first_start = points[first_index]
        first_end = points[(first_index + 1) % edge_count]
        for second_index in range(first_index + 1, edge_count):
            second_start = points[second_index]
            second_end = points[(second_index + 1) % edge_count]
            adjacent = (
                (first_index + 1) % edge_count == second_index
                or (second_index + 1) % edge_count == first_index
            )
            if adjacent:
                if (first_index + 1) % edge_count == second_index:
                    if _adjacent_edges_overlap(
                        first_start,
                        first_end,
                        second_end,
                    ):
                        return False
                elif _adjacent_edges_overlap(
                    second_start,
                    second_end,
                    first_end,
                ):
                    return False
                continue
            if _segments_intersect(
                first_start,
                first_end,
                second_start,
                second_end,
            ):
                return False
    return True


class RoleAssignmentOrigin(str, Enum):
    MANUAL = "manual"
    MACHINE = "machine"
    IMPORTED = "imported"


@dataclass(frozen=True, slots=True)
class NormalizedPoint:
    x: int | float
    y: int | float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _normalized_number(self.x, "point.x"))
        object.__setattr__(self, "y", _normalized_number(self.y, "point.y"))

    def as_dict(self) -> dict[str, int | float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class NormalizedPolygonSelector:
    coordinate_space: str
    coordinate_space_revision: str
    points: tuple[NormalizedPoint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "coordinate_space",
            _identifier(self.coordinate_space, "coordinate_space"),
        )
        object.__setattr__(
            self,
            "coordinate_space_revision",
            _revision(self.coordinate_space_revision, "coordinate_space_revision"),
        )
        points = _typed_values(
            self.points,
            NormalizedPoint,
            "points",
            maximum=MAX_POLYGON_POINTS,
        )
        if len(points) < 3:
            raise _validation(
                "a polygon requires at least three points",
                code="invalid_polygon_selector",
                field_name="points",
            )
        coordinates = [(point.x, point.y) for point in points]
        if len(coordinates) != len(set(coordinates)):
            raise _validation(
                "polygon points must be distinct",
                code="invalid_polygon_selector",
                field_name="points",
            )
        area_twice = sum(
            (point.x * points[(index + 1) % len(points)].y)
            - (points[(index + 1) % len(points)].x * point.y)
            for index, point in enumerate(points)
        )
        if math.isclose(float(area_twice), 0.0, abs_tol=1e-12):
            raise _validation(
                "polygon points must enclose an area",
                code="invalid_polygon_selector",
                field_name="points",
            )
        if not _is_simple_polygon(points):
            raise _validation(
                "polygon edges must not cross or overlap",
                code="invalid_polygon_selector",
                field_name="points",
            )
        object.__setattr__(self, "points", points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "polygon",
            "coordinate_space": self.coordinate_space,
            "coordinate_space_revision": self.coordinate_space_revision,
            "points": [point.as_dict() for point in self.points],
        }


def normalized_polygon_from_legacy_rectangle(
    rectangle: Mapping[str, Any],
    *,
    coordinate_space: str,
    coordinate_space_revision: str,
    canvas_width: int | float | None = None,
    canvas_height: int | float | None = None,
) -> NormalizedPolygonSelector:
    """Adapt ``{x, y, w, h}`` without modifying the legacy mapping.

    Values are assumed normalized when no canvas extent is supplied.  Pixel
    rectangles require both extents and are divided by them.  No clipping is
    performed: an out-of-bounds legacy selector is an explicit validation
    failure rather than silently changed evidence.
    """

    if not isinstance(rectangle, Mapping):
        raise _validation(
            "legacy rectangle must be an object",
            code="invalid_legacy_rectangle",
            field_name="rectangle",
        )
    try:
        x = rectangle["x"]
        y = rectangle["y"]
        width = rectangle.get("w", rectangle.get("width"))
        height = rectangle.get("h", rectangle.get("height"))
    except KeyError as exc:
        raise _validation(
            "legacy rectangle requires x, y, width, and height",
            code="invalid_legacy_rectangle",
            field_name="rectangle",
        ) from exc
    if width is None or height is None:
        raise _validation(
            "legacy rectangle requires x, y, width, and height",
            code="invalid_legacy_rectangle",
            field_name="rectangle",
        )
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise _validation(
            "rectangle.x must be numeric",
            code="invalid_legacy_rectangle",
            field_name="rectangle.x",
        )
    if isinstance(y, bool) or not isinstance(y, (int, float)):
        raise _validation(
            "rectangle.y must be numeric",
            code="invalid_legacy_rectangle",
            field_name="rectangle.y",
        )
    width = _positive_extent(width, "rectangle.width")
    height = _positive_extent(height, "rectangle.height")
    if not math.isfinite(float(x)) or not math.isfinite(float(y)):
        raise _validation(
            "rectangle origin must be finite",
            code="invalid_legacy_rectangle",
            field_name="rectangle",
        )
    if (canvas_width is None) != (canvas_height is None):
        raise _validation(
            "pixel rectangle projection requires both canvas extents",
            code="invalid_legacy_rectangle",
            field_name="canvas_extent",
        )
    if canvas_width is not None and canvas_height is not None:
        canvas_width = _positive_extent(canvas_width, "canvas_width")
        canvas_height = _positive_extent(canvas_height, "canvas_height")
        x = float(x) / float(canvas_width)
        y = float(y) / float(canvas_height)
        width = float(width) / float(canvas_width)
        height = float(height) / float(canvas_height)
    x1 = float(x) + float(width)
    y1 = float(y) + float(height)
    return NormalizedPolygonSelector(
        coordinate_space=coordinate_space,
        coordinate_space_revision=coordinate_space_revision,
        points=(
            NormalizedPoint(x, y),
            NormalizedPoint(x1, y),
            NormalizedPoint(x1, y1),
            NormalizedPoint(x, y1),
        ),
    )


@dataclass(frozen=True, slots=True)
class SpatialAnnotationKey:
    item_id: str
    annotation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "annotation_id",
            _identifier(self.annotation_id, "annotation_id"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "annotation_id": self.annotation_id}


@dataclass(frozen=True, slots=True)
class SpatialSourceRef:
    representation_id: str
    representation_revision: str
    canvas_id: str
    canvas_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "representation_id",
            _identifier(self.representation_id, "representation_id"),
        )
        object.__setattr__(
            self,
            "representation_revision",
            _revision(self.representation_revision, "representation_revision"),
        )
        object.__setattr__(self, "canvas_id", _identifier(self.canvas_id, "canvas_id"))
        object.__setattr__(
            self,
            "canvas_revision",
            _revision(self.canvas_revision, "canvas_revision"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "representation_id": self.representation_id,
            "representation_revision": self.representation_revision,
            "canvas_id": self.canvas_id,
            "canvas_revision": self.canvas_revision,
        }


@dataclass(frozen=True, slots=True)
class SpatialRoleAssignment:
    role: str
    origin: RoleAssignmentOrigin | str
    revision: str
    confidence: int | float | None = None
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", canonical_spatial_role(self.role))
        object.__setattr__(
            self,
            "origin",
            _enum(self.origin, RoleAssignmentOrigin, "origin"),
        )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "confidence"),
        )
        if not isinstance(self.provenance, ArtifactProvenance):
            raise _validation(
                "provenance must be ArtifactProvenance",
                code="invalid_spatial_role",
                field_name="provenance",
            )
        object.__setattr__(
            self,
            "extensions",
            _extensions(self.extensions, "role.extensions"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "origin": self.origin.value,
            "revision": self.revision,
            "confidence": self.confidence,
            "provenance": self.provenance.as_dict(),
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class SpatialAnnotationView:
    key: SpatialAnnotationKey
    revision: str
    source: SpatialSourceRef
    selector: NormalizedPolygonSelector
    order: int = 0
    label: str = ""
    freshness: ArtifactFreshness | str = ArtifactFreshness.UNTRACKED
    role_assignments: tuple[SpatialRoleAssignment, ...] = ()
    caption_assertions: tuple[CaptionAssertion, ...] = ()
    linked_artifact_ids: tuple[str, ...] = ()
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if not isinstance(self.key, SpatialAnnotationKey):
            raise _validation(
                "key must be SpatialAnnotationKey",
                code="invalid_spatial_identity",
                field_name="key",
            )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        if not isinstance(self.source, SpatialSourceRef):
            raise _validation(
                "source must be SpatialSourceRef",
                code="invalid_spatial_source",
                field_name="source",
            )
        if not isinstance(self.selector, NormalizedPolygonSelector):
            raise _validation(
                "selector must be NormalizedPolygonSelector",
                code="invalid_polygon_selector",
                field_name="selector",
            )
        if (
            self.selector.coordinate_space_revision
            != self.source.canvas_revision
        ):
            raise _validation(
                "selector coordinate space must pin the source canvas revision",
                code="spatial_coordinate_revision_mismatch",
                field_name="selector.coordinate_space_revision",
            )
        if (
            isinstance(self.order, bool)
            or not isinstance(self.order, int)
            or self.order < 0
            or self.order > MAX_PORTABLE_INTEGER
        ):
            raise _validation(
                "order must be a non-negative portable integer",
                code="invalid_spatial_order",
                field_name="order",
            )
        object.__setattr__(self, "label", _safe_text(self.label, "label", maximum=512))
        object.__setattr__(
            self,
            "freshness",
            _enum(self.freshness, ArtifactFreshness, "freshness"),
        )
        roles = _typed_values(
            self.role_assignments,
            SpatialRoleAssignment,
            "role_assignments",
            maximum=len(RoleAssignmentOrigin),
        )
        origins = [assignment.origin for assignment in roles]
        if len(origins) != len(set(origins)):
            raise _validation(
                "role assignments must have unique origins",
                code="invalid_spatial_role",
                field_name="role_assignments",
            )
        object.__setattr__(self, "role_assignments", roles)
        captions = _typed_values(
            self.caption_assertions,
            CaptionAssertion,
            "caption_assertions",
            maximum=MAX_ASSERTIONS,
        )
        caption_origins = [caption.origin for caption in captions]
        if len(caption_origins) != len(set(caption_origins)):
            raise _validation(
                "caption assertions must have unique origins",
                code="invalid_caption_assertion",
                field_name="caption_assertions",
            )
        object.__setattr__(self, "caption_assertions", captions)
        if isinstance(self.linked_artifact_ids, (str, bytes)) or not isinstance(
            self.linked_artifact_ids,
            Sequence,
        ):
            raise _validation(
                "linked_artifact_ids must be an array",
                code="invalid_spatial_link",
                field_name="linked_artifact_ids",
            )
        linked = tuple(
            _identifier(value, "linked_artifact_id")
            for value in self.linked_artifact_ids
        )
        if len(linked) > 64 or len(linked) != len(set(linked)):
            raise _validation(
                "linked artifact IDs must be bounded and unique",
                code="invalid_spatial_link",
                field_name="linked_artifact_ids",
            )
        object.__setattr__(self, "linked_artifact_ids", linked)
        if not isinstance(self.provenance, ArtifactProvenance):
            raise _validation(
                "provenance must be ArtifactProvenance",
                code="invalid_spatial_contract",
                field_name="provenance",
            )
        object.__setattr__(self, "extensions", _extensions(self.extensions))

    @property
    def effective_role(self) -> str:
        by_origin = {value.origin: value for value in self.role_assignments}
        for origin in (
            RoleAssignmentOrigin.MANUAL,
            RoleAssignmentOrigin.IMPORTED,
            RoleAssignmentOrigin.MACHINE,
        ):
            if origin in by_origin:
                return by_origin[origin].role
        return ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "revision": self.revision,
            "source": self.source.as_dict(),
            "selector": self.selector.as_dict(),
            "order": self.order,
            "label": self.label,
            "freshness": self.freshness.value,
            "role_assignments": [value.as_dict() for value in self.role_assignments],
            "effective_role": self.effective_role,
            "caption_assertions": [
                value.as_dict() for value in self.caption_assertions
            ],
            "linked_artifact_ids": list(self.linked_artifact_ids),
            "provenance": self.provenance.as_dict(),
            "extensions": _thaw(self.extensions),
        }


def project_legacy_rectangle_annotation(
    *,
    item_id: str,
    annotation_id: str,
    annotation_revision: str,
    source: SpatialSourceRef,
    rectangle: Mapping[str, Any],
    coordinate_space: str = "canvas-normalized",
    canvas_width: int | float | None = None,
    canvas_height: int | float | None = None,
    order: int = 0,
    role: str = "",
    role_origin: RoleAssignmentOrigin | str = RoleAssignmentOrigin.MACHINE,
    provenance: ArtifactProvenance | None = None,
    extensions: JsonMapping | None = None,
) -> SpatialAnnotationView:
    """Purely project one legacy rectangle while preserving its identity."""

    if not isinstance(source, SpatialSourceRef):
        raise _validation(
            "source must be SpatialSourceRef",
            code="invalid_spatial_source",
            field_name="source",
        )
    role_assignments: tuple[SpatialRoleAssignment, ...] = ()
    if role:
        role_assignments = (
            SpatialRoleAssignment(
                role=role,
                origin=role_origin,
                revision=annotation_revision,
                provenance=provenance or ArtifactProvenance(origin="legacy"),
            ),
        )
    return SpatialAnnotationView(
        key=SpatialAnnotationKey(item_id, annotation_id),
        revision=annotation_revision,
        source=source,
        selector=normalized_polygon_from_legacy_rectangle(
            rectangle,
            coordinate_space=coordinate_space,
            coordinate_space_revision=source.canvas_revision,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
        ),
        order=order,
        role_assignments=role_assignments,
        provenance=provenance or ArtifactProvenance(origin="legacy"),
        extensions=extensions or {},
    )


@runtime_checkable
class SpatialAnnotationProjectorPort(Protocol):
    """Read/project annotations without exposing legacy persistence records."""

    def list_spatial_annotations(
        self,
        item_id: str,
        *,
        representation_id: str = "",
        canvas_id: str = "",
    ) -> Sequence[SpatialAnnotationView]: ...

    def get_spatial_annotation(
        self,
        key: SpatialAnnotationKey,
    ) -> SpatialAnnotationView | None: ...


__all__ = [
    "NormalizedPoint",
    "NormalizedPolygonSelector",
    "ROLE_ALIASES",
    "RoleAssignmentOrigin",
    "SPATIAL_ANNOTATIONS_READ_CAPABILITY",
    "SpatialAnnotationKey",
    "SpatialAnnotationProjectorPort",
    "SpatialAnnotationView",
    "SpatialRoleAssignment",
    "SpatialSourceRef",
    "canonical_spatial_role",
    "normalized_polygon_from_legacy_rectangle",
    "project_legacy_rectangle_annotation",
]
