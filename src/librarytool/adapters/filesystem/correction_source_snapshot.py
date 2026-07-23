"""Build immutable correction inputs from public raster projections.

The transform store deliberately accepts a narrow snapshot callback instead of
knowing how captures, OCR figures, or future raster providers are stored.  This
adapter joins the public raster/spatial projections with the trusted resource
resolver and copies the resolved stream into one checksum-verified immutable
snapshot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from ...engine.correction_transforms import CorrectionSourceSnapshot
from ...engine.errors import ConflictError, EngineError, RepositoryError
from ...engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    ResourceState,
)
from ...engine.spatial_annotations import (
    SpatialAnnotationProjectorPort,
    SpatialAnnotationView,
)


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


class FilesystemCorrectionSourceSnapshotReader:
    """Read one transform source through opaque, revision-pinned ports.

    Only full-canvas artifacts inherit canvas-space annotations.  Extracted
    crops deliberately do not: their source reference names the parent page,
    while their pixels use crop-local coordinates.
    """

    def __init__(
        self,
        raster_artifacts: RasterArtifactProjectorPort,
        spatial_annotations: SpatialAnnotationProjectorPort,
        resource_resolver: Any,
    ) -> None:
        for value, method, label in (
            (raster_artifacts, "get_raster_artifact", "raster_artifacts"),
            (
                spatial_annotations,
                "list_spatial_annotations",
                "spatial_annotations",
            ),
            (
                resource_resolver,
                "resolve_raster_resource",
                "resource_resolver",
            ),
        ):
            if not callable(getattr(value, method, None)):
                raise TypeError(f"{label} must expose {method}()")
        self._raster_artifacts = raster_artifacts
        self._spatial_annotations = spatial_annotations
        self._resource_resolver = resource_resolver

    def __call__(
        self,
        key: RasterArtifactKey,
    ) -> CorrectionSourceSnapshot | None:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be RasterArtifactKey")
        try:
            artifact = self._raster_artifacts.get_raster_artifact(key)
            if artifact is None:
                return None
            if (
                not isinstance(artifact, RasterArtifactView)
                or artifact.key != key
            ):
                raise RepositoryError(
                    "the correction raster projector returned an invalid source",
                    code="invalid_correction_transform_authority_snapshot",
                    details=key.as_dict(),
                )
            if (
                artifact.resource_state is not ResourceState.AVAILABLE
                or artifact.resource is None
            ):
                raise ConflictError(
                    "the correction source resource is unavailable",
                    code="correction_source_unavailable",
                    details={
                        **key.as_dict(),
                        "resource_state": artifact.resource_state.value,
                    },
                )

            resolved = self._resource_resolver.resolve_raster_resource(
                key.item_id,
                artifact.resource,
            )
            if resolved is None:
                raise ConflictError(
                    "the correction source resource changed",
                    code="correction_source_stale",
                    details={
                        **key.as_dict(),
                        "source_revision": artifact.resource.revision,
                    },
                )
            content = self._read_resolved(key, artifact, resolved)
            annotations = self._annotations_for(artifact)
            return CorrectionSourceSnapshot(
                artifact=artifact,
                source_revision=artifact.resource.revision,
                content=content,
                annotations=annotations,
            )
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the correction source authority is unavailable",
                code="correction_transform_authority_unavailable",
                details={
                    **key.as_dict(),
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc

    def _read_resolved(
        self,
        key: RasterArtifactKey,
        artifact: RasterArtifactView,
        resolved: Any,
    ) -> bytes:
        stream = getattr(resolved, "stream", None)
        media_type = getattr(resolved, "media_type", None)
        content_sha256 = getattr(resolved, "content_sha256", None)
        size = getattr(resolved, "size", None)
        revision = getattr(resolved, "revision", None)
        if (
            not callable(getattr(stream, "read", None))
            or not callable(getattr(stream, "seek", None))
            or not callable(getattr(stream, "close", None))
            or media_type != artifact.media_type
            or content_sha256 != artifact.content_sha256
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or revision != artifact.resource.revision
        ):
            _close_stream(stream)
            raise RepositoryError(
                "the correction resource resolver returned an invalid result",
                code="invalid_correction_source_resolution",
                details=key.as_dict(),
            )
        try:
            stream.seek(0)
            content = stream.read(size + 1)
        except Exception as exc:
            raise RepositoryError(
                "the correction source resource could not be read",
                code="correction_source_unavailable",
                details={
                    **key.as_dict(),
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc
        finally:
            _close_stream(stream)
        if (
            not isinstance(content, bytes)
            or len(content) != size
            or hashlib.sha256(content).hexdigest() != artifact.content_sha256
        ):
            raise ConflictError(
                "the correction source bytes changed",
                code="correction_source_stale",
                details={
                    **key.as_dict(),
                    "source_revision": artifact.resource.revision,
                },
            )
        return content

    def _annotations_for(
        self,
        artifact: RasterArtifactView,
    ) -> tuple[SpatialAnnotationView, ...]:
        corrections_ui = artifact.extensions.get("corrections_ui", {})
        annotation_frame = (
            corrections_ui.get("annotation_frame")
            if isinstance(corrections_ui, Mapping)
            else ""
        )
        if (
            annotation_frame != "canvas"
            or not artifact.source.canvas_id
        ):
            return ()
        values = self._spatial_annotations.list_spatial_annotations(
            artifact.key.item_id,
            representation_id=artifact.source.representation_id,
            canvas_id=artifact.source.canvas_id,
        )
        if isinstance(values, (str, bytes)) or not isinstance(
            values,
            Sequence,
        ):
            raise RepositoryError(
                "the correction spatial projector returned an invalid collection",
                code="invalid_correction_transform_authority_snapshot",
                details=artifact.key.as_dict(),
            )
        annotations = tuple(values)
        if any(
            not isinstance(value, SpatialAnnotationView)
            for value in annotations
        ):
            raise RepositoryError(
                "the correction spatial projector returned an invalid annotation",
                code="invalid_correction_transform_authority_snapshot",
                details=artifact.key.as_dict(),
            )
        return annotations


__all__ = ["FilesystemCorrectionSourceSnapshotReader"]
