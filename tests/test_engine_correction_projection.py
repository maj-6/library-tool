from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

from librarytool.engine.correction_projection import (
    CorrectionAggregateProjector,
    CorrectionProjectionService,
    reconcile_correction_aggregates,
)
from librarytool.engine.corrections import (
    CORRECTION_TARGET_AUTHORITY_EXTENSION,
    CorrectionAggregateSnapshot,
)
from librarytool.engine.raster_artifacts import (
    ArtifactMetadataAssertion,
    ArtifactFreshness,
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    MetadataAssertionOrigin,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterLineageRef,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
)


def _raster(
    artifact_id: str,
    *,
    revision: str | None = None,
    lineage: tuple[RasterLineageRef, ...] = (),
    categories: tuple[CategoryAssignment, ...] = (),
    roles: tuple[SpatialRoleAssignment, ...] = (),
    captions: tuple[CaptionAssertion, ...] = (),
    metadata: tuple[ArtifactMetadataAssertion, ...] = (),
) -> RasterArtifactView:
    return RasterArtifactView(
        key=RasterArtifactKey("book-1", artifact_id),
        revision=revision or f"{artifact_id}-r1",
        kind="captured-image",
        media_type="image/jpeg",
        content_sha256="ab" * 32,
        dimensions=RasterDimensions(1200, 1800),
        source=RasterSourceRef(
            "capture",
            "capture-r1",
            "page-1",
            "page-1-r1",
        ),
        resource_state=ResourceState.AVAILABLE,
        resource=RasterResourceRef(
            f"resource:{artifact_id}",
            f"bytes-{artifact_id}-r1",
        ),
        freshness=ArtifactFreshness.CURRENT,
        lineage=lineage,
        category_assignments=categories,
        role_assignments=roles,
        caption_assertions=captions,
        metadata_assertions=metadata,
        provenance=ArtifactProvenance(origin="capture"),
    )


def _annotation(
    annotation_id: str,
    *,
    revision: str | None = None,
    roles: tuple[SpatialRoleAssignment, ...] = (),
    linked_artifact_ids: tuple[str, ...] = (),
) -> SpatialAnnotationView:
    return SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", annotation_id),
        revision=revision or f"{annotation_id}-r1",
        source=SpatialSourceRef(
            "capture",
            "capture-r1",
            "page-1",
            "page-1-r1",
        ),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            "page-1-r1",
            (
                NormalizedPoint(0.1, 0.1),
                NormalizedPoint(0.9, 0.1),
                NormalizedPoint(0.9, 0.9),
                NormalizedPoint(0.1, 0.9),
            ),
        ),
        freshness=ArtifactFreshness.CURRENT,
        role_assignments=roles,
        linked_artifact_ids=linked_artifact_ids,
        provenance=ArtifactProvenance(origin="mistral"),
    )


class _RasterProjector:
    def __init__(self, rows, *, resolved=None):
        self.rows = tuple(rows)
        self.resolved = resolved
        self.resolve_calls = []
        self.list_calls = []
        self.get_calls = []

    def list_raster_artifacts(self, item_id):
        self.list_calls.append(item_id)
        return tuple(row for row in self.rows if row.key.item_id == item_id)

    def get_raster_artifact(self, key):
        self.get_calls.append(key)
        return next((row for row in self.rows if row.key == key), None)

    def get_capture_raster_artifact(self, key):
        return self.get_raster_artifact(key)

    def resolve_raster_resource(self, item_id, resource):
        self.resolve_calls.append((item_id, resource))
        return self.resolved


class _SpatialProjector:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def list_spatial_annotations(
        self,
        item_id,
        *,
        representation_id="",
        canvas_id="",
    ):
        return tuple(
            row
            for row in self.rows
            if row.key.item_id == item_id
            and (
                not representation_id
                or row.source.representation_id == representation_id
            )
            and (not canvas_id or row.source.canvas_id == canvas_id)
        )

    def get_spatial_annotation(self, key):
        return next((row for row in self.rows if row.key == key), None)


class _ReadUnit:
    def __init__(self, state):
        self.state = state

    def get(self, item_id):
        return self.state if self.state.item_id == item_id else None


class _ReadRepository:
    def __init__(self, state):
        self.state = state
        self.operations = []

    @contextmanager
    def unit_of_work(self, *, operation_id):
        self.operations.append(operation_id)
        yield _ReadUnit(self.state)


class _DurableProbeUnit(_ReadUnit):
    def __init__(self, state, values):
        super().__init__(state)
        self.values = list(values)
        self.get_calls = []

    def has_durable_aggregate(self, item_id):
        assert item_id == self.state.item_id
        return self.values.pop(0)

    def get(self, item_id):
        self.get_calls.append(item_id)
        return super().get(item_id)


class _DurableProbeRepository:
    def __init__(self, unit):
        self.unit = unit

    @contextmanager
    def unit_of_work(self, *, operation_id):
        yield self.unit


def _project(
    rasters: tuple[RasterArtifactView, ...],
    annotations: tuple[SpatialAnnotationView, ...] = (),
) -> CorrectionAggregateSnapshot:
    return CorrectionAggregateProjector(
        _RasterProjector(rasters),
        _SpatialProjector(annotations),
    ).project("book-1")


def test_aggregate_projection_is_deterministic_across_input_order():
    source = _raster("image-a")
    derived = _raster(
        "image-b",
        lineage=(
            RasterLineageRef(
                "image-a",
                source.revision,
                "derived_from",
            ),
        ),
    )
    first_annotation = _annotation(
        "region-a",
        linked_artifact_ids=("image-a",),
    )
    second_annotation = _annotation("region-b")

    forward = _project(
        (derived, source),
        (second_annotation, first_annotation),
    )
    reverse = _project(
        (source, derived),
        (first_annotation, second_annotation),
    )

    assert forward.as_dict() == reverse.as_dict()
    assert forward.revision == reverse.revision
    assert [value.key.artifact_id for value in forward.artifacts] == [
        "image-a",
        "image-b",
    ]
    assert [value.key.annotation_id for value in forward.annotations] == [
        "region-a",
        "region-b",
    ]
    assert forward.artifact("image-b").source_artifact_id == "image-a"


def test_category_lineage_cycles_clear_every_cycle_edge():
    first = _raster(
        "image-a",
        lineage=(RasterLineageRef("image-b", "image-b-r1", "derived_from"),),
    )
    second = _raster(
        "image-b",
        lineage=(RasterLineageRef("image-a", "image-a-r1", "derived_from"),),
    )

    forward = _project((first, second))
    reverse = _project((second, first))

    assert forward.as_dict() == reverse.as_dict()
    assert {
        value.key.artifact_id: value.source_artifact_id for value in forward.artifacts
    } == {"image-a": "", "image-b": ""}


def test_reconciliation_preserves_manual_evidence_and_only_advances_for_changes():
    initial_raster = _raster(
        "image-a",
        categories=(
            CategoryAssignment(
                "cover",
                AssignmentOrigin.SUGGESTED,
                "machine-category-r1",
            ),
        ),
        captions=(
            CaptionAssertion(
                "Initial machine caption",
                CaptionOrigin.MACHINE,
                "machine-caption-r1",
            ),
        ),
        metadata=(
            ArtifactMetadataAssertion(
                "plate_number",
                3,
                MetadataAssertionOrigin.MACHINE,
                "machine-metadata-r1",
                provenance=ArtifactProvenance(
                    origin="machine",
                    provider_id="mistral",
                ),
            ),
        ),
    )
    initial_annotation = _annotation(
        "region-a",
        roles=(
            SpatialRoleAssignment(
                "figure",
                RoleAssignmentOrigin.MACHINE,
                "machine-role-r1",
            ),
        ),
        linked_artifact_ids=("image-a",),
    )
    live = _project((initial_raster,), (initial_annotation,))
    live_artifact = live.artifact("image-a")
    live_annotation = live.annotation("region-a")
    assert live_artifact is not None
    assert live_annotation is not None

    manual_category = CategoryAssignment(
        "title_page",
        AssignmentOrigin.MANUAL,
        "manual-category-r1",
    )
    manual_caption = CaptionAssertion(
        "Corrected caption",
        CaptionOrigin.MANUAL,
        "manual-caption-r1",
        language="en",
    )
    manual_role = SpatialRoleAssignment(
        "marginalia",
        RoleAssignmentOrigin.MANUAL,
        "manual-role-r1",
    )
    manual_metadata = ArtifactMetadataAssertion(
        "plate_number",
        4,
        MetadataAssertionOrigin.MANUAL,
        "manual-metadata-r1",
        provenance=ArtifactProvenance(origin="manual"),
    )
    durable_artifact = replace(
        live_artifact,
        revision="artifact-correction-r1",
        category_assignments=(
            *live_artifact.category_assignments,
            manual_category,
        ),
        caption_assertions=(
            *live_artifact.caption_assertions,
            manual_caption,
        ),
        metadata_assertions=(
            *live_artifact.metadata_assertions,
            manual_metadata,
        ),
        role_assignments=(manual_role,),
    )
    durable_annotation = replace(
        live_annotation,
        revision="annotation-correction-r1",
        role_assignments=(
            *live_annotation.role_assignments,
            manual_role,
        ),
    )
    durable = replace(
        live,
        revision="aggregate-correction-r1",
        artifacts=(durable_artifact,),
        annotations=(durable_annotation,),
    )

    assert reconcile_correction_aggregates(live, durable) is durable

    updated_raster = _raster(
        "image-a",
        revision="image-a-r2",
        categories=(
            CategoryAssignment(
                "spine",
                AssignmentOrigin.SUGGESTED,
                "machine-category-r2",
            ),
        ),
        captions=(
            CaptionAssertion(
                "Updated machine caption",
                CaptionOrigin.MACHINE,
                "machine-caption-r2",
            ),
        ),
        metadata=(
            ArtifactMetadataAssertion(
                "plate_number",
                5,
                MetadataAssertionOrigin.MACHINE,
                "machine-metadata-r2",
                provenance=ArtifactProvenance(
                    origin="machine",
                    provider_id="mistral",
                ),
            ),
            ArtifactMetadataAssertion(
                "collection",
                "Herbarium",
                MetadataAssertionOrigin.IMPORTED,
                "imported-metadata-r1",
                provenance=ArtifactProvenance(
                    origin="ocr",
                    provider_id="mistral",
                ),
            ),
        ),
    )
    updated_annotation = _annotation(
        "region-a",
        revision="region-a-r2",
        roles=(
            SpatialRoleAssignment(
                "body",
                RoleAssignmentOrigin.MACHINE,
                "machine-role-r2",
            ),
        ),
        linked_artifact_ids=("image-a",),
    )
    updated_live = _project((updated_raster,), (updated_annotation,))
    reconciled = reconcile_correction_aggregates(updated_live, durable)
    artifact = reconciled.artifact("image-a")
    annotation = reconciled.annotation("region-a")
    assert artifact is not None
    assert annotation is not None

    assert artifact.category(AssignmentOrigin.MANUAL) == manual_category
    assert artifact.category(AssignmentOrigin.SUGGESTED).category == "spine"
    assert artifact.caption(CaptionOrigin.MANUAL) == manual_caption
    assert artifact.caption(CaptionOrigin.MACHINE).text == "Updated machine caption"
    assert (
        artifact.metadata("plate_number", MetadataAssertionOrigin.MANUAL)
        == manual_metadata
    )
    assert (
        artifact.metadata("plate_number", MetadataAssertionOrigin.MACHINE).value
        == 5
    )
    assert (
        artifact.metadata("collection", MetadataAssertionOrigin.IMPORTED).value
        == "Herbarium"
    )
    assert artifact.role(RoleAssignmentOrigin.MANUAL) == manual_role
    assert annotation.role(RoleAssignmentOrigin.MANUAL) == manual_role
    assert annotation.role(RoleAssignmentOrigin.MACHINE).role == "body"
    assert artifact.revision != durable_artifact.revision
    assert annotation.revision != durable_annotation.revision
    assert artifact.extensions["machine_evidence_revision"] == "image-a-r2"
    assert annotation.extensions["machine_evidence_revision"] == "region-a-r2"

    stable = reconcile_correction_aggregates(updated_live, reconciled)
    assert stable is reconciled
    assert stable.revision == reconciled.revision
    assert stable.artifact("image-a").revision == artifact.revision
    assert stable.annotation("region-a").revision == annotation.revision


def test_projection_service_exposes_manual_metadata_without_erasing_machine_evidence():
    raster = _raster(
        "image-a",
        metadata=(
            ArtifactMetadataAssertion(
                "plate_number",
                3,
                MetadataAssertionOrigin.MACHINE,
                "machine-metadata-r1",
                provenance=ArtifactProvenance(
                    origin="machine",
                    provider_id="mistral",
                ),
            ),
        ),
    )
    live = _project((raster,))
    live_artifact = live.artifact("image-a")
    assert live_artifact is not None
    durable = replace(
        live,
        revision="aggregate-correction-r1",
        artifacts=(
            replace(
                live_artifact,
                revision="artifact-correction-r1",
                metadata_assertions=(
                    *live_artifact.metadata_assertions,
                    ArtifactMetadataAssertion(
                        "plate_number",
                        4,
                        MetadataAssertionOrigin.MANUAL,
                        "manual-metadata-r1",
                        provenance=ArtifactProvenance(origin="manual"),
                    ),
                ),
            ),
        ),
    )
    service = CorrectionProjectionService(
        _RasterProjector((raster,)),
        _SpatialProjector(()),
        _ReadRepository(durable),
    )

    projected = service.get_raster_artifact(raster.key)

    assert projected is not None
    assert [
        (value.origin.value, value.value)
        for value in projected.metadata_assertions
    ] == [("machine", 3), ("manual", 4)]
    assert projected.effective_metadata == {"plate_number": 4}
    public = projected.as_dict()
    assert public["effective_metadata"] == {"plate_number": 4}
    assert public["metadata_assertions"][0]["provenance"]["provider_id"] == (
        "mistral"
    )


def test_projection_service_exposes_linked_artifact_role_assignments():
    raster = _raster("image-a")
    live = _project((raster,))
    live_artifact = live.artifact("image-a")
    assert live_artifact is not None
    manual_role = SpatialRoleAssignment(
        "figure",
        RoleAssignmentOrigin.MANUAL,
        "manual-role-r1",
        provenance=ArtifactProvenance(origin="manual"),
    )
    durable = replace(
        live,
        revision="aggregate-correction-r1",
        artifacts=(
            replace(
                live_artifact,
                revision="artifact-correction-r1",
                role_assignments=(manual_role,),
            ),
        ),
    )
    service = CorrectionProjectionService(
        _RasterProjector((raster,)),
        _SpatialProjector(()),
        _ReadRepository(durable),
    )

    projected = service.get_raster_artifact(raster.key)

    assert projected is not None
    assert projected.role_assignments == (manual_role,)
    assert projected.effective_role == "figure"
    assert projected.as_dict()["effective_role"] == "figure"


def test_keyed_projection_skips_aggregate_loading_only_for_stable_absence():
    raster = _raster("image-a")
    aggregate = _project((raster,))
    projector = _RasterProjector((raster,))
    unit = _DurableProbeUnit(aggregate, [False, False])
    service = CorrectionProjectionService(
        projector,
        _SpatialProjector(()),
        _DurableProbeRepository(unit),
    )

    projected = service.get_raster_artifact(raster.key)

    assert projected == raster
    assert projector.get_calls == [raster.key]
    assert projector.list_calls == []
    assert unit.get_calls == []


def test_keyed_projection_falls_back_when_durable_state_appears_mid_read():
    raster = _raster("image-a")
    live = _project((raster,))
    correction = replace(
        live.artifact("image-a"),
        revision="artifact-correction-r9",
    )
    durable = replace(
        live,
        revision="aggregate-correction-r9",
        artifacts=(correction,),
    )
    projector = _RasterProjector((raster,))
    unit = _DurableProbeUnit(durable, [False, True])
    service = CorrectionProjectionService(
        projector,
        _SpatialProjector(()),
        _DurableProbeRepository(unit),
    )

    projected = service.get_raster_artifact(raster.key)

    assert projected is not None
    assert projected.revision == "artifact-correction-r9"
    assert projector.get_calls == [raster.key, raster.key]
    assert projector.list_calls == []
    assert unit.get_calls == ["book-1"]


def test_reconciliation_marks_disappeared_targets_unavailable_until_they_return():
    live = _project(
        (_raster("image-a"),),
        (_annotation("region-a", linked_artifact_ids=("image-a",)),),
    )
    durable_artifact = replace(
        live.artifact("image-a"),
        revision="artifact-correction-r1",
        category_assignments=(
            CategoryAssignment(
                "cover",
                AssignmentOrigin.MANUAL,
                "manual-category-r1",
            ),
        ),
    )
    durable_annotation = replace(
        live.annotation("region-a"),
        revision="annotation-correction-r1",
        role_assignments=(
            SpatialRoleAssignment(
                "figure",
                RoleAssignmentOrigin.MANUAL,
                "manual-role-r1",
            ),
        ),
    )
    durable = replace(
        live,
        revision="aggregate-correction-r1",
        artifacts=(durable_artifact,),
        annotations=(durable_annotation,),
    )
    empty_live = _project(())

    unavailable = reconcile_correction_aggregates(empty_live, durable)
    unavailable_artifact = unavailable.artifact("image-a")
    unavailable_annotation = unavailable.annotation("region-a")
    assert unavailable_artifact is not None
    assert unavailable_annotation is not None
    assert unavailable_artifact.revision != durable_artifact.revision
    assert unavailable_annotation.revision != durable_annotation.revision
    assert unavailable_artifact.extensions[
        CORRECTION_TARGET_AUTHORITY_EXTENSION
    ] == {"state": "missing"}
    assert unavailable_annotation.extensions[
        CORRECTION_TARGET_AUTHORITY_EXTENSION
    ] == {"state": "missing"}
    assert (
        reconcile_correction_aggregates(empty_live, unavailable) is unavailable
    )

    restored = reconcile_correction_aggregates(live, unavailable)
    restored_artifact = restored.artifact("image-a")
    restored_annotation = restored.annotation("region-a")
    assert restored_artifact is not None
    assert restored_annotation is not None
    assert CORRECTION_TARGET_AUTHORITY_EXTENSION not in restored_artifact.extensions
    assert CORRECTION_TARGET_AUTHORITY_EXTENSION not in restored_annotation.extensions
    assert (
        restored_artifact.category(AssignmentOrigin.MANUAL).category == "cover"
    )
    assert (
        restored_annotation.role(RoleAssignmentOrigin.MANUAL).role == "figure"
    )


def test_projection_service_materializes_inheritance_without_hiding_manual_child():
    source = _raster(
        "source",
        categories=(
            CategoryAssignment(
                "other",
                AssignmentOrigin.SUGGESTED,
                "source-suggestion-r1",
            ),
        ),
    )
    inherited_child = _raster(
        "child-inherited",
        lineage=(RasterLineageRef("source", source.revision, "derived_from"),),
        categories=(
            CategoryAssignment(
                "content_specimen",
                AssignmentOrigin.SUGGESTED,
                "child-inherited-suggestion-r1",
            ),
        ),
    )
    manual_child = _raster(
        "child-manual",
        lineage=(RasterLineageRef("source", source.revision, "processed_from"),),
        categories=(
            CategoryAssignment(
                "cover",
                AssignmentOrigin.SUGGESTED,
                "child-manual-suggestion-r1",
            ),
        ),
    )
    raw = (manual_child, inherited_child, source)
    live = _project(raw)
    source_correction = live.artifact("source")
    manual_child_correction = live.artifact("child-manual")
    assert source_correction is not None
    assert manual_child_correction is not None
    durable = replace(
        live,
        revision="aggregate-correction-r1",
        artifacts=tuple(
            replace(
                value,
                revision="source-correction-r1",
                category_assignments=(
                    *value.category_assignments,
                    CategoryAssignment(
                        "title_page",
                        AssignmentOrigin.MANUAL,
                        "source-manual-r1",
                    ),
                ),
            )
            if value.key.artifact_id == "source"
            else replace(
                value,
                revision="child-manual-correction-r1",
                category_assignments=(
                    *value.category_assignments,
                    CategoryAssignment(
                        "spine",
                        AssignmentOrigin.MANUAL,
                        "child-manual-r1",
                    ),
                ),
            )
            if value.key.artifact_id == "child-manual"
            else value
            for value in live.artifacts
        ),
    )
    repository = _ReadRepository(durable)
    service = CorrectionProjectionService(
        _RasterProjector(raw),
        _SpatialProjector(()),
        repository,
    )

    values = {
        value.key.artifact_id: value
        for value in service.list_raster_artifacts("book-1")
    }
    inherited = values["child-inherited"]
    explicit = values["child-manual"]

    assert inherited.effective_category == "title_page"
    assert [
        value.inherited_from_artifact_id
        for value in inherited.category_assignments
        if value.origin is AssignmentOrigin.INHERITED
    ] == ["source"]
    assert explicit.effective_category == "spine"
    assert {value.origin for value in explicit.category_assignments} == {
        AssignmentOrigin.SUGGESTED,
        AssignmentOrigin.MANUAL,
    }
    assert explicit.revision == "child-manual-correction-r1"
    assert repository.operations == ["corrections-query"]


def test_links_are_only_promoted_when_exactly_one_known_artifact_is_linked():
    rasters = (_raster("image-a"), _raster("image-b"))
    annotations = (
        _annotation(
            "single",
            linked_artifact_ids=("image-a",),
        ),
        _annotation(
            "ambiguous",
            roles=(
                SpatialRoleAssignment(
                    "figure",
                    RoleAssignmentOrigin.MACHINE,
                    "machine-role-r1",
                ),
            ),
            linked_artifact_ids=("image-a", "image-b"),
        ),
        _annotation(
            "missing",
            linked_artifact_ids=("not-in-projection",),
        ),
    )
    live = _project(rasters, annotations)

    assert live.annotation("single").linked_artifact_id == "image-a"
    assert live.annotation("ambiguous").linked_artifact_id == ""
    assert live.annotation("missing").linked_artifact_id == ""
    assert (
        live.annotation("ambiguous").extensions["correction_link_authority"]["state"]
        == "ambiguous"
    )
    assert (
        live.annotation("missing").extensions["correction_link_authority"]["state"]
        == "missing"
    )

    ambiguous = live.annotation("ambiguous")
    assert ambiguous is not None
    durable = replace(
        live,
        revision="aggregate-correction-r1",
        annotations=tuple(
            replace(
                value,
                revision="ambiguous-correction-r1",
                role_assignments=(
                    *value.role_assignments,
                    SpatialRoleAssignment(
                        "marginalia",
                        RoleAssignmentOrigin.MANUAL,
                        "manual-role-r1",
                    ),
                ),
            )
            if value.key.annotation_id == "ambiguous"
            else value
            for value in live.annotations
        ),
    )
    service = CorrectionProjectionService(
        _RasterProjector(rasters),
        _SpatialProjector(annotations),
        _ReadRepository(durable),
    )

    values = {
        value.key.annotation_id: value
        for value in service.list_spatial_annotations("book-1")
    }
    assert values["ambiguous"].effective_role == "marginalia"
    assert values["ambiguous"].linked_artifact_ids == (
        "image-a",
        "image-b",
    )
    assert values["missing"].linked_artifact_ids == ("not-in-projection",)


def test_projection_service_delegates_raster_resource_resolution():
    artifact = _raster("image-a")
    resolved = object()
    raster_projector = _RasterProjector((artifact,), resolved=resolved)
    service = CorrectionProjectionService(
        raster_projector,
        _SpatialProjector(()),
        _ReadRepository(_project((artifact,))),
    )

    result = service.resolve_raster_resource(
        "book-1",
        artifact.resource,
    )

    assert result is resolved
    assert raster_projector.resolve_calls == [
        ("book-1", artifact.resource),
    ]
