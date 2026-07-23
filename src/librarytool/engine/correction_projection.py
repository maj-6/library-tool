"""Live correction projections over immutable raster and annotation evidence.

The correction aggregate stores human assertions and audit state.  Raster and
layout projectors remain authoritative for machine evidence.  This module
bridges those two views without copying browser state into the engine:

* a base aggregate is projected from current raster/annotation views;
* durable human assertions are reconciled over that live base; and
* query results expose the reconciled target revisions and effective values.

This separation lets later OCR/layout proposals replace machine evidence while
preserving manual classifications.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .corrections import (
    CORRECTION_LINK_AUTHORITY_EXTENSION,
    CORRECTION_TARGET_AUTHORITY_EXTENSION,
    AnnotationCorrectionSnapshot,
    ArtifactCorrectionSnapshot,
    CorrectionAggregateSnapshot,
    CorrectionRepositoryPort,
    CorrectionReviewSnapshot,
    EffectiveCategoryOrigin,
    MetadataAssertionOrigin,
)
from .errors import NotFoundError, RepositoryError
from .raster_artifacts import (
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionOrigin,
    CategoryAssignment,
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterResourceRef,
)
from .spatial_annotations import (
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialAnnotationProjectorPort,
    SpatialAnnotationView,
)


_QUERY_OPERATION_ID = "corrections-query"
_MACHINE_EVIDENCE_REVISION = "machine_evidence_revision"
_CATEGORY_LINEAGE_RELATIONS = frozenset(
    {
        "derived_from",
        "extracted_from",
        "processed_from",
    }
)


def _revision(prefix: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()}"


def _category_sources(
    values: tuple[RasterArtifactView, ...],
) -> dict[str, str]:
    artifact_ids = {value.key.artifact_id for value in values}
    sources: dict[str, str] = {}
    for value in values:
        candidates = {
            lineage.artifact_id
            for lineage in value.lineage
            if (
                lineage.relation in _CATEGORY_LINEAGE_RELATIONS
                and lineage.artifact_id in artifact_ids
            )
        }
        sources[value.key.artifact_id] = (
            next(iter(candidates)) if len(candidates) == 1 else ""
        )

    # Raster lineage is broader than category inheritance and does not promise
    # to be acyclic. Detect cycles against the immutable graph, then remove
    # every edge in each cycle so the outcome cannot depend on input order.
    cyclic: set[str] = set()
    for artifact_id in sources:
        path: list[str] = []
        positions: dict[str, int] = {}
        current = artifact_id
        while current:
            if current in positions:
                cyclic.update(path[positions[current] :])
                break
            if current not in sources:
                break
            positions[current] = len(path)
            path.append(current)
            current = sources[current]
    return {
        artifact_id: "" if artifact_id in cyclic else source_id
        for artifact_id, source_id in sources.items()
    }


def _link_authority(
    linked_artifact_ids: tuple[str, ...],
    known_artifacts: set[str],
) -> tuple[str, dict[str, Any]]:
    linked = tuple(linked_artifact_ids)
    if not linked:
        state = "none"
        effective = ""
    elif len(linked) > 1:
        state = "ambiguous"
        effective = ""
    elif linked[0] not in known_artifacts:
        state = "missing"
        effective = ""
    else:
        state = "single"
        effective = linked[0]
    return effective, {
        "state": state,
        "live_artifact_ids": list(linked),
    }


class CorrectionAggregateProjector:
    """Project current machine evidence into the correction aggregate shape."""

    def __init__(
        self,
        raster_artifacts: RasterArtifactProjectorPort,
        spatial_annotations: SpatialAnnotationProjectorPort,
    ) -> None:
        self._raster_artifacts = raster_artifacts
        self._spatial_annotations = spatial_annotations

    def project(self, item_id: str) -> CorrectionAggregateSnapshot:
        rasters = tuple(
            sorted(
                self._raster_artifacts.list_raster_artifacts(item_id),
                key=lambda value: value.key.artifact_id,
            )
        )
        annotations = tuple(
            sorted(
                self._spatial_annotations.list_spatial_annotations(item_id),
                key=lambda value: value.key.annotation_id,
            )
        )
        if any(
            not isinstance(value, RasterArtifactView) or value.key.item_id != item_id
            for value in rasters
        ) or any(
            not isinstance(value, SpatialAnnotationView) or value.key.item_id != item_id
            for value in annotations
        ):
            raise RepositoryError(
                "the correction base projectors returned invalid state",
                code="invalid_correction_base_projection",
                details={"item_id": item_id},
            )
        raster_ids = [value.key.artifact_id for value in rasters]
        annotation_ids = [value.key.annotation_id for value in annotations]
        if len(raster_ids) != len({value.casefold() for value in raster_ids}) or len(
            annotation_ids
        ) != len({value.casefold() for value in annotation_ids}):
            raise RepositoryError(
                "the correction base projectors returned duplicate targets",
                code="invalid_correction_base_projection",
                details={"item_id": item_id},
            )

        serialized = {
            "item_id": item_id,
            "rasters": [value.as_dict() for value in rasters],
            "annotations": [value.as_dict() for value in annotations],
        }
        aggregate_revision = _revision("correction-base", serialized)
        sources = _category_sources(rasters)
        known_artifacts = set(raster_ids)
        artifacts = tuple(
            ArtifactCorrectionSnapshot(
                key=value.key,
                revision=value.revision,
                source_artifact_id=sources[value.key.artifact_id],
                category_assignments=value.category_assignments,
                caption_assertions=value.caption_assertions,
                extensions={
                    _MACHINE_EVIDENCE_REVISION: value.revision,
                },
            )
            for value in rasters
        )
        spatial = []
        for value in annotations:
            linked_artifact_id, link_authority = _link_authority(
                value.linked_artifact_ids,
                known_artifacts,
            )
            spatial.append(
                AnnotationCorrectionSnapshot(
                    key=value.key,
                    revision=value.revision,
                    linked_artifact_id=linked_artifact_id,
                    role_assignments=value.role_assignments,
                    extensions={
                        _MACHINE_EVIDENCE_REVISION: value.revision,
                        CORRECTION_LINK_AUTHORITY_EXTENSION: link_authority,
                    },
                )
            )
        return CorrectionAggregateSnapshot(
            item_id=item_id,
            revision=aggregate_revision,
            artifacts=artifacts,
            annotations=tuple(spatial),
            review=CorrectionReviewSnapshot(
                _revision(
                    "correction-review-base",
                    {"item_id": item_id, "aggregate": aggregate_revision},
                )
            ),
        )


def _manual(values: tuple[Any, ...], origin: Any) -> tuple[Any, ...]:
    return tuple(value for value in values if value.origin is origin)


def _non_manual(values: tuple[Any, ...], origin: Any) -> tuple[Any, ...]:
    return tuple(value for value in values if value.origin is not origin)


def _reconciled_extensions(
    live: Any,
    durable: Any,
) -> dict[str, Any]:
    extensions = dict(durable.extensions)
    extensions.pop(CORRECTION_TARGET_AUTHORITY_EXTENSION, None)
    evidence_revision = live.extensions.get(_MACHINE_EVIDENCE_REVISION)
    if evidence_revision is not None:
        extensions[_MACHINE_EVIDENCE_REVISION] = evidence_revision
    return extensions


def _unavailable_target(
    value: ArtifactCorrectionSnapshot | AnnotationCorrectionSnapshot,
) -> ArtifactCorrectionSnapshot | AnnotationCorrectionSnapshot:
    authority = value.extensions.get(CORRECTION_TARGET_AUTHORITY_EXTENSION)
    if isinstance(authority, Mapping) and dict(authority) == {"state": "missing"}:
        return value
    serialized = value.as_dict()
    extensions = serialized["extensions"]
    extensions[CORRECTION_TARGET_AUTHORITY_EXTENSION] = {"state": "missing"}
    prefix = (
        "correction-artifact-unavailable"
        if isinstance(value, ArtifactCorrectionSnapshot)
        else "correction-annotation-unavailable"
    )
    return replace(
        value,
        revision=_revision(
            prefix,
            {
                "prior_revision": value.revision,
                "target": serialized,
            },
        ),
        extensions=extensions,
    )


def _reconciled_artifact(
    live: ArtifactCorrectionSnapshot,
    durable: ArtifactCorrectionSnapshot,
) -> ArtifactCorrectionSnapshot:
    categories = (
        *_non_manual(live.category_assignments, AssignmentOrigin.MANUAL),
        *_manual(durable.category_assignments, AssignmentOrigin.MANUAL),
    )
    captions = (
        *_non_manual(live.caption_assertions, CaptionOrigin.MANUAL),
        *_manual(durable.caption_assertions, CaptionOrigin.MANUAL),
    )
    roles = (
        *_non_manual(live.role_assignments, RoleAssignmentOrigin.MANUAL),
        *_manual(durable.role_assignments, RoleAssignmentOrigin.MANUAL),
    )
    metadata = (
        *_non_manual(
            live.metadata_assertions,
            MetadataAssertionOrigin.MANUAL,
        ),
        *_manual(
            durable.metadata_assertions,
            MetadataAssertionOrigin.MANUAL,
        ),
    )
    extensions = _reconciled_extensions(live, durable)
    semantic = {
        "source_artifact_id": live.source_artifact_id,
        "category_assignments": [value.as_dict() for value in categories],
        "caption_assertions": [value.as_dict() for value in captions],
        "role_assignments": [value.as_dict() for value in roles],
        "metadata_assertions": [value.as_dict() for value in metadata],
        "extensions": extensions,
    }
    durable_semantic = durable.as_dict()
    durable_semantic.pop("key")
    durable_semantic.pop("revision")
    revision = (
        durable.revision
        if semantic == durable_semantic
        else _revision(
            "correction-artifact",
            {
                "key": live.key.as_dict(),
                "live_revision": live.revision,
                "durable_revision": durable.revision,
                **semantic,
            },
        )
    )
    return ArtifactCorrectionSnapshot(
        key=live.key,
        revision=revision,
        source_artifact_id=live.source_artifact_id,
        category_assignments=categories,
        caption_assertions=captions,
        role_assignments=roles,
        metadata_assertions=metadata,
        extensions=extensions,
    )


def _reconciled_annotation(
    live: AnnotationCorrectionSnapshot,
    durable: AnnotationCorrectionSnapshot,
    *,
    live_artifact_ids: set[str],
) -> AnnotationCorrectionSnapshot:
    roles = (
        *_non_manual(live.role_assignments, RoleAssignmentOrigin.MANUAL),
        *_manual(durable.role_assignments, RoleAssignmentOrigin.MANUAL),
    )
    extensions = _reconciled_extensions(live, durable)
    authority = dict(
        live.as_dict()["extensions"].get(
            CORRECTION_LINK_AUTHORITY_EXTENSION,
            {},
        )
    )
    state = authority.get("state")
    linked_artifact_id = live.linked_artifact_id
    if durable.linked_artifact_id:
        if durable.linked_artifact_id not in live_artifact_ids:
            state = "missing"
        elif (
            state == "single" and live.linked_artifact_id != durable.linked_artifact_id
        ):
            state = "conflict"
        linked_artifact_id = durable.linked_artifact_id
    authority["state"] = state
    extensions[CORRECTION_LINK_AUTHORITY_EXTENSION] = authority
    semantic = {
        "linked_artifact_id": linked_artifact_id,
        "role_assignments": [value.as_dict() for value in roles],
        "extensions": extensions,
    }
    durable_semantic = durable.as_dict()
    durable_semantic.pop("key")
    durable_semantic.pop("revision")
    revision = (
        durable.revision
        if semantic == durable_semantic
        else _revision(
            "correction-annotation",
            {
                "key": live.key.as_dict(),
                "live_revision": live.revision,
                "durable_revision": durable.revision,
                **semantic,
            },
        )
    )
    return AnnotationCorrectionSnapshot(
        key=live.key,
        revision=revision,
        linked_artifact_id=linked_artifact_id,
        role_assignments=roles,
        extensions=extensions,
    )


def reconcile_correction_aggregates(
    live: CorrectionAggregateSnapshot,
    durable: CorrectionAggregateSnapshot,
) -> CorrectionAggregateSnapshot:
    """Merge current machine evidence with durable human assertions."""

    if live.item_id != durable.item_id:
        raise RepositoryError(
            "correction aggregate reconciliation crossed item scope",
            code="correction_repository_scope_mismatch",
            details={
                "live_item_id": live.item_id,
                "durable_item_id": durable.item_id,
            },
        )
    durable_artifacts = {value.key.artifact_id: value for value in durable.artifacts}
    durable_annotations = {
        value.key.annotation_id: value for value in durable.annotations
    }
    live_artifact_ids = {value.key.artifact_id for value in live.artifacts}
    artifacts = [
        (
            value
            if (
                stored := durable_artifacts.pop(
                    value.key.artifact_id,
                    None,
                )
            )
            is None
            else _reconciled_artifact(value, stored)
        )
        for value in live.artifacts
    ]
    annotations = [
        (
            value
            if (
                stored := durable_annotations.pop(
                    value.key.annotation_id,
                    None,
                )
            )
            is None
            else _reconciled_annotation(
                value,
                stored,
                live_artifact_ids=live_artifact_ids,
            )
        )
        for value in live.annotations
    ]
    # Temporarily unavailable base targets retain their durable assertions.
    # They can become visible again without an unrelated mutation erasing them.
    artifacts.extend(
        _unavailable_target(value) for value in durable_artifacts.values()
    )
    annotations.extend(
        _unavailable_target(value) for value in durable_annotations.values()
    )
    artifact_values = tuple(sorted(artifacts, key=lambda value: value.key.artifact_id))
    annotation_values = tuple(
        sorted(annotations, key=lambda value: value.key.annotation_id)
    )
    if (
        artifact_values == durable.artifacts
        and annotation_values == durable.annotations
    ):
        return durable
    return CorrectionAggregateSnapshot(
        item_id=live.item_id,
        revision=_revision(
            "correction-aggregate",
            {
                "live_revision": live.revision,
                "durable_revision": durable.revision,
                "artifacts": [value.as_dict() for value in artifact_values],
                "annotations": [value.as_dict() for value in annotation_values],
                "review": durable.review.as_dict(),
            },
        ),
        artifacts=artifact_values,
        annotations=annotation_values,
        review=durable.review,
    )


def _inherited_category(
    aggregate: CorrectionAggregateSnapshot,
    artifact_id: str,
) -> CategoryAssignment | None:
    effective = aggregate.effective_category(artifact_id)
    if effective.origin is not EffectiveCategoryOrigin.INHERITED:
        return None
    source = aggregate.artifact(effective.inherited_from_artifact_id)
    provenance = ArtifactProvenance(origin="inherited")
    if source is not None:
        for assignment in source.category_assignments:
            if assignment.revision == effective.assignment_revision:
                provenance = assignment.provenance
                break
    return CategoryAssignment(
        effective.category,
        AssignmentOrigin.INHERITED,
        effective.assignment_revision,
        inherited_from_artifact_id=effective.inherited_from_artifact_id,
        provenance=provenance,
    )


class CorrectionProjectionService(
    RasterArtifactProjectorPort,
    SpatialAnnotationProjectorPort,
):
    """Overlay durable correction state onto live read projections."""

    def __init__(
        self,
        raster_artifacts: RasterArtifactProjectorPort,
        spatial_annotations: SpatialAnnotationProjectorPort,
        repository: CorrectionRepositoryPort,
    ) -> None:
        self._raster_artifacts = raster_artifacts
        self._spatial_annotations = spatial_annotations
        self._repository = repository

    @staticmethod
    def _state(
        value: Any,
        item_id: str,
    ) -> CorrectionAggregateSnapshot:
        if value is None:
            raise NotFoundError(
                "the correction item does not exist",
                code="correction_item_not_found",
                details={"item_id": item_id},
            )
        if not isinstance(value, CorrectionAggregateSnapshot):
            raise RepositoryError(
                "the correction repository returned invalid state",
                code="invalid_correction_snapshot",
                details={"item_id": item_id},
            )
        return value

    def list_raster_artifacts(
        self,
        item_id: str,
    ) -> tuple[RasterArtifactView, ...]:
        with self._repository.unit_of_work(operation_id=_QUERY_OPERATION_ID) as unit:
            aggregate = self._state(unit.get(item_id), item_id)
            values = tuple(self._raster_artifacts.list_raster_artifacts(item_id))
        result = []
        for value in values:
            correction = aggregate.artifact(value.key.artifact_id)
            if correction is None:
                result.append(value)
                continue
            categories = tuple(
                assignment
                for assignment in correction.category_assignments
                if assignment.origin is not AssignmentOrigin.INHERITED
            )
            inherited = _inherited_category(
                aggregate,
                value.key.artifact_id,
            )
            if inherited is not None:
                categories = (*categories, inherited)
            result.append(
                replace(
                    value,
                    revision=correction.revision,
                    category_assignments=categories,
                    caption_assertions=correction.caption_assertions,
                )
            )
        return tuple(result)

    def get_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be RasterArtifactKey")
        return next(
            (
                value
                for value in self.list_raster_artifacts(key.item_id)
                if value.key == key
            ),
            None,
        )

    def list_spatial_annotations(
        self,
        item_id: str,
        *,
        representation_id: str = "",
        canvas_id: str = "",
    ) -> tuple[SpatialAnnotationView, ...]:
        with self._repository.unit_of_work(operation_id=_QUERY_OPERATION_ID) as unit:
            aggregate = self._state(unit.get(item_id), item_id)
            values = tuple(
                self._spatial_annotations.list_spatial_annotations(
                    item_id,
                    representation_id=representation_id,
                    canvas_id=canvas_id,
                )
            )
        return tuple(
            (
                value
                if (correction := aggregate.annotation(value.key.annotation_id)) is None
                else replace(
                    value,
                    revision=correction.revision,
                    role_assignments=correction.role_assignments,
                    linked_artifact_ids=self._linked_artifact_ids(
                        value,
                        correction,
                    ),
                )
            )
            for value in values
        )

    @staticmethod
    def _linked_artifact_ids(
        value: SpatialAnnotationView,
        correction: AnnotationCorrectionSnapshot,
    ) -> tuple[str, ...]:
        authority = correction.extensions.get(
            CORRECTION_LINK_AUTHORITY_EXTENSION,
            {},
        )
        state = authority.get("state") if isinstance(authority, Mapping) else ""
        if state in {"ambiguous", "missing", "conflict"}:
            return tuple(
                sorted(
                    {
                        *value.linked_artifact_ids,
                        *(
                            (correction.linked_artifact_id,)
                            if correction.linked_artifact_id
                            else ()
                        ),
                    }
                )
            )
        if correction.linked_artifact_id:
            return (correction.linked_artifact_id,)
        return value.linked_artifact_ids

    def get_spatial_annotation(
        self,
        key: SpatialAnnotationKey,
    ) -> SpatialAnnotationView | None:
        if not isinstance(key, SpatialAnnotationKey):
            raise TypeError("key must be SpatialAnnotationKey")
        return next(
            (
                value
                for value in self.list_spatial_annotations(key.item_id)
                if value.key == key
            ),
            None,
        )

    def resolve_raster_resource(
        self,
        item_id: str,
        resource: RasterResourceRef,
    ) -> Any:
        resolver = getattr(
            self._raster_artifacts,
            "resolve_raster_resource",
            None,
        )
        if not callable(resolver):
            return None
        return resolver(item_id, resource)


__all__ = [
    "CorrectionAggregateProjector",
    "CorrectionProjectionService",
    "reconcile_correction_aggregates",
]
