from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from flask import Flask

from librarytool.engine.raster_artifacts import (
    ArtifactFreshness,
    ArtifactProvenance,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from librarytool.engine.runtime import (
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
    def __init__(self, raster, spatial):
        self.services = {
            RASTER_ARTIFACT_QUERY_SERVICE: raster,
            SPATIAL_ANNOTATION_QUERY_SERVICE: spatial,
        }

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


def _app(engine, resolver=None):
    app = Flask(__name__)
    app.register_blueprint(
        create_corrections_blueprint(
            lambda: engine,
            raster_resource_resolver_for_request=(
                None if resolver is None else lambda: resolver
            ),
        )
    )
    return app


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
