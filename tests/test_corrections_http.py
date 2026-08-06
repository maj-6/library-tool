from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from flask import Flask

from librarytool.adapters.filesystem import (
    FilesystemCorrectionRepository,
    RecoverableWriteSet,
)
from librarytool.engine.correction_projection import (
    CorrectionAggregateProjector,
    CorrectionProjectionService,
    reconcile_correction_aggregates,
)
from librarytool.engine.corrections import (
    CorrectionAuditEvent,
    CorrectionReviewSnapshot,
    CorrectionService,
)
from librarytool.engine.correction_transforms import (
    CommittedCorrectionOutput,
    CorrectionTransformCommand,
    CorrectionTransformService,
)
from librarytool.engine.correction_ocr import (
    CORRECTION_OCR_PROPOSAL_POLICY,
    CORRECTION_REOCR_OPERATION_PREFIX,
    CorrectionOcrFollowupService,
    CorrectionOcrProposalCatalogService,
    CorrectionOcrProposalCatalogSnapshot,
    CorrectionOcrProposalProviderView,
    CorrectionOcrProposalSummaryView,
    CorrectionOcrProposalView,
    CorrectionOcrProviderSelection,
    CorrectionOcrRecognition,
    CorrectionReocrService,
    StoredCorrectionOcrProposal,
)
from librarytool.engine.jobs import JobManager
from librarytool.engine.items import ItemView, WorkbenchState
from librarytool.engine.raster_artifacts import (
    ArtifactMetadataAssertion,
    ArtifactFreshness,
    ArtifactProvenance,
    CaptionAssertion,
    CaptionOrigin,
    MetadataAssertionOrigin,
    RasterArtifactKey,
    RasterLineageRef,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from librarytool.engine.runtime import (
    CORRECTION_CAPTION_SERVICE,
    CORRECTION_METADATA_SERVICE,
    CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE,
    CORRECTION_OCR_PROPOSAL_QUERY_SERVICE,
    CORRECTION_REOCR_SERVICE,
    CORRECTION_REVIEW_SERVICE,
    CORRECTION_SERVICE,
    CORRECTION_TRANSFORM_SERVICE,
    ITEM_QUERY_SERVICE,
    RASTER_ARTIFACT_QUERY_SERVICE,
    SPATIAL_ANNOTATION_QUERY_SERVICE,
)
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialSourceRef,
)
import librarytool_http.corrections as corrections_http
from librarytool_http.corrections import create_corrections_blueprint


def _raster(
    artifact_id: str,
    *,
    content: bytes = b"png",
    canvas_id: str = "page-1",
    kind: str = "capture",
    extensions=None,
) -> RasterArtifactView:
    revision = f"artifact-{artifact_id}-r1"
    digest = hashlib.sha256(content).hexdigest()
    return RasterArtifactView(
        key=RasterArtifactKey("book-1", artifact_id),
        revision=revision,
        kind=kind,
        media_type="image/png",
        content_sha256=digest,
        dimensions=RasterDimensions(40, 60),
        source=RasterSourceRef(
            "capture",
            "capture-r1",
            canvas_id,
            f"{canvas_id}-r1",
        ),
        resource_state=ResourceState.AVAILABLE,
        resource=RasterResourceRef(
            f"capture:{artifact_id}",
            f"resource-{artifact_id}-r1",
        ),
        label=f"Capture {artifact_id}",
        freshness=ArtifactFreshness.CURRENT,
        provenance=ArtifactProvenance(origin="capture"),
        extensions=extensions or {},
    )


def _annotation(
    annotation_id: str,
    *,
    canvas_revision: str = "page-1-r1",
) -> SpatialAnnotationView:
    return SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", annotation_id),
        revision=f"annotation-{annotation_id}-r1",
        source=SpatialSourceRef(
            "capture",
            "capture-r1",
            "page-1",
            canvas_revision,
        ),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            canvas_revision,
            (
                NormalizedPoint(0.1, 0.1),
                NormalizedPoint(0.9, 0.1),
                NormalizedPoint(0.9, 0.9),
                NormalizedPoint(0.1, 0.9),
            ),
        ),
        label=f"Region {annotation_id}",
        freshness=ArtifactFreshness.CURRENT,
        provenance=ArtifactProvenance(origin="mistral"),
    )


class _RasterProjector:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def list_raster_artifacts(self, item_id):
        return tuple(row for row in self.rows if row.key.item_id == item_id)

    def get_raster_artifact(self, key):
        return next((row for row in self.rows if row.key == key), None)


class _SpatialProjector:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.filters = []

    def list_spatial_annotations(
        self,
        item_id,
        *,
        representation_id="",
        canvas_id="",
    ):
        self.filters.append((representation_id, canvas_id))
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


class _Items:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def list_items(self):
        return self.rows


def _item(item_id="book-1", title="A Herbal"):
    return ItemView(
        item_id=item_id,
        revision=f"{item_id}-r1",
        record_revision=f"{item_id}-record-r1",
        kind="book",
        title=title,
        metadata={},
        representations=(),
        artifacts=(),
        workbench_state=WorkbenchState(
            item_id,
            f"{item_id}-workbench-r1",
            {},
        ),
    )


class _Engine:
    def __init__(
        self,
        raster,
        spatial,
        corrections=None,
        transforms=None,
        ocr_proposals=None,
        ocr_proposal_catalog=None,
        reocr=None,
        items=None,
    ):
        self.services = {
            RASTER_ARTIFACT_QUERY_SERVICE: raster,
            SPATIAL_ANNOTATION_QUERY_SERVICE: spatial,
        }
        if corrections is not None:
            for key in (
                CORRECTION_CAPTION_SERVICE,
                CORRECTION_METADATA_SERVICE,
                CORRECTION_REVIEW_SERVICE,
                CORRECTION_SERVICE,
            ):
                self.services[key] = corrections
        if transforms is not None:
            self.services[CORRECTION_TRANSFORM_SERVICE] = transforms
        if ocr_proposals is not None:
            self.services[CORRECTION_OCR_PROPOSAL_QUERY_SERVICE] = (
                ocr_proposals
            )
        if ocr_proposal_catalog is not None:
            self.services[CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE] = (
                ocr_proposal_catalog
            )
        if reocr is not None:
            self.services[CORRECTION_REOCR_SERVICE] = reocr
        if items is not None:
            self.services[ITEM_QUERY_SERVICE] = items

    def get_service(self, key):
        return self.services.get(key)


@dataclass(frozen=True)
class _Resolved:
    stream: io.BytesIO
    media_type: str
    content_sha256: str
    size: int
    revision: str


class _Resolver:
    def __init__(self, artifact, path):
        self.artifact = artifact
        self.path = path
        self.calls = []

    def resolve_raster_resource(self, item_id, resource):
        self.calls.append((item_id, resource))
        return _Resolved(
            io.BytesIO(self.path.read_bytes()),
            self.artifact.media_type,
            self.artifact.content_sha256,
            self.path.stat().st_size,
            resource.revision,
        )


def _app(
    engine,
    resolver=None,
    transform_submitter=None,
    reocr_submitter=None,
    actor_id_for_request=None,
    workspace_id_for_request=None,
    item_service_for_request=None,
    lazy_capture_index_for_item=None,
):
    app = Flask(__name__)
    app.register_blueprint(
        create_corrections_blueprint(
            lambda: engine,
            raster_resource_resolver_for_request=(
                None if resolver is None else lambda: resolver
            ),
            correction_item_service_for_request=item_service_for_request,
            correction_lazy_capture_index_for_item=(
                lazy_capture_index_for_item
            ),
            correction_actor_id_for_request=actor_id_for_request,
            correction_workspace_id_for_request=workspace_id_for_request,
            correction_transform_submitter=transform_submitter,
            correction_reocr_submitter=reocr_submitter,
        )
    )
    return app


class _Revisions:
    def __init__(self):
        self.value = 0

    def __call__(self, kind, target_id):
        self.value += 1
        return f"{kind}-{target_id}-mutation-{self.value}"


def _mutation_app(
    tmp_path,
    *,
    linked_artifact_ids=("figure-1",),
):
    return _projected_app(
        tmp_path,
        (
            _raster("image-1"),
            _raster("figure-1", kind="extracted-figure"),
        ),
        (
            replace(
                _annotation("region-1"),
                linked_artifact_ids=linked_artifact_ids,
            ),
        ),
    )


def _projected_app(tmp_path, raster_rows, spatial_rows):
    app, _repository = _projected_harness(
        tmp_path,
        raster_rows,
        spatial_rows,
    )
    return app


def _projected_harness(
    tmp_path,
    raster_rows,
    spatial_rows,
    *,
    items=None,
    actor_id_for_request=None,
    item_service_for_request=None,
):
    rasters = _RasterProjector(raster_rows)
    spatial = _SpatialProjector(spatial_rows)
    aggregate = CorrectionAggregateProjector(rasters, spatial)
    repository = FilesystemCorrectionRepository(
        RecoverableWriteSet(tmp_path / "workspace"),
        load_aggregate=aggregate.project,
        reconcile_aggregate=reconcile_correction_aggregates,
        revision_factory=_Revisions(),
        recover=False,
    )
    projected = CorrectionProjectionService(
        rasters,
        spatial,
        repository,
    )
    corrections = CorrectionService(repository)
    return _app(
        _Engine(
            projected,
            projected,
            corrections,
            items=items,
        ),
        actor_id_for_request=actor_id_for_request,
        item_service_for_request=item_service_for_request,
    ), repository


class _ReviewService:
    def __init__(self, review):
        self.review = review
        self.calls = []

    def get_review(self, item_id):
        self.calls.append(item_id)
        return self.review


class _OcrProposalService:
    def __init__(self, proposal):
        self.proposal = proposal
        self.calls = []

    def get_proposal(self, item_id, proposal_ref):
        self.calls.append((item_id, proposal_ref))
        if (
            self.proposal is None
            or self.proposal.item_id != item_id
            or self.proposal.proposal_ref != proposal_ref
        ):
            return None
        return self.proposal


class _ItemService:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def list_items(self):
        return self.rows


def _book_item(
    item_id: str = "book-1",
    title: str = "Book One",
    *,
    kind: str = "book",
) -> ItemView:
    return ItemView(
        item_id=item_id,
        revision=f"{item_id}-r1",
        record_revision=f"{item_id}-record-r1",
        kind=kind,
        title=title,
        metadata={},
        representations=(),
        artifacts=(),
        workbench_state=WorkbenchState(
            item_id=item_id,
            revision=f"{item_id}-workbench-r1",
            readiness={},
        ),
    )


def _review_with_history(count, revision):
    state = "clear"
    history = []
    reason = "Caption needs checking"
    for index in range(count):
        if state == "clear":
            action = "attention.mark"
            after = "needs_attention"
        elif state == "needs_attention":
            action = "attention.resolve"
            after = "resolved"
        else:
            action = "attention.reopen"
            after = "needs_attention"
        history.append(
            CorrectionAuditEvent(
                operation_id=f"review-audit-{index:04d}",
                action=action,
                actor_id="curator-1",
                occurred_at=f"2026-07-23T12:{index:02d}:00+00:00",
                before_state=state,
                after_state=after,
                reason=reason,
                comment=f"Audit event {index}",
            )
        )
        state = after
    return CorrectionReviewSnapshot(
        revision=revision,
        state=state,
        reason="" if state == "clear" else reason,
        history=tuple(history),
    )


def test_raster_list_is_versioned_filterable_and_revision_paged():
    rows = (
        _raster("image-b", canvas_id="page-2"),
        _raster("image-a"),
        _raster("image-c"),
        _raster("processed-a", kind="processed-image"),
        _raster("future-a", kind="ai-upscaled-image"),
    )
    client = _app(
        _Engine(_RasterProjector(rows), _SpatialProjector(()))
    ).test_client()

    first = client.get(
        "/api/v1/items/book-1/raster-artifacts?limit=1"
        "&representation_id=capture&canvas_id=page-1"
        "&group=source-images"
    )
    assert first.status_code == 200
    payload = first.get_json()
    assert payload["schema"] == "librarytool.raster-artifacts/1"
    assert payload["item_id"] == "book-1"
    assert payload["total"] == 2
    assert [row["key"]["artifact_id"] for row in payload["artifacts"]] == [
        "image-a"
    ]
    assert payload["next_cursor"]
    assert Path(rows[0].resource.resource_id).is_absolute() is False

    second = client.get(
        "/api/v1/items/book-1/raster-artifacts?limit=1"
        "&representation_id=capture&canvas_id=page-1"
        "&group=source-images"
        f"&cursor={payload['next_cursor']}"
    )
    assert second.status_code == 200
    assert [
        row["key"]["artifact_id"] for row in second.get_json()["artifacts"]
    ] == ["image-c"]
    assert second.get_json()["next_cursor"] is None

    cached = client.get(
        "/api/v1/items/book-1/raster-artifacts"
        "?limit=1&representation_id=capture&canvas_id=page-1"
        "&group=source-images"
        f"&cursor={payload['next_cursor']}",
        headers={"If-None-Match": first.headers["ETag"]},
    )
    assert cached.status_code == 304
    invalid = client.get(
        "/api/v1/items/book-1/raster-artifacts?group=not-a-group"
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_raster_artifact_group"
    future = client.get(
        "/api/v1/items/book-1/raster-artifacts"
        "?group=generated-images&limit=10"
    )
    assert future.status_code == 200
    assert [
        value["key"]["artifact_id"]
        for value in future.get_json()["artifacts"]
    ] == ["future-a"]
    mixed_case_client = _app(
        _Engine(
            _RasterProjector((_raster("scan-a", kind="SCAN"),)),
            _SpatialProjector(()),
        )
    ).test_client()
    mixed_case = mixed_case_client.get(
        "/api/v1/items/book-1/raster-artifacts?group=source-images"
    )
    assert mixed_case.status_code == 200
    assert mixed_case.get_json()["total"] == 1


def test_collection_cursor_rejects_a_different_snapshot():
    projector = _RasterProjector((_raster("image-a"), _raster("image-b")))
    engine = _Engine(projector, _SpatialProjector(()))
    client = _app(engine).test_client()
    cursor = client.get(
        "/api/v1/items/book-1/raster-artifacts?limit=1"
    ).get_json()["next_cursor"]

    projector.rows = (*projector.rows, _raster("image-c"))
    response = client.get(
        f"/api/v1/items/book-1/raster-artifacts?limit=1&cursor={cursor}"
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "corrections_collection_changed"


def test_raster_and_spatial_details_are_conditional_and_missing_is_explicit():
    raster = _raster("image-a")
    spatial = _annotation("region-a")
    projector = _SpatialProjector(
        (spatial, _annotation("stale-region", canvas_revision="page-1-r0"))
    )
    client = _app(
        _Engine(_RasterProjector((raster,)), projector)
    ).test_client()

    raster_response = client.get(
        "/api/v1/items/book-1/raster-artifacts/image-a"
    )
    assert raster_response.status_code == 200
    assert raster_response.get_json()["artifact"]["resource"] == {
        "id": "capture:image-a",
        "revision": "resource-image-a-r1",
        "variant": "display",
    }
    assert client.get(
        "/api/v1/items/book-1/raster-artifacts/missing"
    ).status_code == 404

    spatial_response = client.get(
        "/api/v1/items/book-1/spatial-annotations"
        "?representation_id=capture&canvas_id=page-1"
        "&canvas_revision=page-1-r1"
    )
    assert spatial_response.status_code == 200
    assert spatial_response.get_json()["total"] == 1
    assert spatial_response.get_json()["annotations"][0]["key"] == {
        "item_id": "book-1",
        "annotation_id": "region-a",
    }
    assert projector.filters == [("capture", "page-1")]

    detail = client.get(
        "/api/v1/items/book-1/spatial-annotations/region-a"
    )
    assert detail.status_code == 200
    assert detail.get_json()["schema"] == "librarytool.spatial-annotation/1"
    assert client.get(
        "/api/v1/items/book-1/spatial-annotations/missing"
    ).status_code == 404


def test_raster_resource_requires_its_pin_and_never_serializes_a_path(tmp_path):
    content = b"\x89PNG\r\n\x1a\nprivate-raster"
    artifact = _raster("image-a", content=content)
    path = tmp_path / "private-image.png"
    path.write_bytes(content)
    resolver = _Resolver(artifact, path)
    client = _app(
        _Engine(_RasterProjector((artifact,)), _SpatialProjector(())),
        resolver,
    ).test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-a/resource"

    required = client.get(endpoint)
    assert required.status_code == 428
    assert required.get_json()["code"] == "raster_resource_revision_required"

    stale = client.get(f"{endpoint}?revision=old")
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "raster_resource_revision_conflict"
    assert resolver.calls == []

    response = client.get(
        f"{endpoint}?revision={artifact.resource.revision}"
    )
    assert response.status_code == 200
    assert response.data == content
    assert response.mimetype == "image/png"
    assert response.headers["X-Resource-Revision"] == (
        artifact.resource.revision
    )
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert str(path).encode("utf-8") not in response.data
    assert resolver.calls == [("book-1", artifact.resource)]

    ranged = client.get(
        f"{endpoint}?revision={artifact.resource.revision}",
        headers={"Range": "bytes=2-4"},
    )
    assert ranged.status_code == 200
    assert ranged.data == content
    assert ranged.content_length == len(content)

    cached = client.get(
        f"{endpoint}?revision={artifact.resource.revision}",
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert cached.status_code == 304


def test_raster_preview_uses_bytes_only_resolver_without_artifact_projection():
    content = b"\x89PNG\r\n\x1a\npreview-bytes"
    digest = hashlib.sha256(content).hexdigest()

    class ExplodingProjector:
        def list_raster_artifacts(self, _item_id):
            pytest.fail("preview listed authoritative artifacts")

        def get_raster_artifact(self, _key):
            pytest.fail("preview projected authoritative artifact detail")

    class PreviewResolver:
        def __init__(self):
            self.calls = []

        def resolve_capture_preview(self, item_id, artifact_id):
            self.calls.append((item_id, artifact_id))
            return _Resolved(
                io.BytesIO(content),
                "image/png",
                digest,
                len(content),
                f"bytes:{digest}",
            )

    resolver = PreviewResolver()
    client = _app(
        _Engine(ExplodingProjector(), _SpatialProjector(())),
        resolver,
    ).test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-a/preview"

    response = client.get(endpoint)

    assert response.status_code == 200
    assert response.data == content
    assert response.mimetype == "image/png"
    assert response.headers["X-Resource-Revision"] == f"bytes:{digest}"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Digest"].startswith("sha-256=:")
    assert response.cache_control.private is True
    assert response.cache_control.no_cache is True
    assert resolver.calls == [("book-1", "image-a")]

    rejected = client.get(f"{endpoint}?revision=not-accepted")
    assert rejected.status_code == 400
    assert resolver.calls == [("book-1", "image-a")]


def test_invalid_preview_resolution_closes_its_owned_stream():
    stream = io.BytesIO(b"invalid preview metadata")

    class InvalidPreviewResolver:
        def resolve_capture_preview(self, _item_id, _artifact_id):
            return _Resolved(
                stream,
                "text/plain",
                "not-a-digest",
                len(stream.getvalue()),
                "preview-r1",
            )

    client = _app(
        _Engine(_RasterProjector(()), _SpatialProjector(())),
        InvalidPreviewResolver(),
    ).test_client()
    response = client.get(
        "/api/v1/items/book-1/raster-artifacts/image-a/preview"
    )

    assert response.status_code == 500
    assert response.get_json()["code"] == "invalid_raster_resource_resolution"
    assert stream.closed is True


def test_invalid_resolver_result_closes_its_owned_stream():
    artifact = _raster("image-a")
    stream = io.BytesIO(b"invalid metadata")

    class InvalidResolver:
        def resolve_raster_resource(self, _item_id, resource):
            return _Resolved(
                stream,
                "image/jpeg",
                artifact.content_sha256,
                len(stream.getvalue()),
                resource.revision,
            )

    client = _app(
        _Engine(_RasterProjector((artifact,)), _SpatialProjector(())),
        InvalidResolver(),
    ).test_client()

    response = client.get(
        "/api/v1/items/book-1/raster-artifacts/image-a/resource"
        f"?revision={artifact.resource.revision}"
    )

    assert response.status_code == 500
    assert response.get_json()["code"] == "invalid_raster_resource_resolution"
    assert stream.closed is True


def test_category_mutation_replays_and_converges_through_query_projection(
    tmp_path,
):
    client = _mutation_app(tmp_path).test_client()
    headers = {
        "Idempotency-Key": "category-op-1",
        "If-Artifact-Match": '"artifact-image-1-r1"',
    }

    first = client.put(
        "/api/v1/items/book-1/raster-artifacts/image-1/category",
        json={"category": "title_page"},
        headers=headers,
    )
    replay = client.put(
        "/api/v1/items/book-1/raster-artifacts/image-1/category",
        json={"category": "title_page"},
        headers=headers,
    )

    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    body = first.get_json()
    assert body["schema"] == "librarytool.correction-mutation-receipt/1"
    assert body["replayed"] is False
    assert "command_sha256" not in body["receipt"]
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == body["receipt"]

    detail = client.get("/api/v1/items/book-1/raster-artifacts/image-1").get_json()[
        "artifact"
    ]
    assert detail["effective_category"] == "title_page"
    assert detail["revision"] == body["receipt"]["targets"][0]["after_revision"]

    stale = client.put(
        "/api/v1/items/book-1/raster-artifacts/image-1/category",
        json={"category": "cover"},
        headers={
            "Idempotency-Key": "category-op-2",
            "If-Artifact-Match": '"artifact-image-1-r1"',
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "artifact_revision_conflict"


def test_manual_caption_transport_replays_and_preserves_machine_evidence(
    tmp_path,
):
    machine = CaptionAssertion(
        "Machine caption",
        CaptionOrigin.MACHINE,
        "caption-machine-r1",
        language="en",
        confidence=0.81,
        provenance=ArtifactProvenance(origin="mistral"),
    )
    client = _projected_app(
        tmp_path,
        (
            replace(_raster("image-1"), caption_assertions=(machine,)),
            _raster("figure-1", kind="extracted-figure"),
        ),
        (replace(_annotation("region-1"), linked_artifact_ids=("figure-1",)),),
    ).test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-1/caption"
    headers = {
        "Idempotency-Key": "caption-set-op",
        "If-Artifact-Match": '"artifact-image-1-r1"',
    }

    first = client.put(
        endpoint,
        json={"text": "Human correction", "language": "en"},
        headers=headers,
    )
    replay = client.put(
        endpoint,
        json={"text": "Human correction", "language": "en"},
        headers=headers,
    )

    assert first.status_code == replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == first.get_json()["receipt"]
    receipt = first.get_json()["receipt"]
    assert receipt["action"] == "caption.set"
    assert receipt["inverse"] == {
        "action": "caption.clear",
        "expected_aggregate_revision": receipt["after_aggregate_revision"],
        "expected_targets": receipt["targets"],
        "payload": {"artifact_id": "image-1"},
    }

    detail = client.get(
        "/api/v1/items/book-1/raster-artifacts/image-1"
    ).get_json()["artifact"]
    assert {
        (value["origin"], value["text"])
        for value in detail["caption_assertions"]
    } == {
        ("manual", "Human correction"),
        ("machine", "Machine caption"),
    }
    assert detail["effective_caption"]["origin"] == "manual"

    cleared = client.delete(
        endpoint,
        json={},
        headers={
            "Idempotency-Key": "caption-clear-op",
            "If-Artifact-Match": f'"{detail["revision"]}"',
        },
    )
    assert cleared.status_code == 200
    assert cleared.get_json()["receipt"]["inverse"]["action"] == "caption.set"
    refreshed = client.get(
        "/api/v1/items/book-1/raster-artifacts/image-1"
    ).get_json()["artifact"]
    assert [(value["origin"], value["text"]) for value in refreshed[
        "caption_assertions"
    ]] == [("machine", "Machine caption")]
    assert refreshed["effective_caption"]["origin"] == "machine"


def test_manual_caption_transport_rejects_overlong_valid_language_tag(tmp_path):
    client = _projected_app(
        tmp_path,
        (
            _raster("image-1"),
            _raster("figure-1", kind="extracted-figure"),
        ),
        (replace(_annotation("region-1"), linked_artifact_ids=("figure-1",)),),
    ).test_client()
    language = "en-" + "-".join(["abcdef"] * 10)

    response = client.put(
        "/api/v1/items/book-1/raster-artifacts/image-1/caption",
        json={"text": "Human correction", "language": language},
        headers={
            "Idempotency-Key": "caption-language-too-long",
            "If-Artifact-Match": '"artifact-image-1-r1"',
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_caption_assertion"


def test_metadata_transport_is_conditional_replay_safe_and_reversible(tmp_path):
    machine = ArtifactMetadataAssertion(
        "plate_number",
        3,
        MetadataAssertionOrigin.MACHINE,
        "metadata-machine-r1",
        provenance=ArtifactProvenance(
            origin="ocr",
            provider_id="mistral",
        ),
    )
    rows = (
        replace(_raster("image-1"), metadata_assertions=(machine,)),
        _raster("figure-1", kind="extracted-figure"),
    )
    spatial = (
        replace(_annotation("region-1"), linked_artifact_ids=("figure-1",)),
    )
    app, _repository = _projected_harness(tmp_path, rows, spatial)
    first_client = app.test_client()
    second_client = app.test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-1/metadata"
    artifact_endpoint = "/api/v1/items/book-1/raster-artifacts/image-1"
    first_view = first_client.get(artifact_endpoint).get_json()["artifact"]
    second_view = second_client.get(artifact_endpoint).get_json()["artifact"]
    assert first_view["revision"] == second_view["revision"]
    assert first_view["effective_metadata"] == {"plate_number": 3}
    headers = {
        "Idempotency-Key": "metadata-assert-op",
        "If-Artifact-Match": f'"{first_view["revision"]}"',
    }
    document = {
        "assertions": {"caption_source": "manual", "plate_number": 7},
        "clear_names": [],
    }

    first = first_client.patch(endpoint, json=document, headers=headers)
    replay = first_client.patch(endpoint, json=document, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    receipt = first.get_json()["receipt"]
    assert receipt["action"] == "metadata.assert"
    assert receipt["inverse"]["payload"] == {
        "artifact_id": "image-1",
        "restore_assertions": [],
        "clear_names": ["caption_source", "plate_number"],
    }
    converged = second_client.get(artifact_endpoint).get_json()["artifact"]
    assert {
        (value["name"], value["origin"], value["value"])
        for value in converged["metadata_assertions"]
    } == {
        ("caption_source", "manual", "manual"),
        ("plate_number", "machine", 3),
        ("plate_number", "manual", 7),
    }
    assert converged["effective_metadata"] == {
        "caption_source": "manual",
        "plate_number": 7,
    }

    stale = second_client.patch(
        endpoint,
        json={"assertions": {"plate_number": 8}, "clear_names": []},
        headers={
            "Idempotency-Key": "metadata-stale-op",
            "If-Artifact-Match": f'"{second_view["revision"]}"',
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "artifact_revision_conflict"

    current_revision = receipt["targets"][0]["after_revision"]
    cleared = second_client.patch(
        endpoint,
        json={"assertions": {}, "clear_names": ["plate_number"]},
        headers={
            "Idempotency-Key": "metadata-clear-op",
            "If-Artifact-Match": f'"{current_revision}"',
        },
    )
    assert cleared.status_code == 200
    restored = cleared.get_json()["receipt"]["inverse"]["payload"][
        "restore_assertions"
    ]
    assert [(value["name"], value["value"]) for value in restored] == [
        ("plate_number", 7)
    ]
    revealed = first_client.get(artifact_endpoint).get_json()["artifact"]
    assert revealed["effective_metadata"] == {
        "caption_source": "manual",
        "plate_number": 3,
    }
    assert {
        (value["origin"], value["value"])
        for value in revealed["metadata_assertions"]
        if value["name"] == "plate_number"
    } == {("machine", 3)}


def test_metadata_capacity_is_explicit_and_reserves_imported_evidence(tmp_path):
    imported = tuple(
        ArtifactMetadataAssertion(
            f"imported_{index:03d}",
            index,
            MetadataAssertionOrigin.IMPORTED,
            f"metadata-imported-{index}",
            provenance=ArtifactProvenance(
                origin="ocr",
                provider_id="mistral",
            ),
        )
        for index in range(64)
    )
    app, _repository = _projected_harness(
        tmp_path,
        (replace(_raster("image-1"), metadata_assertions=imported),),
        (),
    )
    client = app.test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-1"
    revision = client.get(endpoint).get_json()["artifact"]["revision"]
    assertions = {
        f"manual_{index:03d}": index for index in range(64)
    }

    accepted = client.patch(
        f"{endpoint}/metadata",
        json={"assertions": assertions, "clear_names": []},
        headers={
            "Idempotency-Key": "metadata-capacity-accepted",
            "If-Artifact-Match": f'"{revision}"',
        },
    )

    assert accepted.status_code == 200
    projected = client.get(endpoint).get_json()["artifact"]
    assert len(projected["metadata_assertions"]) == 128
    assert projected["effective_metadata"]["manual_063"] == 63


def test_metadata_capacity_conflict_is_typed_before_repository_stage(tmp_path):
    imported = tuple(
        ArtifactMetadataAssertion(
            f"imported_{index:03d}",
            index,
            MetadataAssertionOrigin.IMPORTED,
            f"metadata-imported-{index}",
        )
        for index in range(128)
    )
    app, _repository = _projected_harness(
        tmp_path,
        (replace(_raster("image-1"), metadata_assertions=imported),),
        (),
    )
    client = app.test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-1"
    revision = client.get(endpoint).get_json()["artifact"]["revision"]

    rejected = client.patch(
        f"{endpoint}/metadata",
        json={"assertions": {"manual_override": True}, "clear_names": []},
        headers={
            "Idempotency-Key": "metadata-capacity-rejected",
            "If-Artifact-Match": f'"{revision}"',
        },
    )

    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == (
        "metadata_assertion_capacity_conflict"
    )
    assert rejected.get_json()["details"] == {
        "artifact_id": "image-1",
        "capacity": 128,
        "required": 129,
    }


def test_metadata_effective_value_budget_conflict_is_typed_and_non_mutating(
    tmp_path,
):
    large_value = ["x" * 8192, "y" * 8192]
    imported = ArtifactMetadataAssertion(
        "imported_large",
        large_value,
        MetadataAssertionOrigin.IMPORTED,
        "metadata-imported-large",
    )
    app, _repository = _projected_harness(
        tmp_path,
        (
            replace(
                _raster("image-1"),
                metadata_assertions=(imported,),
            ),
        ),
        (),
    )
    client = app.test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/image-1"
    before = client.get(endpoint).get_json()["artifact"]

    rejected = client.patch(
        f"{endpoint}/metadata",
        json={"assertions": {"manual_large": large_value}, "clear_names": []},
        headers={
            "Idempotency-Key": "metadata-effective-budget-rejected",
            "If-Artifact-Match": f'"{before["revision"]}"',
        },
    )

    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == (
        "metadata_assertion_capacity_conflict"
    )
    assert rejected.get_json()["details"] == {
        "artifact_id": "image-1",
        "capacity": 128,
        "required": 2,
        "cause_code": "invalid_artifact_extensions",
    }
    after = client.get(endpoint).get_json()["artifact"]
    assert after == before


def test_review_transitions_pin_replay_and_retain_audit_history(tmp_path):
    rows = (
        _raster("image-1"),
        _raster("figure-1", kind="extracted-figure"),
    )
    spatial = (
        replace(_annotation("region-1"), linked_artifact_ids=("figure-1",)),
    )
    app, _repository = _projected_harness(tmp_path, rows, spatial)
    first_client = app.test_client()
    second_client = app.test_client()
    endpoint = "/api/v1/items/book-1/corrections/review"
    initial = first_client.get(endpoint)
    initial_body = initial.get_json()
    initial_review = initial_body["review"]
    assert initial.status_code == 200
    assert initial.headers["ETag"] == f'"{initial_review["revision"]}"'
    assert initial_body == {
        "ok": True,
        "schema": "librarytool.correction-review/1",
        "item_id": "book-1",
        "review": {
            "revision": initial_review["revision"],
            "state": "clear",
            "reason": "",
            "history_count": 0,
            "history_tail": [],
        },
    }
    assert second_client.get(
        endpoint,
        headers={"If-None-Match": initial.headers["ETag"]},
    ).status_code == 304

    mark_headers = {
        "Idempotency-Key": "review-mark-op",
        "If-Review-Match": f'"{initial_review["revision"]}"',
    }
    marked = first_client.put(
        f"{endpoint}/attention",
        json={
            "reason": "Caption needs checking",
            "comment": "Found during QA",
        },
        headers=mark_headers,
    )
    replay = first_client.put(
        f"{endpoint}/attention",
        json={
            "reason": "Caption needs checking",
            "comment": "Found during QA",
        },
        headers=mark_headers,
    )
    assert marked.status_code == replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    mark_receipt = marked.get_json()["receipt"]
    assert mark_receipt["inverse"]["action"] == "attention.clear"
    assert mark_receipt["targets"][0]["kind"] == "review"

    stale_mark = second_client.put(
        f"{endpoint}/attention",
        json={
            "reason": "A stale second opinion",
            "comment": "",
        },
        headers={
            "Idempotency-Key": "review-stale-mark-op",
            "If-Review-Match": f'"{initial_review["revision"]}"',
        },
    )
    assert stale_mark.status_code == 409
    assert stale_mark.get_json()["code"] == "review_revision_conflict"

    observed_mark = second_client.get(endpoint).get_json()["review"]
    assert observed_mark["revision"] == mark_receipt["targets"][0]["after_revision"]
    assert observed_mark["state"] == "needs_attention"
    assert observed_mark["history_count"] == 1
    assert [event["action"] for event in observed_mark["history_tail"]] == [
        "attention.mark"
    ]

    resolved = second_client.post(
        f"{endpoint}/resolve",
        json={"comment": "Caption corrected"},
        headers={
            "Idempotency-Key": "review-resolve-op",
            "If-Review-Match": (
                f'"{observed_mark["revision"]}"'
            ),
        },
    )
    assert resolved.status_code == 200
    resolve_receipt = resolved.get_json()["receipt"]
    assert resolve_receipt["inverse"]["action"] == "attention.reopen"

    stale_resolve = first_client.post(
        f"{endpoint}/resolve",
        json={"comment": ""},
        headers={
            "Idempotency-Key": "review-stale-resolve-op",
            "If-Review-Match": f'"{observed_mark["revision"]}"',
        },
    )
    assert stale_resolve.status_code == 409
    assert stale_resolve.get_json()["code"] == "review_revision_conflict"

    observed_resolution = first_client.get(endpoint).get_json()["review"]
    assert observed_resolution["state"] == "resolved"
    reopened = first_client.post(
        f"{endpoint}/reopen",
        json={"comment": "Second review requested"},
        headers={
            "Idempotency-Key": "review-reopen-op",
            "If-Review-Match": (
                f'"{observed_resolution["revision"]}"'
            ),
        },
    )
    assert reopened.status_code == 200
    assert reopened.get_json()["receipt"]["inverse"]["action"] == (
        "attention.resolve"
    )

    converged = second_client.get(endpoint).get_json()["review"]
    assert converged["state"] == "needs_attention"
    assert converged["reason"] == "Caption needs checking"
    assert converged["history_count"] == 3
    assert [event["action"] for event in converged["history_tail"]] == [
        "attention.mark",
        "attention.resolve",
        "attention.reopen",
    ]
    assert {
        event["actor_id"] for event in converged["history_tail"]
    } == {"local-desktop"}
    history_endpoint = f"{endpoint}/history"
    first_page = second_client.get(
        history_endpoint,
        query_string={"limit": 2},
        headers={"If-Review-Match": f'"{converged["revision"]}"'},
    )
    first_history = first_page.get_json()
    assert first_page.status_code == 200
    assert first_page.headers["ETag"] == f'"{converged["revision"]}"'
    assert first_history == {
        "ok": True,
        "schema": "librarytool.correction-review-history/1",
        "item_id": "book-1",
        "review_revision": converged["revision"],
        "review_state": "needs_attention",
        "events": first_history["events"],
        "next_cursor": first_history["next_cursor"],
        "total": 3,
    }
    assert [event["action"] for event in first_history["events"]] == [
        "attention.mark",
        "attention.resolve",
    ]
    assert first_history["next_cursor"]
    final_page = first_client.get(
        history_endpoint,
        query_string={
            "limit": 2,
            "cursor": first_history["next_cursor"],
        },
        headers={"If-Review-Match": f'"{converged["revision"]}"'},
    )
    assert final_page.status_code == 200
    assert [event["action"] for event in final_page.get_json()["events"]] == [
        "attention.reopen"
    ]
    assert final_page.get_json()["next_cursor"] is None
    assert first_client.get(history_endpoint).status_code == 428
    stale_history = first_client.get(
        history_endpoint,
        headers={"If-Review-Match": f'"{initial_review["revision"]}"'},
    )
    assert stale_history.status_code == 409
    assert stale_history.get_json()["code"] == "review_revision_conflict"

    stale = second_client.post(
        f"{endpoint}/resolve",
        json={"comment": ""},
        headers={
            "Idempotency-Key": "review-stale-op",
            "If-Review-Match": f'"{initial_review["revision"]}"',
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "review_revision_conflict"


def test_books_index_and_review_detail_project_one_engine_snapshot(tmp_path):
    rows = (
        _raster(
            "capture:asset-1:original",
            extensions={"capture_order": 1},
        ),
        _raster(
            "capture:asset-1:display",
            extensions={"capture_order": 1},
        ),
    )
    captured_item = _item(title="Captured Herbal")
    captured_item = replace(
        captured_item,
        workbench_state=WorkbenchState(
            item_id=captured_item.item_id,
            revision="book-1-workbench-capture-r1",
            readiness={"source": "missing", "text": "missing"},
            issues=("representation.missing", "text.missing"),
        ),
    )
    app, _repository = _projected_harness(
        tmp_path,
        rows,
        (),
        items=_Items((captured_item,)),
        actor_id_for_request=lambda: "account-42",
    )
    client = app.test_client()

    initial = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    assert initial.status_code == 200
    body = initial.get_json()
    assert body["schema"] == "librarytool.corrections-index/2"
    assert body["attention"] == []
    assert len(body["books"]) == 1
    book = body["books"][0]
    assert book["kind"] == "book"
    assert book["title"] == "Captured Herbal"
    assert book["issues"] == ["text.missing"]
    assert [
        capture["artifact_id"] for capture in book["captures"]
    ] == ["capture:asset-1:display"]
    capture = book["captures"][0]
    assert capture["capture_order"] == 1
    assert capture["thumbnail"]["url"].startswith(
        "/api/v1/items/book-1/raster-artifacts/"
    )
    assert "workspace" not in initial.get_data(as_text=True).casefold()

    detail = client.get("/api/v1/items/book-1/corrections/review")
    assert detail.status_code == 200
    review = detail.get_json()["review"]
    assert review["state"] == "clear"
    assert review["history_count"] == 0
    assert review["history_tail"] == []
    cached = client.get(
        "/api/v1/items/book-1/corrections/review",
        headers={"If-None-Match": detail.headers["ETag"]},
    )
    assert cached.status_code == 304

    marked = client.put(
        "/api/v1/items/book-1/corrections/review/attention",
        json={"reason": "Check the title leaf", "comment": ""},
        headers={
            "Idempotency-Key": "index-review-mark",
            "If-Review-Match": f'"{review["revision"]}"',
        },
    )
    assert marked.status_code == 200
    updated = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    ).get_json()
    assert updated["revision"] != body["revision"]
    assert updated["books"][0]["revision"] != book["revision"]
    assert updated["books"][0]["review"]["state"] == "needs_attention"
    assert updated["attention"][0]["target"] == {
        "kind": "book",
        "item_id": "book-1",
    }
    assert updated["attention"][0]["review"]["latest_event"][
        "actor_id"
    ] == "account-42"


def test_corrections_index_can_use_a_dedicated_item_projection(tmp_path):
    raster = replace(
        _raster("capture-only"),
        key=RasterArtifactKey("capture-stable-id", "capture-only"),
        extensions={"capture_order": 0},
    )
    engine_items = _Items((_item("book-global", "Global catalogue"),))
    corrections_items = _Items(
        (
            replace(
                _item("capture-stable-id", "Phone capture"),
                kind="capture",
            ),
        )
    )
    app, _repository = _projected_harness(
        tmp_path,
        (raster,),
        (),
        items=engine_items,
        item_service_for_request=lambda: corrections_items,
    )

    response = app.test_client().get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200
    assert [
        (item["id"], item["kind"])
        for item in response.get_json()["books"]
    ] == [("capture-stable-id", "capture")]


def test_lazy_index_returns_capture_shell_before_preview_bytes_are_resolved():
    content = b"\x89PNG\r\n\x1a\nlazy-preview"
    digest = hashlib.sha256(content).hexdigest()

    class HintOnlyRasterProjector:
        def list_capture_index_hints(self, item_id):
            assert item_id == "book-1"
            return ({
                "artifact_id": "capture:asset-1:display",
                "revision": "index:capture-asset-1-r1",
                "capture_order": 0,
                "label": "Front cover",
                "representation_id": "capture:book-1",
                "canvas_id": "capture:asset-1",
                "effective_category": "cover",
                "resource_state": "available",
                "import_state": "ready",
                "freshness": "current",
                "imported_at": "2026-08-05T12:00:00Z",
            },)

        def list_raster_artifacts(self, _item_id):
            pytest.fail("lazy capture index hydrated authoritative detail")

        def get_raster_artifact(self, _key):
            pytest.fail("lazy capture index hydrated an artifact")

    class PreviewResolver:
        def __init__(self):
            self.calls = []

        def resolve_capture_preview(self, item_id, artifact_id):
            self.calls.append((item_id, artifact_id))
            return _Resolved(
                io.BytesIO(content),
                "image/png",
                digest,
                len(content),
                f"bytes:{digest}",
            )

    resolver = PreviewResolver()
    reviews = _ReviewService(_review_with_history(0, "review-clear-r1"))
    engine = _Engine(
        HintOnlyRasterProjector(),
        _SpatialProjector(()),
        corrections=reviews,
        items=_ItemService((_book_item(),)),
    )
    client = _app(
        engine,
        resolver,
        lazy_capture_index_for_item=lambda item_id: item_id == "book-1",
    ).test_client()

    index = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert index.status_code == 200, index.get_json()
    capture = index.get_json()["books"][0]["captures"][0]
    assert capture["artifact_id"] == "capture:asset-1:display"
    assert capture["revision"].startswith("index:")
    assert capture["thumbnail"]["url"].endswith("/preview")
    assert resolver.calls == []
    assert reviews.calls == []

    preview = client.get(capture["thumbnail"]["url"])
    assert preview.status_code == 200
    assert preview.data == content
    assert resolver.calls == [("book-1", "capture:asset-1:display")]


def test_corrections_index_includes_books_and_capture_entries_only():
    review = _review_with_history(1, "review-attention-r1")
    reviews = _ReviewService(review)
    engine = _Engine(
        _RasterProjector(()),
        _SpatialProjector(()),
        reviews,
        items=_ItemService(
            (
                _book_item("book-1", "Built Book"),
                _book_item(
                    "capture-1",
                    "Captured Entry",
                    kind="capture",
                ),
                _book_item(
                    "collection-1",
                    "Collection",
                    kind="collection",
                ),
            )
        ),
    )

    response = _app(engine).test_client().get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [
        (item["id"], item["kind"]) for item in body["books"]
    ] == [
        ("book-1", "book"),
        ("capture-1", "capture"),
    ]
    assert reviews.calls == ["book-1", "capture-1"]
    assert {
        entry["target"]["item_id"]: entry["target"]["kind"]
        for entry in body["attention"]
    } == {
        "book-1": "book",
        "capture-1": "book",
    }


def test_corrections_index_projects_capture_import_timestamps():
    reviews = _ReviewService(_review_with_history(0, "review-clear-r1"))
    rows = (
        _raster(
            "capture:asset-1:display",
            extensions={
                "capture_order": 0,
                "imported_at": "2026-07-28T09:00:00+00:00",
            },
        ),
        _raster(
            "capture:asset-2:display",
            extensions={
                "capture_order": 1,
                "imported_at": "2026-07-30T10:15:00Z",
            },
        ),
        _raster(
            "capture:asset-3:display",
            extensions={"capture_order": 2},
        ),
        _raster(
            "capture:asset-4:display",
            extensions={"capture_order": 3, "imported_at": "not a timestamp"},
        ),
    )
    engine = _Engine(
        _RasterProjector(rows),
        _SpatialProjector(()),
        reviews,
        items=_ItemService((_book_item("book-1", "Captured Herbal"),)),
    )

    response = _app(engine).test_client().get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200
    book = response.get_json()["books"][0]
    assert [
        capture["imported_at"] for capture in book["captures"]
    ] == [
        "2026-07-28T09:00:00+00:00",
        "2026-07-30T10:15:00Z",
        "",
        "",
    ]
    assert book["latest_imported_at"] == "2026-07-30T10:15:00Z"


def test_corrections_index_tolerates_imports_without_timestamps():
    reviews = _ReviewService(_review_with_history(0, "review-clear-r1"))
    engine = _Engine(
        _RasterProjector(
            (_raster("capture-only", extensions={"capture_order": 0}),)
        ),
        _SpatialProjector(()),
        reviews,
        items=_ItemService((_book_item("book-1", "Older Import"),)),
    )

    response = _app(engine).test_client().get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200
    book = response.get_json()["books"][0]
    assert book["captures"][0]["imported_at"] == ""
    assert book["latest_imported_at"] == ""


def test_review_actor_is_server_owned_and_client_spoofing_is_rejected(
    tmp_path,
):
    app, _repository = _projected_harness(
        tmp_path,
        (_raster("image-1"),),
        (),
        actor_id_for_request=lambda: "trusted-user-1",
    )
    client = app.test_client()
    endpoint = "/api/v1/items/book-1/corrections/review"
    initial = client.get(endpoint).get_json()["review"]
    headers = {
        "Idempotency-Key": "review-server-actor-op",
        "If-Review-Match": f'"{initial["revision"]}"',
    }

    spoofed = client.put(
        f"{endpoint}/attention",
        json={
            "reason": "Check the title leaf",
            "actor_id": "spoofed-client",
            "comment": "",
        },
        headers=headers,
    )

    assert spoofed.status_code == 400
    assert spoofed.get_json()["code"] == "invalid_correction_mutation_envelope"
    assert client.get(endpoint).get_json()["review"]["state"] == "clear"

    marked = client.put(
        f"{endpoint}/attention",
        json={"reason": "Check the title leaf", "comment": ""},
        headers=headers,
    )
    observed = client.get(endpoint).get_json()["review"]

    assert marked.status_code == 200
    assert observed["history_tail"][-1]["actor_id"] == "trusted-user-1"


def test_corrections_index_rejects_ambiguous_or_inactive_workspace_scope():
    app = _app(
        _Engine(_RasterProjector(()), _SpatialProjector(())),
        workspace_id_for_request=lambda: "workspace-authority",
    )
    client = app.test_client()

    missing = client.get("/api/v1/corrections/index")
    repeated = client.get(
        "/api/v1/corrections/index"
        "?workspace_id=workspace-authority&workspace_id=workspace-other"
    )
    mismatched = client.get(
        "/api/v1/corrections/index?workspace_id=workspace-other"
    )

    assert missing.status_code == 400
    assert missing.get_json()["code"] == "invalid_corrections_workspace"
    assert repeated.status_code == 400
    assert repeated.get_json()["code"] == "invalid_corrections_workspace"
    assert mismatched.status_code == 409
    assert mismatched.get_json()["code"] == "corrections_workspace_mismatch"


def test_corrections_index_enforces_aggregate_capture_budget(monkeypatch):
    rasters = tuple(
        replace(
            _raster(f"image-{index}"),
            key=RasterArtifactKey(
                f"book-{index}",
                f"image-{index}",
            ),
            extensions={"capture_order": index},
        )
        for index in range(1, 3)
    )
    engine = _Engine(
        _RasterProjector(rasters),
        _SpatialProjector(()),
        _ReviewService(CorrectionReviewSnapshot("review-r1")),
        items=_ItemService(
            (
                _book_item("book-1", "Book One"),
                _book_item("book-2", "Book Two"),
            )
        ),
    )
    monkeypatch.setattr(
        corrections_http,
        "CORRECTIONS_INDEX_TOTAL_CAPTURE_LIMIT",
        1,
    )

    response = _app(engine).test_client().get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 500
    assert response.get_json()["code"] == (
        "invalid_corrections_index_projection"
    )
    assert response.get_json()["details"] == {
        "item_id": "book-2",
        "maximum_captures": 1,
    }


def test_corrections_index_enforces_serialized_size_budget(monkeypatch):
    raster = replace(
        _raster("image-1"),
        extensions={"capture_order": 0},
    )
    engine = _Engine(
        _RasterProjector((raster,)),
        _SpatialProjector(()),
        _ReviewService(CorrectionReviewSnapshot("review-r1")),
        items=_ItemService((_book_item(),)),
    )
    monkeypatch.setattr(
        corrections_http,
        "CORRECTIONS_INDEX_MAX_BYTES",
        128,
    )

    response = _app(engine).test_client().get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 500
    assert response.get_json()["code"] == (
        "invalid_corrections_index_projection"
    )
    assert response.get_json()["details"] == {
        "item_id": "book-1",
        "maximum_bytes": 128,
    }


def test_review_snapshot_bounds_tail_and_history_pages_pin_exact_revision():
    first_review = _review_with_history(25, "review-long-r1")
    service = _ReviewService(first_review)
    client = _app(
        _Engine(
            _RasterProjector(()),
            _SpatialProjector(()),
            service,
        )
    ).test_client()
    endpoint = "/api/v1/items/book-1/corrections/review"

    summary_response = client.get(endpoint)
    summary = summary_response.get_json()["review"]
    assert summary_response.status_code == 200
    assert summary["revision"] == first_review.revision
    assert summary["history_count"] == 25
    assert len(summary["history_tail"]) == 8
    assert [
        event["operation_id"] for event in summary["history_tail"]
    ] == [
        event.operation_id for event in first_review.history[-8:]
    ]
    assert "history" not in summary

    history_endpoint = f"{endpoint}/history"
    cursor = None
    first_cursor = None
    events = []
    while True:
        response = client.get(
            history_endpoint,
            query_string={
                "limit": 7,
                **({"cursor": cursor} if cursor else {}),
            },
            headers={
                "If-Review-Match": f'"{first_review.revision}"',
            },
        )
        assert response.status_code == 200
        page = response.get_json()
        assert page["review_revision"] == first_review.revision
        assert page["review_state"] == first_review.state.value
        assert page["total"] == 25
        assert len(page["events"]) <= 7
        events.extend(page["events"])
        cursor = page["next_cursor"]
        if first_cursor is None:
            first_cursor = cursor
        if cursor is None:
            break
    assert [event["operation_id"] for event in events] == [
        event.operation_id for event in first_review.history
    ]

    oversized = client.get(
        history_endpoint,
        query_string={"limit": 101},
        headers={"If-Review-Match": f'"{first_review.revision}"'},
    )
    assert oversized.status_code == 400
    assert oversized.get_json()["code"] == "invalid_corrections_page_limit"

    second_review = _review_with_history(26, "review-long-r2")
    service.review = second_review
    changed = client.get(
        history_endpoint,
        query_string={"limit": 7, "cursor": first_cursor},
        headers={"If-Review-Match": f'"{first_review.revision}"'},
    )
    assert changed.status_code == 409
    assert changed.get_json()["code"] == "review_revision_conflict"
    assert changed.get_json()["details"] == {
        "actual_revision": second_review.revision,
        "expected_revision": first_review.revision,
    }

    mixed_cursor = client.get(
        history_endpoint,
        query_string={"limit": 7, "cursor": first_cursor},
        headers={"If-Review-Match": f'"{second_review.revision}"'},
    )
    assert mixed_cursor.status_code == 409
    assert mixed_cursor.get_json()["code"] == "review_revision_conflict"


def test_inherited_category_changes_detail_etag_without_changing_child_cas(
    tmp_path,
):
    source = _raster("source-1")
    child = replace(
        _raster("child-1", kind="processed-image"),
        lineage=(
            RasterLineageRef(
                "source-1",
                source.revision,
                "processed_from",
            ),
        ),
    )
    client = _projected_app(
        tmp_path,
        (source, child),
        (),
    ).test_client()
    endpoint = "/api/v1/items/book-1/raster-artifacts/child-1"
    before = client.get(endpoint)
    child_revision = before.get_json()["artifact"]["revision"]

    mutation = client.put(
        "/api/v1/items/book-1/raster-artifacts/source-1/category",
        json={"category": "cover"},
        headers={
            "Idempotency-Key": "source-category-op",
            "If-Artifact-Match": f'"{source.revision}"',
        },
    )
    after = client.get(
        endpoint,
        headers={"If-None-Match": before.headers["ETag"]},
    )

    assert mutation.status_code == 200
    assert after.status_code == 200
    assert after.headers["ETag"] != before.headers["ETag"]
    assert after.get_json()["artifact"]["effective_category"] == "cover"
    assert after.get_json()["artifact"]["revision"] == child_revision


def test_linked_region_role_mutates_annotation_and_artifact_atomically(
    tmp_path,
):
    client = _mutation_app(tmp_path).test_client()

    response = client.put(
        "/api/v1/items/book-1/spatial-annotations/region-1/role",
        json={"role": "figure", "linked_artifact_id": "figure-1"},
        headers={
            "Idempotency-Key": "role-op-1",
            "If-Annotation-Match": '"annotation-region-1-r1"',
            "If-Linked-Artifact-Match": '"artifact-figure-1-r1"',
        },
    )

    assert response.status_code == 200
    receipt = response.get_json()["receipt"]
    assert [(target["kind"], target["target_id"]) for target in receipt["targets"]] == [
        ("annotation", "region-1"),
        ("artifact", "figure-1"),
    ]
    annotation = client.get(
        "/api/v1/items/book-1/spatial-annotations/region-1"
    ).get_json()["annotation"]
    artifact = client.get(
        "/api/v1/items/book-1/raster-artifacts/figure-1"
    ).get_json()["artifact"]
    assert annotation["effective_role"] == "figure"
    assert annotation["revision"] == receipt["targets"][0]["after_revision"]
    assert artifact["effective_role"] == "figure"
    assert artifact["role_assignments"][0]["origin"] == "manual"
    assert artifact["revision"] == receipt["targets"][1]["after_revision"]


def test_introduced_figure_link_survives_refresh_and_clears_atomically(
    tmp_path,
):
    client = _mutation_app(
        tmp_path,
        linked_artifact_ids=(),
    ).test_client()
    assign = client.put(
        "/api/v1/items/book-1/spatial-annotations/region-1/role",
        json={"role": "figure", "linked_artifact_id": "figure-1"},
        headers={
            "Idempotency-Key": "role-introduce-op",
            "If-Annotation-Match": '"annotation-region-1-r1"',
            "If-Linked-Artifact-Match": '"artifact-figure-1-r1"',
        },
    )
    assert assign.status_code == 200

    annotation = client.get(
        "/api/v1/items/book-1/spatial-annotations/region-1"
    ).get_json()["annotation"]
    figure = client.get("/api/v1/items/book-1/raster-artifacts/figure-1").get_json()[
        "artifact"
    ]
    assert annotation["linked_artifact_ids"] == ["figure-1"]
    assert figure["effective_role"] == "figure"
    clear = client.delete(
        "/api/v1/items/book-1/spatial-annotations/region-1/role",
        json={"linked_artifact_id": "figure-1"},
        headers={
            "Idempotency-Key": "role-clear-op",
            "If-Annotation-Match": f'"{annotation["revision"]}"',
            "If-Linked-Artifact-Match": f'"{figure["revision"]}"',
        },
    )

    assert clear.status_code == 200
    assert [
        (target["kind"], target["target_id"])
        for target in clear.get_json()["receipt"]["targets"]
    ] == [
        ("annotation", "region-1"),
        ("artifact", "figure-1"),
    ]
    refreshed = client.get(
        "/api/v1/items/book-1/spatial-annotations/region-1"
    ).get_json()["annotation"]
    assert refreshed["effective_role"] == ""
    assert refreshed["linked_artifact_ids"] == ["figure-1"]
    refreshed_figure = client.get(
        "/api/v1/items/book-1/raster-artifacts/figure-1"
    ).get_json()["artifact"]
    assert refreshed_figure["effective_role"] == ""
    assert refreshed_figure["role_assignments"] == []


def test_ambiguous_region_links_are_readable_but_not_mutable(tmp_path):
    client = _mutation_app(
        tmp_path,
        linked_artifact_ids=("image-1", "figure-1"),
    ).test_client()
    detail = client.get("/api/v1/items/book-1/spatial-annotations/region-1")
    assert detail.status_code == 200
    assert detail.get_json()["annotation"]["linked_artifact_ids"] == [
        "figure-1",
        "image-1",
    ]

    mutation = client.put(
        "/api/v1/items/book-1/spatial-annotations/region-1/role",
        json={"role": "figure", "linked_artifact_id": "figure-1"},
        headers={
            "Idempotency-Key": "ambiguous-role-op",
            "If-Annotation-Match": '"annotation-region-1-r1"',
            "If-Linked-Artifact-Match": '"artifact-figure-1-r1"',
        },
    )

    assert mutation.status_code == 409
    assert mutation.get_json()["code"] == "linked_artifact_authority_conflict"


def test_correction_mutation_transport_rejects_ambiguous_documents(tmp_path):
    client = _mutation_app(tmp_path).test_client()
    path = "/api/v1/items/book-1/raster-artifacts/image-1/category"

    missing_operation = client.put(
        path,
        data='{"category":"cover"}',
        content_type="application/json",
        headers={"If-Artifact-Match": '"artifact-image-1-r1"'},
    )
    missing_revision = client.put(
        path,
        data='{"category":"cover"}',
        content_type="application/json",
        headers={"Idempotency-Key": "category-op-missing-revision"},
    )
    duplicate = client.put(
        path,
        data='{"category":"cover","category":"spine"}',
        content_type="application/json",
        headers={
            "Idempotency-Key": "category-op-duplicate",
            "If-Artifact-Match": '"artifact-image-1-r1"',
        },
    )
    extra = client.put(
        path,
        json={"category": "cover", "provenance": {"origin": "forged"}},
        headers={
            "Idempotency-Key": "category-op-extra",
            "If-Artifact-Match": '"artifact-image-1-r1"',
        },
    )

    assert missing_operation.status_code == 428
    assert missing_operation.get_json()["code"] == "idempotency_key_required"
    assert missing_revision.status_code == 428
    assert missing_revision.get_json()["code"] == "correction_target_revision_required"
    assert duplicate.status_code == 400
    assert duplicate.get_json()["code"] == "invalid_correction_mutation_document"
    assert extra.status_code == 400
    assert extra.get_json()["code"] == "invalid_correction_mutation_envelope"


def _ocr_proposal_view(
    *,
    item_id="book-1",
    proposal_ref="cop-" + "a" * 40,
):
    source = CommittedCorrectionOutput(
        "ocr-ready",
        "corrected-ocr-ready",
        "corrected-r1",
        "b" * 64,
    )
    return CorrectionOcrProposalView(
        proposal_ref=proposal_ref,
        item_id=item_id,
        operation_id="transform-op-1",
        source=source,
        provider=CorrectionOcrProposalProviderView(
            "mistral",
            "mistral-ocr-latest",
        ),
        recognition={
            "text": "Machine proposal",
            "regions": [{"role": "marginalia"}],
        },
        publication_policy=CORRECTION_OCR_PROPOSAL_POLICY,
        content_sha256="c" * 64,
    )


def test_ocr_proposal_resource_is_versioned_item_scoped_and_private():
    proposal = _ocr_proposal_view()
    service = _OcrProposalService(proposal)
    client = _app(
        _Engine(
            _RasterProjector(()),
            _SpatialProjector(()),
            ocr_proposals=service,
        )
    ).test_client()
    path = (
        "/api/v1/items/book-1/ocr-proposals/"
        f"{proposal.proposal_ref}"
    )

    response = client.get(path)

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "schema": "librarytool.correction-ocr-proposal/1",
        "proposal": proposal.as_dict(),
    }
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["ETag"] == f'"{proposal.content_sha256}"'
    assert service.calls == [("book-1", proposal.proposal_ref)]
    serialized = response.get_data(as_text=True).casefold()
    assert "credential" not in serialized
    assert "storage_path" not in serialized
    assert "options" not in serialized

    wrong_item = client.get(
        "/api/v1/items/another-book/ocr-proposals/"
        f"{proposal.proposal_ref}"
    )
    assert wrong_item.status_code == 404
    assert wrong_item.get_json()["code"] == "correction_ocr_proposal_not_found"


def test_ocr_proposal_resource_rejects_invalid_refs_and_missing_module():
    proposal = _ocr_proposal_view()
    service = _OcrProposalService(proposal)
    client = _app(
        _Engine(
            _RasterProjector(()),
            _SpatialProjector(()),
            ocr_proposals=service,
        )
    ).test_client()

    invalid = client.get(
        "/api/v1/items/book-1/ocr-proposals/not-an-opaque-ref"
    )
    missing = _app(
        _Engine(_RasterProjector(()), _SpatialProjector(()))
    ).test_client().get(
        "/api/v1/items/book-1/ocr-proposals/"
        f"{proposal.proposal_ref}"
    )

    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_correction_ocr_proposal_ref"
    assert service.calls == []
    assert missing.status_code == 503
    assert missing.get_json()["code"] == (
        "correction_ocr_proposal_module_unavailable"
    )


def _transform_command(artifact, operation_id="transform-op-1"):
    return CorrectionTransformCommand(
        item_id=artifact.key.item_id,
        artifact_id=artifact.key.artifact_id,
        artifact_revision=artifact.revision,
        source_revision=artifact.resource.revision,
        source_sha256=artifact.content_sha256,
        quad=((0, 0), (1, 0), (1, 1), (0, 1)),
        operation_id=operation_id,
    )


def test_transform_queue_is_versioned_idempotent_and_schedules_outside_http():
    artifact = _raster("image-a")
    jobs = JobManager(checkpoint_interval=0)
    transforms = CorrectionTransformService(jobs)
    submitted = []

    def submitter(service, command, queued):
        submitted.append((service, command, queued))

    client = _app(
        _Engine(
            _RasterProjector((artifact,)),
            _SpatialProjector(()),
            transforms=transforms,
        ),
        transform_submitter=submitter,
    ).test_client()
    command = _transform_command(artifact)
    path = "/api/v1/items/book-1/raster-artifacts/image-a/transforms"
    headers = {
        "Idempotency-Key": command.operation_id,
        "If-Artifact-Match": f'"{artifact.revision}"',
    }

    first = client.post(path, json=command.as_dict(), headers=headers)
    replay = client.post(path, json=command.as_dict(), headers=headers)

    assert first.status_code == 202
    assert replay.status_code == 200
    body = first.get_json()
    assert body["schema"] == (
        "librarytool.correction-transform-queue-receipt/1"
    )
    assert body["replayed"] is False
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["job_id"] == body["job_id"]
    assert body["job"]["kind"] == "correction.transform"
    assert body["job"]["input_revisions"] == {
        "artifact_id": artifact.key.artifact_id,
        "artifact_revision": artifact.revision,
        "operation_id": command.operation_id,
        "source_revision": artifact.resource.revision,
        "source_sha256": artifact.content_sha256,
        "transform": {
            "quad": [list(point) for point in command.quad],
            "adjustment": (
                command.adjustment.as_dict()
                if command.adjustment is not None
                else None
            ),
            "rerun_ocr": command.rerun_ocr,
        },
    }
    assert "command_sha256" not in str(body)
    assert first.headers["Location"] == (
        f"/api/v1/jobs/{body['job_id']}"
    )
    assert first.headers["Cache-Control"] == "no-store"
    assert len(jobs.list()) == 1
    assert [value[2].created for value in submitted] == [True, False]


def test_transform_queue_rejects_envelope_mismatch_before_registration():
    artifact = _raster("image-a")
    jobs = JobManager()
    transforms = CorrectionTransformService(jobs)
    client = _app(
        _Engine(
            _RasterProjector((artifact,)),
            _SpatialProjector(()),
            transforms=transforms,
        ),
        transform_submitter=lambda *_args: None,
    ).test_client()
    command = _transform_command(artifact)
    payload = command.as_dict()
    payload["artifact_revision"] = "different-revision"

    response = client.post(
        "/api/v1/items/book-1/raster-artifacts/image-a/transforms",
        json=payload,
        headers={
            "Idempotency-Key": command.operation_id,
            "If-Artifact-Match": f'"{artifact.revision}"',
        },
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == (
        "correction_transform_envelope_mismatch"
    )
    assert jobs.list() == []


def test_transform_queue_requires_a_process_executor_before_registration():
    artifact = _raster("image-a")
    jobs = JobManager()
    client = _app(
        _Engine(
            _RasterProjector((artifact,)),
            _SpatialProjector(()),
            transforms=CorrectionTransformService(jobs),
        )
    ).test_client()
    command = _transform_command(artifact)

    response = client.post(
        "/api/v1/items/book-1/raster-artifacts/image-a/transforms",
        json=command.as_dict(),
        headers={
            "Idempotency-Key": command.operation_id,
            "If-Artifact-Match": f'"{artifact.revision}"',
        },
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == (
        "correction_transform_executor_unavailable"
    )
    assert jobs.list() == []


def test_ocr_proposal_list_is_versioned_and_revision_pinned():
    first = _ocr_proposal_view(proposal_ref="cop-" + "a" * 40)
    second = _ocr_proposal_view(proposal_ref="cop-" + "b" * 40)
    snapshot = CorrectionOcrProposalCatalogSnapshot(
        item_id="book-1",
        proposals=(
            CorrectionOcrProposalSummaryView.from_proposal(first),
            CorrectionOcrProposalSummaryView.from_proposal(second),
        ),
    )

    class _CatalogRepository:
        def list_proposals(self, item_id):
            assert item_id == "book-1"
            return snapshot

    client = _app(
        _Engine(
            _RasterProjector(()),
            _SpatialProjector(()),
            ocr_proposal_catalog=CorrectionOcrProposalCatalogService(
                _CatalogRepository()
            ),
        )
    ).test_client()

    response = client.get("/api/v1/items/book-1/ocr-proposals")

    assert response.status_code == 200
    body = response.get_json()
    assert body["schema"] == "librarytool.correction-ocr-proposals/1"
    assert body["item_id"] == "book-1"
    assert body["snapshot_revision"] == snapshot.snapshot_revision
    assert [row["proposal_ref"] for row in body["proposals"]] == [
        first.proposal_ref,
        second.proposal_ref,
    ]
    assert body["next_cursor"] is None
    assert body["total"] == 2
    assert response.headers["ETag"] == f'"{snapshot.snapshot_revision}"'
    serialized = response.get_data(as_text=True).casefold()
    assert "recognition" not in serialized
    assert "options" not in serialized

    paged = client.get("/api/v1/items/book-1/ocr-proposals?limit=1")
    paged_body = paged.get_json()
    assert [row["proposal_ref"] for row in paged_body["proposals"]] == [
        first.proposal_ref
    ]
    assert paged_body["next_cursor"]
    rest = client.get(
        "/api/v1/items/book-1/ocr-proposals",
        query_string={"cursor": paged_body["next_cursor"], "limit": 1},
    )
    assert [row["proposal_ref"] for row in rest.get_json()["proposals"]] == [
        second.proposal_ref
    ]

    stale = client.get(
        "/api/v1/items/book-1/ocr-proposals",
        query_string={"snapshot_revision": "cops-" + "0" * 64},
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == (
        "correction_ocr_proposal_catalog_changed"
    )

    missing = _app(
        _Engine(_RasterProjector(()), _SpatialProjector(()))
    ).test_client().get("/api/v1/items/book-1/ocr-proposals")
    assert missing.status_code == 503
    assert missing.get_json()["code"] == (
        "correction_ocr_proposal_catalog_module_unavailable"
    )


class _ReocrProposalRepository:
    def __init__(self, content):
        self.content = content
        self.stored = {}

    def read_source(self, request):
        return self.content

    def find_proposal(self, request):
        return self.stored.get(request.operation_id)

    def commit_proposal(self, request, recognition):
        return self.stored.setdefault(
            request.operation_id,
            StoredCorrectionOcrProposal(
                f"proposal-{len(self.stored) + 1}",
                request.source,
                CorrectionOcrProviderSelection(
                    recognition.provider_id,
                    recognition.model,
                    recognition.options,
                ),
            ),
        )


class _ReocrProvider:
    def select_provider(self):
        return CorrectionOcrProviderSelection("tesseract", "5.4")

    def recognize(self, selection, content, hooks):
        return CorrectionOcrRecognition(
            selection.provider_id,
            selection.model,
            {"text": "machine"},
            selection.options,
        )


def _reocr_harness(content=b"ocr ready bytes"):
    display = _raster(
        "ctr-display-1",
        kind="corrected-image",
        content=b"display bytes",
        extensions={
            "correction_transform": {
                "operation_id": "transform-op-1",
                "output_kind": "corrected-display",
            }
        },
    )
    ocr_ready = _raster(
        "ctr-ocr-1",
        kind="processed-source",
        content=content,
        extensions={
            "correction_transform": {
                "operation_id": "transform-op-1",
                "output_kind": "ocr-ready",
            }
        },
    )
    projector = _RasterProjector((display, ocr_ready, _raster("image-1")))
    jobs = JobManager(checkpoint_interval=0)
    followup = CorrectionOcrFollowupService(
        jobs,
        _ReocrProposalRepository(content),
        _ReocrProvider(),
    )
    return display, ocr_ready, jobs, CorrectionReocrService(
        followup,
        projector,
    ), projector


def test_reocr_queue_is_versioned_idempotent_and_schedules_outside_http():
    display, ocr_ready, jobs, reocr, projector = _reocr_harness()
    submitted = []
    client = _app(
        _Engine(projector, _SpatialProjector(()), reocr=reocr),
        reocr_submitter=lambda service, queued: submitted.append(
            (service, queued)
        ),
    ).test_client()
    path = "/api/v1/items/book-1/raster-artifacts/ctr-display-1/reocr"
    headers = {
        "Idempotency-Key": "reocr-key-1",
        "If-Artifact-Match": f'"{display.revision}"',
    }

    first = client.post(path, json={}, headers=headers)
    replay = client.post(path, json={}, headers=headers)

    assert first.status_code == 202
    assert replay.status_code == 200
    body = first.get_json()
    assert body["schema"] == "librarytool.correction-reocr-queue-receipt/1"
    assert body["replayed"] is False
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["job_id"] == body["job_id"]
    assert body["operation_id"].startswith(CORRECTION_REOCR_OPERATION_PREFIX)
    assert body["job"]["kind"] == "correction.ocr-followup"
    assert body["source"] == {
        "kind": "ocr-ready",
        "artifact_id": "ctr-ocr-1",
        "artifact_revision": ocr_ready.revision,
        "content_sha256": ocr_ready.content_sha256,
    }
    assert body["job"]["input_revisions"] == {
        "parent_operation_id": body["operation_id"],
        "artifact_id": "ctr-ocr-1",
        "artifact_revision": ocr_ready.revision,
        "source_sha256": ocr_ready.content_sha256,
        "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
    }
    assert "command_sha256" not in str(body)
    assert first.headers["Location"] == f"/api/v1/jobs/{body['job_id']}"
    assert first.headers["Cache-Control"] == "no-store"
    assert len(jobs.list()) == 1
    assert [value[1].created for value in submitted] == [True, False]


def test_reocr_queue_rejects_missing_headers_and_invalid_targets():
    display, _ocr_ready, jobs, reocr, projector = _reocr_harness()
    client = _app(
        _Engine(projector, _SpatialProjector(()), reocr=reocr),
        reocr_submitter=lambda *_args: None,
    ).test_client()
    path = "/api/v1/items/book-1/raster-artifacts/ctr-display-1/reocr"
    revision = {"If-Artifact-Match": f'"{display.revision}"'}

    missing_key = client.post(path, json={}, headers=revision)
    missing_revision = client.post(
        path,
        json={},
        headers={"Idempotency-Key": "reocr-key-1"},
    )
    extra_field = client.post(
        path,
        json={"provider": "forged"},
        headers={"Idempotency-Key": "reocr-key-1", **revision},
    )
    stale = client.post(
        path,
        json={},
        headers={
            "Idempotency-Key": "reocr-key-1",
            "If-Artifact-Match": '"stale-revision"',
        },
    )
    not_transform = client.post(
        "/api/v1/items/book-1/raster-artifacts/image-1/reocr",
        json={},
        headers={
            "Idempotency-Key": "reocr-key-1",
            "If-Artifact-Match": '"artifact-image-1-r1"',
        },
    )
    unknown = client.post(
        "/api/v1/items/book-1/raster-artifacts/missing/reocr",
        json={},
        headers={
            "Idempotency-Key": "reocr-key-1",
            "If-Artifact-Match": '"whatever"',
        },
    )

    assert missing_key.status_code == 428
    assert missing_key.get_json()["code"] == "idempotency_key_required"
    assert missing_revision.status_code == 428
    assert missing_revision.get_json()["code"] == (
        "correction_target_revision_required"
    )
    assert extra_field.status_code == 400
    assert extra_field.get_json()["code"] == (
        "invalid_correction_mutation_envelope"
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "artifact_revision_conflict"
    assert not_transform.status_code == 400
    assert not_transform.get_json()["code"] == (
        "correction_reocr_source_not_transform_output"
    )
    assert unknown.status_code == 404
    assert unknown.get_json()["code"] == "raster_artifact_not_found"
    assert jobs.list() == []

    no_executor = _app(
        _Engine(projector, _SpatialProjector(()), reocr=reocr)
    ).test_client().post(
        path,
        json={},
        headers={"Idempotency-Key": "reocr-key-1", **revision},
    )
    assert no_executor.status_code == 503
    assert no_executor.get_json()["code"] == (
        "correction_reocr_executor_unavailable"
    )
    assert jobs.list() == []


def test_transform_queue_accepts_a_mask_polygon_and_rejects_unknown_fields():
    artifact = _raster("image-a")
    jobs = JobManager(checkpoint_interval=0)
    transforms = CorrectionTransformService(jobs)

    client = _app(
        _Engine(
            _RasterProjector((artifact,)),
            _SpatialProjector(()),
            transforms=transforms,
        ),
        transform_submitter=lambda service, command, queued: None,
    ).test_client()
    mask = [[0.1, 0.1], [0.9, 0.2], [0.5, 0.9]]
    command = _transform_command(artifact)
    path = "/api/v1/items/book-1/raster-artifacts/image-a/transforms"
    headers = {
        "Idempotency-Key": command.operation_id,
        "If-Artifact-Match": f'"{artifact.revision}"',
    }

    accepted = client.post(
        path,
        json=dict(command.as_dict(), mask_polygon=mask),
        headers=headers,
    )

    assert accepted.status_code == 202
    assert accepted.get_json()["job"]["input_revisions"]["transform"][
        "mask_polygon"
    ] == mask

    # The optional key widens the envelope by exactly the documented fields.
    unknown = client.post(
        path,
        json=dict(command.as_dict(), sharpen=True),
        headers={
            "Idempotency-Key": "transform-op-2",
            "If-Artifact-Match": f'"{artifact.revision}"',
        },
    )
    assert unknown.status_code == 400
    assert unknown.get_json()["code"] == "invalid_correction_mutation_envelope"

    degenerate = client.post(
        path,
        json=dict(
            command.as_dict(),
            operation_id="transform-op-3",
            mask_polygon=[[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        ),
        headers={
            "Idempotency-Key": "transform-op-3",
            "If-Artifact-Match": f'"{artifact.revision}"',
        },
    )
    assert degenerate.status_code == 400
    assert degenerate.get_json()["code"] == "invalid_correction_mask_polygon"
