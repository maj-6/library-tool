from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import pytest

from librarytool.adapters.filesystem import (
    FilesystemCorrectionSourceSnapshotReader,
)
from librarytool.engine.errors import ConflictError
from librarytool.engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialSourceRef,
)


def _artifact(
    content: bytes,
    *,
    annotation_frame: str = "canvas",
    content_sha256: str = "",
) -> RasterArtifactView:
    return RasterArtifactView(
        key=RasterArtifactKey("book-1", "capture-1"),
        revision="artifact-r1",
        kind="captured-image",
        media_type="image/png",
        content_sha256=content_sha256 or hashlib.sha256(content).hexdigest(),
        dimensions=RasterDimensions(80, 120),
        source=RasterSourceRef(
            "capture",
            "capture-r1",
            "canvas-1",
            "canvas-r1",
        ),
        resource_state=ResourceState.AVAILABLE,
        resource=RasterResourceRef("capture-resource-1", "bytes-r1"),
        extensions={
            "corrections_ui": {"annotation_frame": annotation_frame},
        },
    )


def _annotation() -> SpatialAnnotationView:
    return SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", "region-1"),
        revision="region-r1",
        source=SpatialSourceRef(
            "capture",
            "capture-r1",
            "canvas-1",
            "canvas-r1",
        ),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            "canvas-r1",
            (
                NormalizedPoint(0.1, 0.1),
                NormalizedPoint(0.9, 0.1),
                NormalizedPoint(0.9, 0.9),
                NormalizedPoint(0.1, 0.9),
            ),
        ),
    )


class _Raster:
    def __init__(self, artifact):
        self.artifact = artifact

    def get_raster_artifact(self, key):
        return self.artifact if self.artifact.key == key else None


class _Spatial:
    def __init__(self, values):
        self.values = tuple(values)
        self.calls = []

    def list_spatial_annotations(
        self,
        item_id,
        *,
        representation_id="",
        canvas_id="",
    ):
        self.calls.append((item_id, representation_id, canvas_id))
        return self.values


@dataclass(frozen=True)
class _Resolved:
    stream: io.BytesIO
    media_type: str
    content_sha256: str
    size: int
    revision: str


class _Resolver:
    def __init__(self, artifact, content):
        self.artifact = artifact
        self.content = content
        self.stream = None

    def resolve_raster_resource(self, item_id, resource):
        assert item_id == self.artifact.key.item_id
        assert resource == self.artifact.resource
        self.stream = io.BytesIO(self.content)
        return _Resolved(
            self.stream,
            self.artifact.media_type,
            self.artifact.content_sha256,
            len(self.content),
            self.artifact.resource.revision,
        )


def test_reader_copies_verified_bytes_and_canvas_annotations() -> None:
    content = b"immutable-raster-source"
    artifact = _artifact(content)
    spatial = _Spatial((_annotation(),))
    resolver = _Resolver(artifact, content)
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        spatial,
        resolver,
    )

    snapshot = reader(artifact.key)

    assert snapshot.artifact is artifact
    assert snapshot.source_revision == "bytes-r1"
    assert snapshot.content == content
    assert snapshot.annotations == (_annotation(),)
    assert spatial.calls == [("book-1", "capture", "canvas-1")]
    assert resolver.stream.closed is True


def test_reader_does_not_apply_page_space_annotations_to_crop_bytes() -> None:
    content = b"extracted-crop"
    artifact = _artifact(content, annotation_frame="crop")
    spatial = _Spatial((_annotation(),))
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        spatial,
        _Resolver(artifact, content),
    )

    snapshot = reader(artifact.key)

    assert snapshot.annotations == ()
    assert spatial.calls == []


def test_reader_rejects_changed_bytes_and_closes_the_snapshot_stream() -> None:
    declared = hashlib.sha256(b"declared").hexdigest()
    artifact = _artifact(b"declared", content_sha256=declared)
    resolver = _Resolver(artifact, b"changed!")
    reader = FilesystemCorrectionSourceSnapshotReader(
        _Raster(artifact),
        _Spatial(()),
        resolver,
    )

    with pytest.raises(ConflictError) as raised:
        reader(artifact.key)

    assert raised.value.code == "correction_source_stale"
    assert resolver.stream.closed is True
