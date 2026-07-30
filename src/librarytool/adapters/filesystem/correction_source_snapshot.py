"""Build immutable correction inputs from public raster projections.

The transform store deliberately accepts a narrow snapshot callback instead of
knowing how captures, OCR figures, or future raster providers are stored.  This
adapter joins the public raster/spatial projections with the trusted resource
resolver and copies the resolved stream into one checksum-verified immutable
snapshot.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ...engine.correction_transforms import (
    CorrectionSourceSnapshot,
    HumanTextAssertion,
    HumanTextOrigin,
)
from ...engine.errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
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
from ...engine.text_layer_aggregate import (
    TextLayerDocumentView,
    TextLayerSummaryView,
    TextLayerUnitSnapshot,
)


HumanTextAssertionLookup = Callable[
    [RasterArtifactView], Sequence[HumanTextAssertion]
]
TextLayerItemIdentityLookup = Callable[[str], str | None]


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        pass


def _human_text_origin(
    unit: TextLayerUnitSnapshot,
) -> HumanTextOrigin | None:
    """Map canonical provenance to the correction transform's protection set."""

    provenance = unit.provenance
    if provenance.review_state == "rejected":
        return None
    if provenance.review_state in {"reviewed", "approved"}:
        return HumanTextOrigin.VERIFIED
    if provenance.origin == "human":
        return HumanTextOrigin.MANUAL
    if provenance.origin == "import":
        return HumanTextOrigin.IMPORTED
    return None


def _human_text_assertion_id(
    item_id: str,
    layer_id: str,
    selector: str,
) -> str:
    # Keep identity stable while text/provenance revisions change. Hashing also
    # keeps the composed identity inside the correction contract's 128-byte
    # portable identifier bound.
    payload = f"{item_id}\0{layer_id}\0{selector}".encode("utf-8")
    return "txt-" + hashlib.sha256(payload).hexdigest()


class CanonicalTextLayerHumanAssertionReader:
    """Project protected text from the canonical text-layer query service.

    Text-layer selectors are deliberately opaque, so protection is scoped to
    the artifact's exact representation revision rather than guessing that a
    selector names its canvas. A list/get convergence check makes a changing
    layer fail closed; the correction store's later source reload then uses
    each unit revision as an ordinary dependent CAS pin.

    Corrections identities may remain stable while a capture is promoted to a
    build whose native text layers use a different item identity. The optional
    lookup maps the stable Corrections identity to that active native identity.
    Returning ``None`` deliberately projects no human layers, as for a
    capture-only entry that has no native text-layer store yet.
    """

    def __init__(
        self,
        text_layers: Any,
        *,
        text_layer_item_id_for: TextLayerItemIdentityLookup | None = None,
    ) -> None:
        for method in ("list", "get"):
            if not callable(getattr(text_layers, method, None)):
                raise TypeError(f"text_layers must expose {method}()")
        if (
            text_layer_item_id_for is not None
            and not callable(text_layer_item_id_for)
        ):
            raise TypeError("text_layer_item_id_for must be callable or None")
        self._text_layers = text_layers
        self._text_layer_item_id_for = text_layer_item_id_for

    def __call__(
        self,
        artifact: RasterArtifactView,
    ) -> tuple[HumanTextAssertion, ...]:
        if not isinstance(artifact, RasterArtifactView):
            raise TypeError("artifact must be RasterArtifactView")
        item_id = artifact.key.item_id
        text_layer_item_id = (
            item_id
            if self._text_layer_item_id_for is None
            else self._text_layer_item_id_for(item_id)
        )
        if text_layer_item_id is None:
            return ()
        representation_id = artifact.source.representation_id
        representation_revision = artifact.source.representation_revision
        summaries = self._text_layers.list(text_layer_item_id)
        if isinstance(summaries, (str, bytes)) or not isinstance(
            summaries,
            Sequence,
        ):
            raise RepositoryError(
                "the text-layer query returned an invalid collection",
                code="invalid_correction_human_text_snapshot",
                details=artifact.key.as_dict(),
            )
        values = tuple(summaries)
        if any(not isinstance(value, TextLayerSummaryView) for value in values):
            raise RepositoryError(
                "the text-layer query returned an invalid summary",
                code="invalid_correction_human_text_snapshot",
                details=artifact.key.as_dict(),
            )
        layer_ids = tuple(value.layer_id for value in values)
        if (
            any(value.item_id != text_layer_item_id for value in values)
            or len(layer_ids) != len(set(layer_ids))
        ):
            raise RepositoryError(
                "the text-layer query returned an incoherent collection",
                code="invalid_correction_human_text_snapshot",
                details=artifact.key.as_dict(),
            )

        assertions: list[HumanTextAssertion] = []
        for summary in values:
            if (
                summary.source.representation_id != representation_id
                or summary.source.pinned_revision != representation_revision
            ):
                continue
            try:
                view = self._text_layers.get(
                    text_layer_item_id,
                    summary.layer_id,
                )
            except NotFoundError as exc:
                raise ConflictError(
                    "a protected text layer changed while it was read",
                    code="correction_human_text_snapshot_changed",
                    details={
                        **artifact.key.as_dict(),
                        "layer_id": summary.layer_id,
                    },
                ) from exc
            if not isinstance(view, TextLayerDocumentView):
                raise RepositoryError(
                    "the text-layer query returned an invalid document",
                    code="invalid_correction_human_text_snapshot",
                    details={
                        **artifact.key.as_dict(),
                        "layer_id": summary.layer_id,
                    },
                )
            if view.summary() != summary:
                raise ConflictError(
                    "a protected text layer changed while it was read",
                    code="correction_human_text_snapshot_changed",
                    details={
                        **artifact.key.as_dict(),
                        "layer_id": summary.layer_id,
                    },
                )
            for unit in view.document.units:
                origin = _human_text_origin(unit)
                if origin is None:
                    continue
                try:
                    assertions.append(
                        HumanTextAssertion(
                            assertion_id=_human_text_assertion_id(
                                item_id,
                                summary.layer_id,
                                unit.selector,
                            ),
                            revision=unit.unit_revision,
                            text=unit.text,
                            origin=origin,
                            language=view.document.language,
                        )
                    )
                except ValidationError as exc:
                    # Canonical units permit empty and much larger text than
                    # the transform assertion envelope. Never silently drop a
                    # human-owned value merely because the schemas differ.
                    raise RepositoryError(
                        "a protected text unit cannot fit the correction snapshot",
                        code="incompatible_correction_human_text_assertion",
                        details={
                            **artifact.key.as_dict(),
                            "layer_id": summary.layer_id,
                            "selector": unit.selector,
                            "cause_code": exc.code,
                        },
                    ) from exc

        assertions.sort(key=lambda value: value.assertion_id)
        assertion_ids = tuple(value.assertion_id for value in assertions)
        if len(assertion_ids) != len(set(assertion_ids)):
            raise RepositoryError(
                "protected text identities collided",
                code="invalid_correction_human_text_snapshot",
                details=artifact.key.as_dict(),
            )
        return tuple(assertions)


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
        *,
        human_text_assertions_for: HumanTextAssertionLookup | None = None,
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
        if (
            human_text_assertions_for is not None
            and not callable(human_text_assertions_for)
        ):
            raise TypeError(
                "human_text_assertions_for must be callable or None"
            )
        self._raster_artifacts = raster_artifacts
        self._spatial_annotations = spatial_annotations
        self._resource_resolver = resource_resolver
        self._human_text_assertions_for = human_text_assertions_for

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
            human_text_assertions = self._human_text_assertions(artifact)
            return CorrectionSourceSnapshot(
                artifact=artifact,
                source_revision=artifact.resource.revision,
                content=content,
                annotations=annotations,
                human_text_assertions=human_text_assertions,
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
        return tuple(
            value
            for value in annotations
            if (
                value.source.representation_revision
                == artifact.source.representation_revision
                and value.source.canvas_revision
                == artifact.source.canvas_revision
                and value.selector.coordinate_space_revision
                == artifact.source.canvas_revision
            )
        )


    def _human_text_assertions(
        self,
        artifact: RasterArtifactView,
    ) -> tuple[HumanTextAssertion, ...]:
        lookup = self._human_text_assertions_for
        if lookup is None:
            return ()
        values = lookup(artifact)
        if isinstance(values, (str, bytes)) or not isinstance(
            values,
            Sequence,
        ):
            raise RepositoryError(
                "the correction human-text lookup returned an invalid collection",
                code="invalid_correction_human_text_snapshot",
                details=artifact.key.as_dict(),
            )
        assertions = tuple(values)
        if any(
            not isinstance(value, HumanTextAssertion)
            for value in assertions
        ):
            raise RepositoryError(
                "the correction human-text lookup returned an invalid assertion",
                code="invalid_correction_human_text_snapshot",
                details=artifact.key.as_dict(),
            )
        return assertions


__all__ = [
    "CanonicalTextLayerHumanAssertionReader",
    "FilesystemCorrectionSourceSnapshotReader",
]
