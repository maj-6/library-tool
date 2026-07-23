from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, replace
from pathlib import Path

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
from librarytool.engine.corrections import CorrectionService
from librarytool.engine.correction_transforms import (
    CorrectionTransformCommand,
    CorrectionTransformService,
)
from librarytool.engine.jobs import JobManager
from librarytool.engine.raster_artifacts import (
    ArtifactFreshness,
    ArtifactProvenance,
    RasterArtifactKey,
    RasterLineageRef,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from librarytool.engine.runtime import (
    CORRECTION_SERVICE,
    CORRECTION_TRANSFORM_SERVICE,
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
from librarytool_http.corrections import create_corrections_blueprint


def _raster(
    artifact_id: str,
    *,
    content: bytes = b"png",
    canvas_id: str = "page-1",
    kind: str = "capture",
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


class _Engine:
    def __init__(self, raster, spatial, corrections=None, transforms=None):
        self.services = {
            RASTER_ARTIFACT_QUERY_SERVICE: raster,
            SPATIAL_ANNOTATION_QUERY_SERVICE: spatial,
        }
        if corrections is not None:
            self.services[CORRECTION_SERVICE] = corrections
        if transforms is not None:
            self.services[CORRECTION_TRANSFORM_SERVICE] = transforms

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


def _app(engine, resolver=None, transform_submitter=None):
    app = Flask(__name__)
    app.register_blueprint(
        create_corrections_blueprint(
            lambda: engine,
            raster_resource_resolver_for_request=(
                None if resolver is None else lambda: resolver
            ),
            correction_transform_submitter=transform_submitter,
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
    return _app(_Engine(projected, projected, corrections))


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
    assert annotation["effective_role"] == "figure"
    assert annotation["revision"] == receipt["targets"][0]["after_revision"]


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
