"""Recoverable publication adapter for immutable correction transforms.

The transform worker builds a complete, immutable commit draft in memory.  This
adapter owns the last concurrency boundary: it reloads the source under the
workspace and catalogue locks, compares every command and assertion revision
pin, and publishes four new object files, their publication envelope, and the
item-scoped immutable pointer and idempotency receipt in one
:class:`RecoverableWriteSet` transaction.

Public identifiers never become path components.  Private storage uses
SHA-256-addressed filenames below ``.engine`` and a receipt is staged last so a
durable replay can only become visible with the complete publication. Public
queries inspect only the requested item's pointer directory; legacy version-1
publications remain replayable but are intentionally not projected because
they do not pin a representation/canvas source scope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, Protocol, TypeAlias, runtime_checkable

from ...engine.correction_transforms import (
    CORRECTION_OUTPUT_KINDS,
    CommittedCorrectionOutput,
    CorrectionHumanAssertions,
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    CorrectionTransformCommitDraft,
    CorrectionTransformCommitResult,
)
from ...engine.errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    RepositoryError,
)
from ...engine.raster_artifacts import (
    ArtifactFreshness,
    ArtifactProvenance,
    CaptionAssertion,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterLineageRef,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from ...engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
)
from .corrections_artifact_repository import (
    ResolvedRasterResource,
    _AuthorityDirectorySnapshot,
    _AuthoritySnapshot,
    _finish_verified_regular,
    _open_verified_regular,
    _stable_stat_identity,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    WriteSetError,
    _is_redirecting_path,
)


SourceSnapshotLookup: TypeAlias = Callable[
    [RasterArtifactKey], CorrectionSourceSnapshot | None
]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]


@runtime_checkable
class CorrectionTransformOutputResolverPort(Protocol):
    """Resolve one exact committed output without exposing private paths."""

    def resolve_committed_output(
        self,
        item_id: str,
        operation_id: str,
        output: CommittedCorrectionOutput,
    ) -> ResolvedRasterResource | None: ...

CORRECTION_TRANSFORM_PUBLICATION_SCHEMA = "librarytool.correction-transform-publication"
CORRECTION_TRANSFORM_PUBLICATION_VERSION = 2
CORRECTION_TRANSFORM_RECEIPT_SCHEMA = "librarytool.correction-transform-receipt"
CORRECTION_TRANSFORM_RECEIPT_VERSION = 1
CORRECTION_TRANSFORM_ITEM_POINTER_SCHEMA = (
    "librarytool.correction-transform-item-pointer"
)
CORRECTION_TRANSFORM_ITEM_POINTER_VERSION = 1
_ITEM_INDEX_MARKER_SCHEMA = "librarytool.correction-transform-item-index"
_ITEM_INDEX_MARKER_VERSION = 1

_OBJECT_ROOT = PurePosixPath(".engine/correction-transforms/objects")
_PUBLICATION_ROOT = PurePosixPath(".engine/correction-transforms/publications")
_ITEM_POINTER_ROOT = PurePosixPath(".engine/correction-transforms/by-item")
_ITEM_INDEX_MARKER = PurePosixPath(
    ".engine/correction-transforms/item-index-v1.json"
)
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/correction-transforms")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_ITEM_POINTER_BYTES = 64 * 1024
_MAX_PUBLICATION_BYTES = 128 * 1024 * 1024
_MAX_OUTPUT_BYTES = 1024 * 1024 * 1024
_MAX_ITEM_TRANSFORMS = 10_000
_MAX_LEGACY_MIGRATION_DOCUMENTS = 100_000
_TRANSFORM_DOCUMENT_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_PROJECTED_RASTER_OUTPUTS = {
    "corrected-display": ("corrected-image", "Corrected display", "display"),
    "ocr-ready": ("processed-source", "OCR-ready image", "ocr"),
    "thumbnail": ("processed-image", "Correction thumbnail", "thumbnail"),
}
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "operation_id",
        "command_sha256",
        "publication_sha256",
        "result",
    }
)
_ITEM_POINTER_FIELDS = frozenset(
    {
        "schema",
        "version",
        "item_id",
        "operation_id",
        "command_sha256",
        "publication_sha256",
    }
)
_ITEM_INDEX_MARKER_FIELDS = frozenset({"schema", "version"})
_RESULT_FIELDS = frozenset({"operation_id", "outputs"})
_COMMITTED_OUTPUT_FIELDS = frozenset(
    {"kind", "artifact_id", "artifact_revision", "content_sha256"}
)
_PUBLICATION_FIELDS = frozenset(
    {
        "schema",
        "version",
        "operation_id",
        "command_sha256",
        "command",
        "source",
        "result",
        "outputs",
        "mapped_annotations",
        "dropped_annotation_ids",
        "human_assertions",
        "human_assertion_policy",
    }
)
_PUBLICATION_SOURCE_V1_FIELDS = frozenset(
    {
        "item_id",
        "artifact_id",
        "artifact_revision",
        "source_revision",
        "source_sha256",
        "dependent_revision_pins",
    }
)
_PUBLICATION_SOURCE_FIELDS = frozenset(
    {
        "item_id",
        "artifact_id",
        "artifact_revision",
        "source_revision",
        "source_sha256",
        "dependent_revision_pins",
        "raster_source",
    }
)
_RASTER_SOURCE_FIELDS = frozenset(
    {"representation_id", "representation_revision"}
)
_RASTER_CANVAS_SOURCE_FIELDS = frozenset(
    {
        "representation_id",
        "representation_revision",
        "canvas_id",
        "canvas_revision",
    }
)
_PUBLICATION_OUTPUT_FIELDS = frozenset(
    {
        "kind",
        "media_type",
        "content_sha256",
        "bytes",
        "dimensions",
        "provenance",
        "artifact_id",
        "artifact_revision",
        "storage",
    }
)
_HUMAN_ASSERTION_FIELDS = frozenset(
    {"artifact_categories", "artifact_captions", "spatial", "text"}
)
_DIMENSION_FIELDS = frozenset({"width", "height", "orientation"})
_PROVENANCE_FIELDS = frozenset(
    {
        "origin",
        "provider_id",
        "model",
        "recipe_revision",
        "operation_id",
        "generated_at",
        "extensions",
    }
)
_MAPPED_ANNOTATION_FIELDS = frozenset(
    {
        "annotation_id",
        "source_revision",
        "coordinate_space",
        "points",
        "order",
        "label",
        "role_assignments",
        "caption_assertions",
        "linked_artifact_ids",
    }
)
_POINT_FIELDS = frozenset({"x", "y"})
_ROLE_FIELDS = frozenset(
    {
        "role",
        "origin",
        "revision",
        "confidence",
        "provenance",
        "extensions",
    }
)
_CAPTION_FIELDS = frozenset(
    {
        "text",
        "origin",
        "revision",
        "language",
        "source_annotation_id",
        "confidence",
        "provenance",
        "extensions",
    }
)
_PRESERVED_SPATIAL_FIELDS = frozenset(
    {
        "annotation_id",
        "annotation_revision",
        "roles",
        "captions",
    }
)


@dataclass(frozen=True, slots=True)
class _TransformResourceCandidate:
    path: Path
    media_type: str
    content_sha256: str
    size: int
    revision: str
    artifact: str


@dataclass(frozen=True, slots=True)
class _TransformProjection:
    raster_artifacts: tuple[RasterArtifactView, ...]
    spatial_annotations: tuple[SpatialAnnotationView, ...]
    resources: Mapping[
        tuple[str, str, str],
        _TransformResourceCandidate,
    ]


@dataclass(frozen=True, slots=True)
class _ItemPublicationPointer:
    item_id: str
    operation_id: str
    command_sha256: str
    publication_sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedTransformPublication:
    command: CorrectionTransformCommand
    result: CorrectionTransformCommitResult
    document: Mapping[str, Any]
    publication_sha256: str


def _repository_error(
    message: str,
    *,
    code: str,
    artifact: str = "",
    cause: Exception | None = None,
    retryable: bool = False,
) -> RepositoryError:
    details: dict[str, Any] = {}
    if artifact:
        details["artifact"] = artifact
    if cause is not None:
        details["cause_type"] = type(cause).__name__
    return RepositoryError(
        message,
        code=code,
        details=details,
        retryable=retryable,
    )


def _canonical_json(value: Any, *, artifact: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _repository_error(
            "a correction transform document cannot be serialized",
            code="invalid_correction_transform_document",
            artifact=artifact,
            cause=exc,
        ) from exc


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _strict_object(
    value: Any,
    *,
    fields: frozenset[str],
    artifact: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        raise ValueError(f"{artifact} must contain its exact schema fields")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _source_pins(source: CorrectionSourceSnapshot) -> dict[str, str]:
    return {
        "item_id": source.artifact.key.item_id,
        "artifact_id": source.artifact.key.artifact_id,
        "artifact_revision": source.artifact.revision,
        "source_revision": source.source_revision,
        "source_sha256": source.source_sha256,
    }


def _command_pins(draft: CorrectionTransformCommitDraft) -> dict[str, str]:
    command = draft.command
    return {
        "item_id": command.item_id,
        "artifact_id": command.artifact_id,
        "artifact_revision": command.artifact_revision,
        "source_revision": command.source_revision,
        "source_sha256": command.source_sha256,
    }


def _output_identity(
    command_sha256: str,
    kind: str,
    reserved: set[str],
) -> str:
    for nonce in range(32):
        payload = f"{command_sha256}\0{kind}\0{nonce}\0artifact".encode("ascii")
        candidate = "ctr-" + hashlib.sha256(payload).hexdigest()[:40]
        if candidate.casefold() not in reserved:
            reserved.add(candidate.casefold())
            return candidate
    raise _repository_error(
        "a distinct correction output identity could not be allocated",
        code="correction_output_identity_exhausted",
    )


def _output_revision(command_sha256: str, kind: str, content_sha256: str) -> str:
    payload = f"{command_sha256}\0{kind}\0{content_sha256}\0revision".encode(
        "ascii"
    )
    return "ctr:" + hashlib.sha256(payload).hexdigest()


def _commit_result_for(
    draft: CorrectionTransformCommitDraft,
) -> CorrectionTransformCommitResult:
    reserved = {draft.command.artifact_id.casefold()}
    outputs: list[CommittedCorrectionOutput] = []
    for kind in CORRECTION_OUTPUT_KINDS:
        output = draft.output(kind)
        artifact_id = _output_identity(
            draft.command.fingerprint,
            kind,
            reserved,
        )
        outputs.append(
            CommittedCorrectionOutput(
                kind=kind,
                artifact_id=artifact_id,
                artifact_revision=_output_revision(
                    draft.command.fingerprint,
                    kind,
                    output.content_sha256,
                ),
                content_sha256=output.content_sha256,
            )
        )
    return CorrectionTransformCommitResult(
        draft.command.operation_id,
        tuple(outputs),
    )


class FilesystemCorrectionTransformStore:
    """Load correction sources and atomically publish transform outputs."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        source_snapshot_for: SourceSnapshotLookup,
        lock_context_for: LockContextFactory,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not callable(source_snapshot_for):
            raise TypeError("source_snapshot_for must be callable")
        if not callable(lock_context_for):
            raise TypeError("lock_context_for must be callable")
        self._write_set = write_set
        self._source_snapshot_for = source_snapshot_for
        self._lock_context_for = lock_context_for
        self._unindexed_v2_cache: dict[
            str,
            tuple[_ItemPublicationPointer, ...],
        ] | None = None
        self._unindexed_v2_overflow: set[str] = set()
        self._unindexed_v2_errors: dict[str, RepositoryError] = {}
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except WriteSetError as exc:
                raise _repository_error(
                    "the correction transform store could not recover",
                    code="correction_transform_recovery_failed",
                    cause=exc,
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise _repository_error(
                    "the correction transform authority lock is unavailable",
                    code="correction_transform_authority_unavailable",
                    cause=exc,
                    retryable=True,
                ) from exc

    def load_source(self, key: RasterArtifactKey) -> CorrectionSourceSnapshot:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be a RasterArtifactKey")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    return self._load_source_locked(key)
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction source authority is unavailable",
                code="correction_transform_authority_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def commit_transform(
        self,
        draft: CorrectionTransformCommitDraft,
    ) -> CorrectionTransformCommitResult:
        if not isinstance(draft, CorrectionTransformCommitDraft):
            raise TypeError("draft must be a CorrectionTransformCommitDraft")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    replay = self._read_receipt(draft.command.operation_id)
                    if replay is not None:
                        command_sha256, publication_sha256, result = replay
                        if command_sha256 != draft.command.fingerprint:
                            raise ConflictError(
                                "correction operation was reused for another command",
                                code="correction_operation_conflict",
                                details={"operation_id": draft.command.operation_id},
                            )
                        publication = self._validate_replay_publication(
                            draft.command,
                            result,
                            publication_sha256=publication_sha256,
                        )
                        if (
                            publication["version"]
                            == CORRECTION_TRANSFORM_PUBLICATION_VERSION
                        ):
                            self._ensure_item_pointer(
                                draft.command,
                                publication_sha256=publication_sha256,
                            )
                        return result

                    self._validate_draft(draft)
                    live = self._load_source_locked(draft.command.key)
                    self._compare_source(draft, live)
                    result = _commit_result_for(draft)
                    self._publish(draft, result)
                    return result
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform transaction failed",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except (ConflictError, NotFoundError, RepositoryError):
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform transaction failed",
                code="correction_transform_transaction_failed",
                cause=exc,
                retryable=True,
            ) from exc

    def list_raster_artifacts(
        self,
        item_id: str,
    ) -> tuple[RasterArtifactView, ...]:
        """Project durable image outputs without exposing private storage."""

        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    return self._project_locked(item_id).raster_artifacts
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except (NotFoundError, RepositoryError):
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform projection is unavailable",
                code="correction_transform_projection_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def get_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be a RasterArtifactKey")
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
        """Project immutable mapped geometry for corrected display outputs."""

        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    values = self._project_locked(item_id).spatial_annotations
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except (NotFoundError, RepositoryError):
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform projection is unavailable",
                code="correction_transform_projection_unavailable",
                cause=exc,
                retryable=True,
            ) from exc
        return tuple(
            value
            for value in values
            if (
                not representation_id
                or value.source.representation_id == representation_id
            )
            and (not canvas_id or value.source.canvas_id == canvas_id)
        )

    def get_spatial_annotation(
        self,
        key: SpatialAnnotationKey,
    ) -> SpatialAnnotationView | None:
        if not isinstance(key, SpatialAnnotationKey):
            raise TypeError("key must be a SpatialAnnotationKey")
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
    ) -> ResolvedRasterResource | None:
        if not isinstance(resource, RasterResourceRef):
            raise TypeError("resource must be a RasterResourceRef")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    candidate = self._project_locked(item_id).resources.get(
                        (
                            resource.resource_id,
                            resource.revision,
                            resource.variant,
                        )
                    )
                    if candidate is None:
                        return None
                    return self._snapshot_resource(candidate)
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except RepositoryError:
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform resource is unavailable",
                code="correction_transform_resource_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def resolve_committed_output(
        self,
        item_id: str,
        operation_id: str,
        output: CommittedCorrectionOutput,
    ) -> ResolvedRasterResource | None:
        """Return a verified immutable snapshot for one committed descriptor.

        The descriptor must match its item-scoped publication by kind,
        artifact identity, revision, and checksum. The returned temporary
        stream contains checksum-verified bytes and never exposes the private
        object path.
        """

        if not isinstance(output, CommittedCorrectionOutput):
            raise TypeError("output must be a CommittedCorrectionOutput")
        if not isinstance(operation_id, str) or not operation_id:
            raise TypeError("operation_id must be a non-empty string")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    candidate = self._committed_output_candidate_locked(
                        item_id,
                        operation_id,
                        output,
                    )
                    if candidate is None:
                        return None
                    return self._snapshot_resource(candidate)
        except WriteSetError as exc:
            raise _repository_error(
                "the correction transform workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except RepositoryError:
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the committed correction output is unavailable",
                code="correction_transform_resource_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def _committed_output_candidate_locked(
        self,
        item_id: str,
        operation_id: str,
        output: CommittedCorrectionOutput,
    ) -> _TransformResourceCandidate | None:
        RasterArtifactKey(item_id, "correction-transform-output")
        validated = self._validated_operation_publication(
            item_id,
            operation_id,
            verify_objects=False,
        )
        if validated is None:
            return None
        for committed, descriptor in zip(
            validated.result.outputs,
            validated.document["outputs"],
            strict=True,
        ):
            if committed.artifact_id.casefold() != output.artifact_id.casefold():
                continue
            if committed != output:
                return None
            return _TransformResourceCandidate(
                self._object_path(committed.artifact_id),
                descriptor["media_type"],
                committed.content_sha256,
                descriptor["bytes"],
                committed.artifact_revision,
                committed.kind,
            )
        return None

    def _project_locked(self, item_id: str) -> _TransformProjection:
        # Validate the public scope even when no publication exists.
        RasterArtifactKey(item_id, "correction-transform-projection")
        values: list[RasterArtifactView] = []
        spatial: list[SpatialAnnotationView] = []
        resources: dict[
            tuple[str, str, str],
            _TransformResourceCandidate,
        ] = {}
        identities: set[str] = set()
        spatial_identities: set[str] = set()
        for pointer in self._projection_item_pointers_locked(item_id):
            command, result, publication = self._validated_publication(pointer)
            source_scope = self._raster_source_from_publication(publication)
            for committed, raw_output in zip(
                result.outputs,
                publication["outputs"],
                strict=True,
            ):
                public = _PROJECTED_RASTER_OUTPUTS.get(committed.kind)
                if public is None:
                    continue
                kind, label, variant = public
                artifact_identity = committed.artifact_id.casefold()
                if artifact_identity in identities:
                    raise _repository_error(
                        "a correction transform output identity is duplicated",
                        code="invalid_correction_transform_storage",
                        artifact="correction_transform_publication",
                    )
                identities.add(artifact_identity)
                dimensions = RasterDimensions(
                    **_strict_object(
                        raw_output["dimensions"],
                        fields=_DIMENSION_FIELDS,
                        artifact="correction_transform_output_dimensions",
                    )
                )
                provenance = ArtifactProvenance(
                    **_strict_object(
                        raw_output["provenance"],
                        fields=_PROVENANCE_FIELDS,
                        artifact="correction_transform_output_provenance",
                    )
                )
                resource_id = self._resource_id(
                    item_id,
                    committed.artifact_id,
                )
                resource = RasterResourceRef(
                    resource_id,
                    committed.artifact_revision,
                    variant,
                )
                source = self._projected_raster_source(
                    source_scope,
                    committed.artifact_revision,
                )
                extensions: dict[str, Any] = {
                    "correction_transform": {
                        "operation_id": command.operation_id,
                        "output_kind": committed.kind,
                        "source_revision": command.source_revision,
                        "source_scope": source_scope.as_dict(),
                    }
                }
                if committed.kind == "corrected-display" and source.canvas_id:
                    extensions["corrections_ui"] = {
                        "annotation_frame": "canvas",
                    }
                view = RasterArtifactView(
                    key=RasterArtifactKey(item_id, committed.artifact_id),
                    revision=committed.artifact_revision,
                    kind=kind,
                    media_type=raw_output["media_type"],
                    content_sha256=committed.content_sha256,
                    dimensions=dimensions,
                    source=source,
                    resource_state=ResourceState.AVAILABLE,
                    resource=resource,
                    label=label,
                    # The private publication proves immutable bytes, not that
                    # its lineage is still the live authority.  The public
                    # correction projection may later refine this state.
                    freshness=ArtifactFreshness.UNTRACKED,
                    lineage=(
                        RasterLineageRef(
                            command.artifact_id,
                            command.artifact_revision,
                            "processed_from",
                        ),
                    ),
                    provenance=provenance,
                    extensions=extensions,
                )
                key = (
                    resource.resource_id,
                    resource.revision,
                    resource.variant,
                )
                if key in resources:
                    raise _repository_error(
                        "a correction transform resource identity is duplicated",
                        code="invalid_correction_transform_storage",
                        artifact="correction_transform_publication",
                    )
                resources[key] = _TransformResourceCandidate(
                    self._object_path(committed.artifact_id),
                    raw_output["media_type"],
                    committed.content_sha256,
                    raw_output["bytes"],
                    resource.revision,
                    committed.kind,
                )
                values.append(view)
            for annotation in self._mapped_annotation_views(
                command,
                result,
                publication,
                source_scope,
            ):
                identity = annotation.key.annotation_id.casefold()
                if identity in spatial_identities:
                    raise _repository_error(
                        "a mapped correction annotation identity is duplicated",
                        code="invalid_correction_transform_storage",
                        artifact="correction_transform_publication",
                    )
                spatial_identities.add(identity)
                spatial.append(annotation)
        return _TransformProjection(
            tuple(sorted(values, key=lambda value: value.key.artifact_id)),
            tuple(
                sorted(
                    spatial,
                    key=lambda value: value.key.annotation_id,
                )
            ),
            resources,
        )

    def _item_publication_pointers(
        self,
        item_id: str,
    ) -> tuple[_ItemPublicationPointer, ...]:
        relative = _ITEM_POINTER_ROOT / _digest_text(item_id)
        paths = self._authority_document_paths(
            relative,
            artifact="correction_transform_item_index",
            maximum_entries=_MAX_ITEM_TRANSFORMS,
        )
        return tuple(
            self._validated_item_pointer(path, item_id=item_id)
            for _name, path in sorted(paths.items())
        )

    def _projection_item_pointers_locked(
        self,
        item_id: str,
    ) -> tuple[_ItemPublicationPointer, ...]:
        indexed = {
            value.operation_id: value
            for value in self._item_publication_pointers(item_id)
        }
        marker_path = self._target(_ITEM_INDEX_MARKER)
        if self._path_exists(
            marker_path,
            artifact="correction_transform_item_index_marker",
        ):
            self._validate_item_index_marker(marker_path)
            return tuple(
                indexed[key]
                for key in sorted(indexed)
            )

        if self._unindexed_v2_cache is None:
            self._unindexed_v2_cache = (
                self._scan_unindexed_v2_publications_locked()
            )
        if item_id in self._unindexed_v2_overflow:
            raise RepositoryError(
                "the unindexed correction transform history for this item "
                "exceeds the supported projection limit",
                code="correction_transform_item_limit",
                details={
                    "item_id": item_id,
                    "limit": _MAX_ITEM_TRANSFORMS,
                },
            )
        discovery_error = self._unindexed_v2_errors.get(item_id)
        if discovery_error is not None:
            raise discovery_error
        for pointer in self._unindexed_v2_cache.get(item_id, ()):
            prior = indexed.get(pointer.operation_id)
            if prior is not None and prior != pointer:
                raise _repository_error(
                    "the correction transform item pointer conflicts with "
                    "its unindexed publication",
                    code="invalid_correction_transform_storage",
                    artifact="correction_transform_item_pointer",
                )
            indexed[pointer.operation_id] = pointer
        return tuple(indexed[key] for key in sorted(indexed))

    def _scan_unindexed_v2_publications_locked(
        self,
    ) -> dict[str, tuple[_ItemPublicationPointer, ...]]:
        self._unindexed_v2_errors.clear()
        publications = self._authority_document_paths(
            _PUBLICATION_ROOT,
            artifact="correction_transform_publications",
            maximum_entries=_MAX_LEGACY_MIGRATION_DOCUMENTS,
        )
        values: dict[str, list[_ItemPublicationPointer]] = {}
        for _name, path in sorted(publications.items()):
            try:
                raw = self._read_json(
                    path,
                    maximum=_MAX_PUBLICATION_BYTES,
                    artifact="correction_transform_publication",
                )
                publication = _strict_object(
                    raw,
                    fields=_PUBLICATION_FIELDS,
                    artifact="correction_transform_publication",
                )
                if (
                    publication["version"]
                    != CORRECTION_TRANSFORM_PUBLICATION_VERSION
                ):
                    continue
                command = CorrectionTransformCommand.from_dict(
                    publication["command"]
                )
            except (
                EngineError,
                KeyError,
                RepositoryError,
                TypeError,
                ValueError,
            ):
                # Compatibility discovery must not let one corrupt, orphaned
                # unindexed document break unrelated item reads. Indexed
                # documents remain strict and fail closed.
                continue
            if path != self._publication_path(command.operation_id):
                self._unindexed_v2_errors.setdefault(
                    command.item_id,
                    _repository_error(
                        "an unindexed correction transform publication is "
                        "not bound to its operation",
                        code="invalid_correction_transform_storage",
                        artifact="correction_transform_publication",
                    ),
                )
                continue
            try:
                validated = self._validated_operation_publication(
                    command.item_id,
                    command.operation_id,
                    verify_objects=False,
                )
            except RepositoryError as exc:
                self._unindexed_v2_errors.setdefault(
                    command.item_id,
                    exc,
                )
                continue
            if validated is None:
                self._unindexed_v2_errors.setdefault(
                    command.item_id,
                    _repository_error(
                        "an unindexed correction transform publication has "
                        "no valid receipt",
                        code="invalid_correction_transform_storage",
                        artifact="correction_transform_publication",
                    ),
                )
                continue
            item_values = values.setdefault(command.item_id, [])
            if len(item_values) >= _MAX_ITEM_TRANSFORMS:
                self._unindexed_v2_overflow.add(command.item_id)
                continue
            item_values.append(
                _ItemPublicationPointer(
                    command.item_id,
                    command.operation_id,
                    command.fingerprint,
                    validated.publication_sha256,
                )
            )
        return {
            item_id: tuple(
                sorted(
                    pointers,
                    key=lambda value: value.operation_id,
                )
            )
            for item_id, pointers in values.items()
        }

    def _assert_item_pointer_capacity(self, item_id: str) -> None:
        relative = _ITEM_POINTER_ROOT / _digest_text(item_id)
        paths = self._authority_document_paths(
            relative,
            artifact="correction_transform_item_index",
            maximum_entries=_MAX_ITEM_TRANSFORMS,
        )
        if len(paths) >= _MAX_ITEM_TRANSFORMS:
            raise RepositoryError(
                "the correction transform history for this item is full",
                code="correction_transform_item_limit",
                details={
                    "item_id": item_id,
                    "limit": _MAX_ITEM_TRANSFORMS,
                },
            )

    def _ensure_item_index_migrated_locked(self) -> None:
        marker_path = self._target(_ITEM_INDEX_MARKER)
        if self._path_exists(
            marker_path,
            artifact="correction_transform_item_index_marker",
        ):
            self._validate_item_index_marker(marker_path)
            return

        publications = self._authority_document_paths(
            _PUBLICATION_ROOT,
            artifact="correction_transform_publications",
            maximum_entries=_MAX_LEGACY_MIGRATION_DOCUMENTS,
        )

        existing_by_item: dict[
            str,
            dict[str, _ItemPublicationPointer],
        ] = {}
        pending: list[tuple[Path, bytes]] = []
        for _name, path in sorted(publications.items()):
            raw = self._read_json(
                path,
                maximum=_MAX_PUBLICATION_BYTES,
                artifact="correction_transform_publication",
            )
            try:
                publication = _strict_object(
                    raw,
                    fields=_PUBLICATION_FIELDS,
                    artifact="correction_transform_publication",
                )
                command = CorrectionTransformCommand.from_dict(
                    publication["command"]
                )
                if path != self._publication_path(command.operation_id):
                    raise ValueError(
                        "publication path is not bound to its operation"
                    )
                version = publication["version"]
                if type(version) is not int or version not in {
                    1,
                    CORRECTION_TRANSFORM_PUBLICATION_VERSION,
                }:
                    raise ValueError(
                        "publication version cannot be migrated"
                    )
            except (EngineError, KeyError, TypeError, ValueError) as exc:
                raise _repository_error(
                    "the correction transform publication is invalid",
                    code="invalid_correction_transform_storage",
                    artifact="correction_transform_publication",
                    cause=exc,
                ) from exc

            if version == 1:
                # Version 1 has no representation/canvas scope and is never
                # projected. Its exact receipt remains independently
                # replayable/resolvable and must not block the v2 index.
                continue
            validated = self._validated_operation_publication(
                command.item_id,
                command.operation_id,
                verify_objects=False,
            )
            if validated is None:  # pragma: no cover - parity invariant
                raise _repository_error(
                    "the correction transform publication has no receipt",
                    code="invalid_correction_transform_storage",
                    artifact="correction_transform_publication",
                )

            item_pointers = existing_by_item.get(command.item_id)
            if item_pointers is None:
                item_pointers = {
                    value.operation_id: value
                    for value in self._item_publication_pointers(
                        command.item_id
                    )
                }
                existing_by_item[command.item_id] = item_pointers
            expected = _ItemPublicationPointer(
                command.item_id,
                command.operation_id,
                command.fingerprint,
                validated.publication_sha256,
            )
            prior = item_pointers.get(command.operation_id)
            if prior is not None:
                if prior != expected:
                    raise _repository_error(
                        "the correction transform item pointer conflicts "
                        "with its publication",
                        code="invalid_correction_transform_storage",
                        artifact="correction_transform_item_pointer",
                    )
                continue
            if len(item_pointers) >= _MAX_ITEM_TRANSFORMS:
                raise RepositoryError(
                    "the correction transform history for this item is full",
                    code="correction_transform_item_limit",
                    details={
                        "item_id": command.item_id,
                        "limit": _MAX_ITEM_TRANSFORMS,
                    },
                )
            pending.append(
                (
                    self._item_pointer_path(
                        command.item_id,
                        command.operation_id,
                    ),
                    self._item_pointer_payload(
                        command,
                        publication_sha256=validated.publication_sha256,
                    ),
                )
            )
            item_pointers[command.operation_id] = expected

        marker_payload = _canonical_json(
            {
                "schema": _ITEM_INDEX_MARKER_SCHEMA,
                "version": _ITEM_INDEX_MARKER_VERSION,
            },
            artifact="correction_transform_item_index_marker",
        )
        try:
            transaction = self._write_set.begin(
                operation_id="correction-transform-item-index-v1",
                scope="correction-transform-item-index-migration",
                metadata={
                    "indexed_publications": len(pending),
                    "publication_documents": len(publications),
                },
            )
            for path, payload in pending:
                transaction.stage_write(self._relative(path), payload)
            # The marker is last so an interrupted transaction cannot claim
            # migration completion before all item pointers are recoverable.
            transaction.stage_write(
                self._relative(marker_path),
                marker_payload,
            )
            transaction.commit(
                receipt={
                    "indexed_publications": len(pending),
                    "publication_documents": len(publications),
                }
            )
            self._unindexed_v2_cache = None
            self._unindexed_v2_overflow.clear()
            self._unindexed_v2_errors.clear()
        except WriteSetError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform item index migration failed",
                code="correction_transform_transaction_failed",
                artifact="correction_transform_item_index_marker",
                cause=exc,
                retryable=True,
            ) from exc

    def _validate_item_index_marker(self, path: Path) -> None:
        payload, _digest, _byte_count = self._read_regular(
            path,
            maximum=_MAX_ITEM_POINTER_BYTES,
            artifact="correction_transform_item_index_marker",
            collect=True,
        )
        raw = self._decode_json(
            payload,
            artifact="correction_transform_item_index_marker",
        )
        try:
            marker = _strict_object(
                raw,
                fields=_ITEM_INDEX_MARKER_FIELDS,
                artifact="correction_transform_item_index_marker",
            )
            if marker != {
                "schema": _ITEM_INDEX_MARKER_SCHEMA,
                "version": _ITEM_INDEX_MARKER_VERSION,
            }:
                raise ValueError("item index marker envelope is invalid")
            canonical = _canonical_json(
                marker,
                artifact="correction_transform_item_index_marker",
            )
            if payload != canonical:
                raise ValueError("item index marker is not canonical")
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform item index marker is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_item_index_marker",
                cause=exc,
            ) from exc

    def _authority_document_paths(
        self,
        relative: PurePosixPath,
        *,
        artifact: str,
        maximum_entries: int,
    ) -> dict[str, Path]:
        directory = self._target(relative)
        self._safe_target(directory, artifact=artifact)
        if not os.path.lexists(directory):
            return {}
        try:
            before = directory.lstat()
            if (
                not stat.S_ISDIR(before.st_mode)
                or _is_redirecting_path(directory)
            ):
                raise ValueError("transform authority is not a directory")
            paths: dict[str, Path] = {}
            count = 0
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > maximum_entries:
                        raise ValueError(
                            "transform authority exceeds its entry budget"
                        )
                    path = directory / entry.name
                    named = path.lstat()
                    if (
                        _TRANSFORM_DOCUMENT_NAME_RE.fullmatch(entry.name) is None
                        or not entry.is_file(follow_symlinks=False)
                        or not stat.S_ISREG(named.st_mode)
                        or named.st_nlink != 1
                        or entry.is_symlink()
                        or _is_redirecting_path(path)
                    ):
                        raise ValueError(
                            "transform authority contains an invalid entry"
                        )
                    paths[entry.name] = path
            after = directory.lstat()
            if (
                _is_redirecting_path(directory)
                or not stat.S_ISDIR(after.st_mode)
                or not os.path.samestat(before, after)
                or _stable_stat_identity(before)
                != _stable_stat_identity(after)
            ):
                raise ValueError(
                    "transform authority changed while it was inspected"
                )
            return paths
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "the correction transform document authority is invalid",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc

    def _validated_item_pointer(
        self,
        path: Path,
        *,
        item_id: str,
    ) -> _ItemPublicationPointer:
        payload, _digest, _byte_count = self._read_regular(
            path,
            maximum=_MAX_ITEM_POINTER_BYTES,
            artifact="correction_transform_item_pointer",
            collect=True,
        )
        raw = self._decode_json(
            payload,
            artifact="correction_transform_item_pointer",
        )
        try:
            pointer = _strict_object(
                raw,
                fields=_ITEM_POINTER_FIELDS,
                artifact="correction_transform_item_pointer",
            )
            if (
                pointer["schema"]
                != CORRECTION_TRANSFORM_ITEM_POINTER_SCHEMA
                or type(pointer["version"]) is not int
                or pointer["version"]
                != CORRECTION_TRANSFORM_ITEM_POINTER_VERSION
                or pointer["item_id"] != item_id
                or not isinstance(pointer["operation_id"], str)
                or not pointer["operation_id"]
                or not isinstance(pointer["command_sha256"], str)
                or _SHA256_RE.fullmatch(pointer["command_sha256"]) is None
                or not isinstance(pointer["publication_sha256"], str)
                or _SHA256_RE.fullmatch(pointer["publication_sha256"]) is None
                or path
                != self._item_pointer_path(
                    item_id,
                    pointer["operation_id"],
                )
            ):
                raise ValueError("item pointer envelope is invalid")
            canonical = _canonical_json(
                pointer,
                artifact="correction_transform_item_pointer",
            )
            if payload != canonical:
                raise ValueError("item pointer is not canonical")
            return _ItemPublicationPointer(
                item_id,
                pointer["operation_id"],
                pointer["command_sha256"],
                pointer["publication_sha256"],
            )
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform item pointer is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_item_pointer",
                cause=exc,
            ) from exc

    def _validated_publication(
        self,
        pointer: _ItemPublicationPointer,
    ) -> tuple[
        CorrectionTransformCommand,
        CorrectionTransformCommitResult,
        Mapping[str, Any],
    ]:
        validated = self._validated_operation_publication(
            pointer.item_id,
            pointer.operation_id,
        )
        if validated is None:
            raise _repository_error(
                "the correction transform item pointer has no receipt",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_item_pointer",
            )
        if (
            validated.command.fingerprint != pointer.command_sha256
            or validated.publication_sha256
            != pointer.publication_sha256
        ):
            raise _repository_error(
                "the correction transform item pointer is not bound to its "
                "publication receipt",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_item_pointer",
            )
        if (
            validated.document["version"]
            != CORRECTION_TRANSFORM_PUBLICATION_VERSION
        ):
            raise _repository_error(
                "legacy correction transform publications cannot be "
                "projected without raster source scope",
                code="unsupported_correction_transform_publication",
                artifact="correction_transform_publication",
            )
        return (
            validated.command,
            validated.result,
            validated.document,
        )

    def _validated_operation_publication(
        self,
        item_id: str,
        operation_id: str,
        *,
        verify_objects: bool = True,
    ) -> _ValidatedTransformPublication | None:
        receipt = self._read_receipt(operation_id)
        if receipt is None:
            return None
        command_sha256, publication_sha256, result = receipt
        path = self._publication_path(operation_id)
        raw = self._read_json(
            path,
            maximum=_MAX_PUBLICATION_BYTES,
            artifact="correction_transform_publication",
        )
        try:
            publication = _strict_object(
                raw,
                fields=_PUBLICATION_FIELDS,
                artifact="correction_transform_publication",
            )
            command = CorrectionTransformCommand.from_dict(
                publication["command"]
            )
            if (
                publication["operation_id"] != command.operation_id
                or path != self._publication_path(command.operation_id)
                or command.operation_id != operation_id
            ):
                raise ValueError(
                    "publication path is not bound to its operation"
                )
            if command.item_id != item_id:
                return None
            if command_sha256 != command.fingerprint:
                raise ValueError(
                    "publication command is not bound to its receipt"
                )
            validated = self._validate_replay_publication(
                command,
                result,
                publication_sha256=publication_sha256,
                verify_objects=verify_objects,
            )
            return _ValidatedTransformPublication(
                command,
                result,
                validated,
                publication_sha256,
            )
        except RepositoryError:
            raise
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform publication is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_publication",
                cause=exc,
            ) from exc

    @staticmethod
    def _raster_source_from_publication(
        publication: Mapping[str, Any],
    ) -> RasterSourceRef:
        source = _strict_object(
            publication["source"],
            fields=_PUBLICATION_SOURCE_FIELDS,
            artifact="correction_transform_source",
        )
        raw = source["raster_source"]
        if not isinstance(raw, Mapping):
            raise ValueError("publication raster source must be an object")
        fields = frozenset(raw)
        if fields not in {
            _RASTER_SOURCE_FIELDS,
            _RASTER_CANVAS_SOURCE_FIELDS,
        }:
            raise ValueError(
                "publication raster source must contain its exact schema fields"
            )
        return RasterSourceRef(**raw)

    @staticmethod
    def _projected_raster_source(
        source: RasterSourceRef,
        output_revision: str,
    ) -> RasterSourceRef:
        # Keep the source representation/canvas identities so item-scoped UI
        # filters retain the derivative.  The immutable output revision names
        # its new coordinate frame; the exact parent scope remains in lineage
        # and the correction_transform extension.
        if source.canvas_id:
            return RasterSourceRef(
                source.representation_id,
                output_revision,
                source.canvas_id,
                output_revision,
            )
        return RasterSourceRef(
            source.representation_id,
            output_revision,
        )

    @staticmethod
    def _provenance_from_document(
        raw: Any,
        *,
        artifact: str,
    ) -> ArtifactProvenance:
        return ArtifactProvenance(
            **_strict_object(
                raw,
                fields=_PROVENANCE_FIELDS,
                artifact=artifact,
            )
        )

    @classmethod
    def _role_from_document(cls, raw: Any) -> SpatialRoleAssignment:
        value = dict(
            _strict_object(
                raw,
                fields=_ROLE_FIELDS,
                artifact="correction_transform_mapped_role",
            )
        )
        value["provenance"] = cls._provenance_from_document(
            value["provenance"],
            artifact="correction_transform_mapped_role_provenance",
        )
        return SpatialRoleAssignment(**value)

    @classmethod
    def _caption_from_document(cls, raw: Any) -> CaptionAssertion:
        value = dict(
            _strict_object(
                raw,
                fields=_CAPTION_FIELDS,
                artifact="correction_transform_mapped_caption",
            )
        )
        value["provenance"] = cls._provenance_from_document(
            value["provenance"],
            artifact="correction_transform_mapped_caption_provenance",
        )
        return CaptionAssertion(**value)

    @classmethod
    def _preserved_spatial_assertions(
        cls,
        publication: Mapping[str, Any],
    ) -> dict[
        str,
        tuple[
            str,
            tuple[SpatialRoleAssignment, ...],
            tuple[CaptionAssertion, ...],
        ],
    ]:
        human = _strict_object(
            publication["human_assertions"],
            fields=_HUMAN_ASSERTION_FIELDS,
            artifact="correction_transform_human_assertions",
        )
        values = cls._require_sequence(
            human["spatial"],
            field="human_assertions.spatial",
        )
        result: dict[
            str,
            tuple[
                str,
                tuple[SpatialRoleAssignment, ...],
                tuple[CaptionAssertion, ...],
            ],
        ] = {}
        for raw in values:
            preserved = _strict_object(
                raw,
                fields=_PRESERVED_SPATIAL_FIELDS,
                artifact="correction_transform_preserved_spatial",
            )
            annotation_id = preserved["annotation_id"]
            annotation_revision = preserved["annotation_revision"]
            if (
                not isinstance(annotation_id, str)
                or not isinstance(annotation_revision, str)
                or annotation_id in result
            ):
                raise ValueError(
                    "preserved spatial assertions have invalid source pins"
                )
            roles = tuple(
                cls._role_from_document(value)
                for value in cls._require_sequence(
                    preserved["roles"],
                    field="human_assertions.spatial.roles",
                )
            )
            captions = tuple(
                cls._caption_from_document(value)
                for value in cls._require_sequence(
                    preserved["captions"],
                    field="human_assertions.spatial.captions",
                )
            )
            if (
                len({value.origin for value in roles}) != len(roles)
                or len({value.origin for value in captions})
                != len(captions)
            ):
                raise ValueError(
                    "preserved human assertions must have unique origins"
                )
            result[annotation_id] = (
                annotation_revision,
                roles,
                captions,
            )
        return result

    @staticmethod
    def _mapped_annotation_identity(
        command_sha256: str,
        source_annotation_id: str,
        target_artifact_id: str,
    ) -> str:
        payload = (
            f"{command_sha256}\0{source_annotation_id}\0"
            f"{target_artifact_id}\0mapped-annotation"
        ).encode("utf-8")
        return "ctr-ann-" + hashlib.sha256(payload).hexdigest()[:40]

    @staticmethod
    def _mapped_annotation_revision(
        command_sha256: str,
        target_revision: str,
        raw: Mapping[str, Any],
    ) -> str:
        payload = (
            command_sha256.encode("ascii")
            + b"\0"
            + target_revision.encode("ascii")
            + b"\0"
            + _canonical_json(
                raw,
                artifact="correction_transform_mapped_annotation",
            )
        )
        return "ctr-ann:" + hashlib.sha256(payload).hexdigest()

    def _mapped_annotation_views(
        self,
        command: CorrectionTransformCommand,
        result: CorrectionTransformCommitResult,
        publication: Mapping[str, Any],
        source_scope: RasterSourceRef,
    ) -> tuple[SpatialAnnotationView, ...]:
        raw_values = self._require_sequence(
            publication["mapped_annotations"],
            field="mapped_annotations",
        )
        dropped = self._require_sequence(
            publication["dropped_annotation_ids"],
            field="dropped_annotation_ids",
        )
        source = _strict_object(
            publication["source"],
            fields=_PUBLICATION_SOURCE_FIELDS,
            artifact="correction_transform_source",
        )
        pins = self._validate_dependent_revision_pins(
            source["dependent_revision_pins"]
        )["spatial_annotations"]
        preserved_assertions = self._preserved_spatial_assertions(
            publication
        )
        if raw_values and not source_scope.canvas_id:
            raise ValueError(
                "mapped correction annotations require a source canvas"
            )
        target = result.output("corrected-display")
        source_for_target = (
            SpatialSourceRef(
                source_scope.representation_id,
                target.artifact_revision,
                source_scope.canvas_id,
                target.artifact_revision,
            )
            if source_scope.canvas_id
            else None
        )
        values: list[SpatialAnnotationView] = []
        mapped_ids: set[str] = set()
        for raw in raw_values:
            mapped = _strict_object(
                raw,
                fields=_MAPPED_ANNOTATION_FIELDS,
                artifact="correction_transform_mapped_annotation",
            )
            annotation_id = mapped["annotation_id"]
            source_revision = mapped["source_revision"]
            SpatialAnnotationKey(command.item_id, annotation_id)
            if (
                annotation_id in mapped_ids
                or pins.get(annotation_id) != source_revision
            ):
                raise ValueError(
                    "mapped annotation is not bound to a unique source pin"
                )
            mapped_ids.add(annotation_id)
            if mapped["coordinate_space"] != "corrected-output-normalized":
                raise ValueError(
                    "mapped annotation coordinate space is invalid"
                )
            points_raw = self._require_sequence(
                mapped["points"],
                field="mapped_annotation.points",
            )
            points = tuple(
                NormalizedPoint(
                    **_strict_object(
                        point,
                        fields=_POINT_FIELDS,
                        artifact="correction_transform_mapped_point",
                    )
                )
                for point in points_raw
            )
            if source_for_target is None:
                raise ValueError(
                    "mapped correction annotation source is unavailable"
                )
            selector = NormalizedPolygonSelector(
                "corrected-output-normalized",
                target.artifact_revision,
                points,
            )
            roles_raw = self._require_sequence(
                mapped["role_assignments"],
                field="mapped_annotation.role_assignments",
            )
            captions_raw = self._require_sequence(
                mapped["caption_assertions"],
                field="mapped_annotation.caption_assertions",
            )
            roles = tuple(
                self._role_from_document(value)
                for value in roles_raw
            )
            captions = tuple(
                self._caption_from_document(value)
                for value in captions_raw
            )
            preserved = preserved_assertions.get(annotation_id)
            if preserved is not None:
                preserved_revision, preserved_roles, preserved_captions = (
                    preserved
                )
                if (
                    preserved_revision != source_revision
                    or any(value not in roles for value in preserved_roles)
                    or any(
                        value not in captions
                        for value in preserved_captions
                    )
                ):
                    raise ValueError(
                        "mapped annotation drops preserved human assertions"
                    )
            links_raw = self._require_sequence(
                mapped["linked_artifact_ids"],
                field="mapped_annotation.linked_artifact_ids",
            )
            source_links: list[str] = []
            for linked_artifact_id in links_raw:
                RasterArtifactKey(command.item_id, linked_artifact_id)
                source_links.append(linked_artifact_id)
            if (
                len(source_links) > 64
                or len({value.casefold() for value in source_links})
                != len(source_links)
            ):
                raise ValueError(
                    "mapped annotation source links must be bounded and unique"
                )
            identity = self._mapped_annotation_identity(
                command.fingerprint,
                annotation_id,
                target.artifact_id,
            )
            revision = self._mapped_annotation_revision(
                command.fingerprint,
                target.artifact_revision,
                mapped,
            )
            values.append(
                SpatialAnnotationView(
                    key=SpatialAnnotationKey(command.item_id, identity),
                    revision=revision,
                    source=source_for_target,
                    selector=selector,
                    order=mapped["order"],
                    label=mapped["label"],
                    freshness=ArtifactFreshness.UNTRACKED,
                    role_assignments=roles,
                    caption_assertions=captions,
                    linked_artifact_ids=(target.artifact_id,),
                    provenance=ArtifactProvenance(
                        origin="transform",
                        recipe_revision="correction-transform-v1",
                        operation_id=command.operation_id,
                    ),
                    extensions={
                        "correction_transform": {
                            "operation_id": command.operation_id,
                            "source_annotation_id": annotation_id,
                            "source_annotation_revision": source_revision,
                            "source_linked_artifact_ids": source_links,
                            "source_scope": source_scope.as_dict(),
                            "target_artifact_id": target.artifact_id,
                            "target_artifact_revision": (
                                target.artifact_revision
                            ),
                        }
                    },
                )
            )
        dropped_ids: set[str] = set()
        for annotation_id in dropped:
            SpatialAnnotationKey(command.item_id, annotation_id)
            if annotation_id in dropped_ids:
                raise ValueError(
                    "dropped annotation identities must be unique"
                )
            dropped_ids.add(annotation_id)
        if (
            mapped_ids & dropped_ids
            or (mapped_ids | dropped_ids) != set(pins)
        ):
            raise ValueError(
                "mapped and dropped annotations do not cover source pins"
            )
        if any(
            pins.get(annotation_id) != preserved[0]
            for annotation_id, preserved in preserved_assertions.items()
        ):
            raise ValueError(
                "preserved human assertions are not bound to source pins"
            )
        return tuple(values)

    def _snapshot_resource(
        self,
        candidate: _TransformResourceCandidate,
    ) -> ResolvedRasterResource:
        try:
            snapshot = tempfile.TemporaryFile(mode="w+b")
        except OSError as exc:
            raise _repository_error(
                "a correction transform resource cannot be resolved",
                code="correction_transform_resource_unavailable",
                artifact=candidate.artifact,
                cause=exc,
                retryable=True,
            ) from exc
        try:
            _payload, digest, size = self._read_regular(
                candidate.path,
                maximum=_MAX_OUTPUT_BYTES,
                artifact=candidate.artifact,
                collect=False,
                sink=snapshot,
            )
            if (
                size != candidate.size
                or digest != candidate.content_sha256
            ):
                raise _repository_error(
                    "a correction transform resource does not match its pins",
                    code="invalid_correction_transform_storage",
                    artifact=candidate.artifact,
                )
            snapshot.seek(0)
            return ResolvedRasterResource(
                snapshot,
                candidate.media_type,
                candidate.content_sha256,
                candidate.size,
                candidate.revision,
            )
        except BaseException:
            snapshot.close()
            raise

    @staticmethod
    def _resource_id(item_id: str, artifact_id: str) -> str:
        digest = hashlib.sha256(
            f"{item_id}\0{artifact_id}".encode("utf-8")
        ).hexdigest()
        return f"correction-raster:{digest[:40]}"

    def _load_source_locked(
        self,
        key: RasterArtifactKey,
    ) -> CorrectionSourceSnapshot:
        try:
            source = self._source_snapshot_for(key)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction source authority is unavailable",
                code="correction_transform_authority_unavailable",
                cause=exc,
                retryable=True,
            ) from exc
        if source is None:
            raise NotFoundError(
                "the correction source artifact does not exist",
                code="raster_artifact_not_found",
                details={
                    "item_id": key.item_id,
                    "artifact_id": key.artifact_id,
                },
            )
        if not isinstance(source, CorrectionSourceSnapshot):
            raise _repository_error(
                "the correction source authority returned an invalid snapshot",
                code="invalid_correction_transform_authority_snapshot",
            )
        if source.artifact.key != key:
            raise _repository_error(
                "the correction source authority returned another artifact",
                code="invalid_correction_transform_authority_snapshot",
            )
        return source

    def _validate_draft(self, draft: CorrectionTransformCommitDraft) -> None:
        if _source_pins(draft.source) != _command_pins(draft):
            raise ConflictError(
                "correction draft is not pinned to its command source",
                code="correction_source_stale",
                details={
                    "expected": _command_pins(draft),
                    "actual": _source_pins(draft.source),
                },
            )
        preserved = CorrectionHumanAssertions.from_source(draft.source)
        if draft.human_assertions != preserved:
            raise _repository_error(
                "the correction draft does not preserve its human assertions",
                code="invalid_correction_transform_draft",
                artifact="human_assertions",
            )
        for output in draft.outputs:
            if output.provenance.operation_id != draft.command.operation_id:
                raise _repository_error(
                    "a correction output has unrelated provenance",
                    code="invalid_correction_transform_draft",
                    artifact=output.kind,
                )

    def _compare_source(
        self,
        draft: CorrectionTransformCommitDraft,
        live: CorrectionSourceSnapshot,
    ) -> None:
        expected = _command_pins(draft)
        actual = _source_pins(live)
        if actual != expected:
            raise ConflictError(
                "correction source changed before commit",
                code="correction_source_stale",
                details={"expected": expected, "actual": actual},
            )
        expected_scope = draft.source.artifact.source
        actual_scope = live.artifact.source
        if actual_scope != expected_scope:
            raise ConflictError(
                "correction source scope changed before commit",
                code="correction_source_stale",
                details={
                    "expected": expected_scope.as_dict(),
                    "actual": actual_scope.as_dict(),
                },
            )
        expected_dependencies = draft.source.dependent_revision_pins
        actual_dependencies = live.dependent_revision_pins
        if actual_dependencies != expected_dependencies:
            raise ConflictError(
                "correction assertions changed before commit",
                code="correction_assertions_stale",
                details={
                    "expected": expected_dependencies,
                    "actual": actual_dependencies,
                },
            )
        if CorrectionHumanAssertions.from_source(live) != draft.human_assertions:
            raise ConflictError(
                "human correction assertions changed before commit",
                code="correction_assertions_stale",
                details={
                    "expected": draft.human_assertions.as_dict(),
                    "actual": CorrectionHumanAssertions.from_source(live).as_dict(),
                },
            )

    def _publish(
        self,
        draft: CorrectionTransformCommitDraft,
        result: CorrectionTransformCommitResult,
    ) -> None:
        operation_id = draft.command.operation_id
        self._ensure_item_index_migrated_locked()
        publication = self._publication_document(draft, result)
        try:
            self._validate_publication_document(
                publication,
                command=draft.command,
                result=result,
            )
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform publication draft is invalid",
                code="invalid_correction_transform_draft",
                artifact="correction_transform_publication",
                cause=exc,
            ) from exc
        publication_payload = _canonical_json(
            publication,
            artifact="correction_transform_publication",
        )
        if len(publication_payload) > _MAX_PUBLICATION_BYTES:
            raise _repository_error(
                "the correction transform publication is too large",
                code="invalid_correction_transform_document",
                artifact="correction_transform_publication",
            )
        receipt = {
            "schema": CORRECTION_TRANSFORM_RECEIPT_SCHEMA,
            "version": CORRECTION_TRANSFORM_RECEIPT_VERSION,
            "operation_id": operation_id,
            "command_sha256": draft.command.fingerprint,
            "publication_sha256": _sha256(publication_payload),
            "result": result.as_dict(),
        }
        receipt_payload = _canonical_json(
            receipt,
            artifact="correction_transform_receipt",
        )
        if len(receipt_payload) > _MAX_RECEIPT_BYTES:
            raise _repository_error(
                "the correction transform receipt is too large",
                code="invalid_correction_transform_document",
                artifact="correction_transform_receipt",
            )
        pointer_payload = self._item_pointer_payload(
            draft.command,
            publication_sha256=receipt["publication_sha256"],
        )

        targets: list[tuple[Path, bytes, str]] = []
        for kind in CORRECTION_OUTPUT_KINDS:
            output = draft.output(kind)
            if len(output.content) > _MAX_OUTPUT_BYTES:
                raise _repository_error(
                    "a correction transform output is too large",
                    code="invalid_correction_transform_document",
                    artifact=kind,
                )
            committed = result.output(kind)
            targets.append(
                (
                    self._object_path(committed.artifact_id),
                    output.content,
                    kind,
                )
            )
        targets.extend(
            (
                (
                    self._publication_path(operation_id),
                    publication_payload,
                    "correction_transform_publication",
                ),
                (
                    self._item_pointer_path(
                        draft.command.item_id,
                        operation_id,
                    ),
                    pointer_payload,
                    "correction_transform_item_pointer",
                ),
                (
                    self._receipt_path(operation_id),
                    receipt_payload,
                    "correction_transform_receipt",
                ),
            )
        )
        for path, _payload, artifact in targets:
            if self._path_exists(path, artifact=artifact):
                raise _repository_error(
                    "an immutable correction transform target already exists",
                    code="correction_transform_target_exists",
                    artifact=artifact,
                )
        self._assert_item_pointer_capacity(draft.command.item_id)

        try:
            transaction = self._write_set.begin(
                operation_id=operation_id,
                scope="correction-transform",
                metadata={
                    "item_id": draft.command.item_id,
                    "source_artifact_id": draft.command.artifact_id,
                    "command_sha256": draft.command.fingerprint,
                },
            )
            for path, payload, _artifact in targets:
                transaction.stage_write(self._relative(path), payload)
            transaction.commit(
                receipt={
                    "operation_id": operation_id,
                    "outputs": [value.as_dict() for value in result.outputs],
                }
            )
        except WriteSetError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform publication failed",
                code="correction_transform_transaction_failed",
                cause=exc,
                retryable=True,
            ) from exc

    def _item_pointer_payload(
        self,
        command: CorrectionTransformCommand,
        *,
        publication_sha256: str,
    ) -> bytes:
        pointer = {
            "schema": CORRECTION_TRANSFORM_ITEM_POINTER_SCHEMA,
            "version": CORRECTION_TRANSFORM_ITEM_POINTER_VERSION,
            "item_id": command.item_id,
            "operation_id": command.operation_id,
            "command_sha256": command.fingerprint,
            "publication_sha256": publication_sha256,
        }
        payload = _canonical_json(
            pointer,
            artifact="correction_transform_item_pointer",
        )
        if len(payload) > _MAX_ITEM_POINTER_BYTES:
            raise _repository_error(
                "the correction transform item pointer is too large",
                code="invalid_correction_transform_document",
                artifact="correction_transform_item_pointer",
            )
        return payload

    def _ensure_item_pointer(
        self,
        command: CorrectionTransformCommand,
        *,
        publication_sha256: str,
    ) -> None:
        path = self._item_pointer_path(
            command.item_id,
            command.operation_id,
        )
        expected = _ItemPublicationPointer(
            command.item_id,
            command.operation_id,
            command.fingerprint,
            publication_sha256,
        )
        if self._path_exists(
            path,
            artifact="correction_transform_item_pointer",
        ):
            actual = self._validated_item_pointer(
                path,
                item_id=command.item_id,
            )
            if actual != expected:
                raise _repository_error(
                    "the correction transform item pointer conflicts with "
                    "its publication",
                    code="invalid_correction_transform_storage",
                    artifact="correction_transform_item_pointer",
                )
            return

        self._assert_item_pointer_capacity(command.item_id)
        payload = self._item_pointer_payload(
            command,
            publication_sha256=publication_sha256,
        )
        try:
            transaction = self._write_set.begin(
                operation_id=f"{command.operation_id}:item-index",
                scope="correction-transform-item-index",
                metadata={
                    "item_id": command.item_id,
                    "operation_id": command.operation_id,
                    "command_sha256": command.fingerprint,
                },
            )
            transaction.stage_write(self._relative(path), payload)
            transaction.commit(
                receipt={
                    "item_id": command.item_id,
                    "operation_id": command.operation_id,
                    "publication_sha256": publication_sha256,
                }
            )
            self._unindexed_v2_cache = None
            self._unindexed_v2_overflow.clear()
            self._unindexed_v2_errors.clear()
        except WriteSetError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction transform item index could not be repaired",
                code="correction_transform_transaction_failed",
                artifact="correction_transform_item_pointer",
                cause=exc,
                retryable=True,
            ) from exc

    def _publication_document(
        self,
        draft: CorrectionTransformCommitDraft,
        result: CorrectionTransformCommitResult,
    ) -> dict[str, Any]:
        outputs: list[dict[str, Any]] = []
        for kind in CORRECTION_OUTPUT_KINDS:
            staged = draft.output(kind)
            committed = result.output(kind)
            outputs.append(
                {
                    **staged.as_dict(),
                    "artifact_id": committed.artifact_id,
                    "artifact_revision": committed.artifact_revision,
                    "storage": "immutable-object-v1",
                }
            )
        return {
            "schema": CORRECTION_TRANSFORM_PUBLICATION_SCHEMA,
            "version": CORRECTION_TRANSFORM_PUBLICATION_VERSION,
            "operation_id": draft.command.operation_id,
            "command_sha256": draft.command.fingerprint,
            "command": draft.command.as_dict(),
            "source": {
                **_source_pins(draft.source),
                "dependent_revision_pins": (draft.source.dependent_revision_pins),
                "raster_source": draft.source.artifact.source.as_dict(),
            },
            "result": result.as_dict(),
            "outputs": outputs,
            "mapped_annotations": [
                value.as_dict() for value in draft.mapped_annotations
            ],
            "dropped_annotation_ids": list(draft.dropped_annotation_ids),
            "human_assertions": draft.human_assertions.as_dict(),
            "human_assertion_policy": "carry-separately-never-overwrite",
        }

    def _read_receipt(
        self,
        operation_id: str,
    ) -> tuple[str, str, CorrectionTransformCommitResult] | None:
        path = self._receipt_path(operation_id)
        if not self._path_exists(
            path,
            artifact="correction_transform_receipt",
        ):
            return None
        raw = self._read_json(
            path,
            maximum=_MAX_RECEIPT_BYTES,
            artifact="correction_transform_receipt",
        )
        try:
            receipt = _strict_object(
                raw,
                fields=_RECEIPT_FIELDS,
                artifact="correction_transform_receipt",
            )
            if (
                receipt["schema"] != CORRECTION_TRANSFORM_RECEIPT_SCHEMA
                or type(receipt["version"]) is not int
                or receipt["version"] != CORRECTION_TRANSFORM_RECEIPT_VERSION
                or receipt["operation_id"] != operation_id
                or not isinstance(receipt["command_sha256"], str)
                or not _SHA256_RE.fullmatch(receipt["command_sha256"])
                or not isinstance(receipt["publication_sha256"], str)
                or not _SHA256_RE.fullmatch(receipt["publication_sha256"])
            ):
                raise ValueError("receipt envelope is invalid")
            result = self._result_from_document(
                receipt["result"],
                operation_id=operation_id,
                artifact="correction_transform_result",
            )
            return (
                receipt["command_sha256"],
                receipt["publication_sha256"],
                result,
            )
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform receipt is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_receipt",
                cause=exc,
            ) from exc

    def _result_from_document(
        self,
        raw: Any,
        *,
        operation_id: str,
        artifact: str,
    ) -> CorrectionTransformCommitResult:
        result_raw = _strict_object(
            raw,
            fields=_RESULT_FIELDS,
            artifact=artifact,
        )
        if (
            result_raw["operation_id"] != operation_id
            or isinstance(result_raw["outputs"], (str, bytes))
            or not isinstance(result_raw["outputs"], Sequence)
        ):
            raise ValueError(f"{artifact} is invalid")
        outputs = tuple(
            CommittedCorrectionOutput(
                **_strict_object(
                    value,
                    fields=_COMMITTED_OUTPUT_FIELDS,
                    artifact="correction_transform_output",
                )
            )
            for value in result_raw["outputs"]
        )
        if tuple(output.kind for output in outputs) != CORRECTION_OUTPUT_KINDS:
            raise ValueError(f"{artifact} output order is invalid")
        return CorrectionTransformCommitResult(operation_id, outputs)

    def _validate_replay_publication(
        self,
        command: CorrectionTransformCommand,
        result: CorrectionTransformCommitResult,
        *,
        publication_sha256: str,
        verify_objects: bool = True,
    ) -> Mapping[str, Any]:
        artifact_ids = tuple(output.artifact_id for output in result.outputs)
        if command.artifact_id.casefold() in {
            artifact_id.casefold() for artifact_id in artifact_ids
        } or len({artifact_id.casefold() for artifact_id in artifact_ids}) != len(
            artifact_ids
        ):
            raise _repository_error(
                "the correction transform receipt reuses an artifact identity",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_receipt",
            )
        reserved = {command.artifact_id.casefold()}
        for output in result.outputs:
            if (
                output.artifact_id
                != _output_identity(command.fingerprint, output.kind, reserved)
                or output.artifact_revision
                != _output_revision(
                    command.fingerprint,
                    output.kind,
                    output.content_sha256,
                )
            ):
                raise _repository_error(
                    "the correction transform receipt identity is invalid",
                    code="invalid_correction_transform_storage",
                    artifact="correction_transform_receipt",
                )

        publication_payload, _, _ = self._read_regular(
            self._publication_path(command.operation_id),
            maximum=_MAX_PUBLICATION_BYTES,
            artifact="correction_transform_publication",
            collect=True,
        )
        if _sha256(publication_payload) != publication_sha256:
            raise _repository_error(
                "the correction transform publication checksum is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_publication",
            )
        publication = self._decode_json(
            publication_payload,
            artifact="correction_transform_publication",
        )
        try:
            self._validate_publication_document(
                publication,
                command=command,
                result=result,
                allow_legacy=True,
            )
            canonical = _canonical_json(
                publication,
                artifact="correction_transform_publication",
            )
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction transform publication is invalid",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_publication",
                cause=exc,
            ) from exc
        if publication_payload != canonical:
            raise _repository_error(
                "the correction transform publication is not canonical",
                code="invalid_correction_transform_storage",
                artifact="correction_transform_publication",
            )

        if verify_objects:
            publication_outputs = publication["outputs"]
            for committed, descriptor in zip(
                result.outputs,
                publication_outputs,
                strict=True,
            ):
                _payload, digest, byte_count = self._read_regular(
                    self._object_path(committed.artifact_id),
                    maximum=_MAX_OUTPUT_BYTES,
                    artifact=committed.kind,
                    collect=False,
                )
                if (
                    digest != committed.content_sha256
                    or byte_count != descriptor["bytes"]
                ):
                    raise _repository_error(
                        "a correction transform object checksum is invalid",
                        code="invalid_correction_transform_storage",
                        artifact=committed.kind,
                    )
        return publication

    def _validate_publication_document(
        self,
        raw: Any,
        *,
        command: CorrectionTransformCommand,
        result: CorrectionTransformCommitResult,
        allow_legacy: bool = False,
    ) -> None:
        publication = _strict_object(
            raw,
            fields=_PUBLICATION_FIELDS,
            artifact="correction_transform_publication",
        )
        version = publication["version"]
        supported_versions = (
            {1, CORRECTION_TRANSFORM_PUBLICATION_VERSION}
            if allow_legacy
            else {CORRECTION_TRANSFORM_PUBLICATION_VERSION}
        )
        if (
            publication["schema"] != CORRECTION_TRANSFORM_PUBLICATION_SCHEMA
            or type(version) is not int
            or version not in supported_versions
            or publication["operation_id"] != command.operation_id
            or publication["command_sha256"] != command.fingerprint
            or publication["human_assertion_policy"]
            != "carry-separately-never-overwrite"
        ):
            raise ValueError("publication envelope is invalid")
        stored_command = CorrectionTransformCommand.from_dict(publication["command"])
        if stored_command != command or stored_command.fingerprint != command.fingerprint:
            raise ValueError("publication command is not bound to its receipt")

        source = _strict_object(
            publication["source"],
            fields=(
                _PUBLICATION_SOURCE_V1_FIELDS
                if version == 1
                else _PUBLICATION_SOURCE_FIELDS
            ),
            artifact="correction_transform_source",
        )
        source_pins = {
            key: source[key]
            for key in (
                "item_id",
                "artifact_id",
                "artifact_revision",
                "source_revision",
                "source_sha256",
            )
        }
        if source_pins != {
            "item_id": command.item_id,
            "artifact_id": command.artifact_id,
            "artifact_revision": command.artifact_revision,
            "source_revision": command.source_revision,
            "source_sha256": command.source_sha256,
        }:
            raise ValueError("publication source is not bound to its command")
        self._validate_dependent_revision_pins(source["dependent_revision_pins"])
        raster_source = (
            None
            if version == 1
            else self._raster_source_from_publication(publication)
        )

        publication_result = self._result_from_document(
            publication["result"],
            operation_id=command.operation_id,
            artifact="correction_transform_publication_result",
        )
        if publication_result != result:
            raise ValueError("publication result does not match its receipt")

        outputs_raw = publication["outputs"]
        if isinstance(outputs_raw, (str, bytes)) or not isinstance(
            outputs_raw,
            Sequence,
        ):
            raise ValueError("publication outputs must be a sequence")
        if len(outputs_raw) != len(result.outputs):
            raise ValueError("publication outputs are incomplete")
        for committed, raw_output in zip(result.outputs, outputs_raw, strict=True):
            output = _strict_object(
                raw_output,
                fields=_PUBLICATION_OUTPUT_FIELDS,
                artifact="correction_transform_publication_output",
            )
            media_type = output["media_type"]
            dimensions = output["dimensions"]
            if isinstance(media_type, str) and media_type.startswith("image/"):
                RasterDimensions(
                    **_strict_object(
                        dimensions,
                        fields=_DIMENSION_FIELDS,
                        artifact="correction_transform_output_dimensions",
                    )
                )
            elif dimensions is not None:
                raise ValueError("non-raster correction output has dimensions")
            provenance = ArtifactProvenance(
                **_strict_object(
                    output["provenance"],
                    fields=_PROVENANCE_FIELDS,
                    artifact="correction_transform_output_provenance",
                )
            )
            expected_provenance = ArtifactProvenance(
                origin="transform",
                recipe_revision="correction-transform-v1",
                operation_id=command.operation_id,
            )
            expected_media_type = (
                "application/json"
                if committed.kind == "transform-manifest"
                else "image/png"
            )
            if (
                output["kind"] != committed.kind
                or output["artifact_id"] != committed.artifact_id
                or output["artifact_revision"] != committed.artifact_revision
                or output["content_sha256"] != committed.content_sha256
                or output["storage"] != "immutable-object-v1"
                or type(output["bytes"]) is not int
                or output["bytes"] <= 0
                or media_type != expected_media_type
                or provenance != expected_provenance
            ):
                raise ValueError("publication output is not bound to its result")

        human_assertions = _strict_object(
            publication["human_assertions"],
            fields=_HUMAN_ASSERTION_FIELDS,
            artifact="correction_transform_human_assertions",
        )
        for field in _HUMAN_ASSERTION_FIELDS:
            self._require_sequence(human_assertions[field], field=field)
        if raster_source is None:
            self._require_sequence(
                publication["mapped_annotations"],
                field="mapped_annotations",
            )
            dropped = self._require_sequence(
                publication["dropped_annotation_ids"],
                field="dropped_annotation_ids",
            )
            if any(not isinstance(value, str) for value in dropped):
                raise ValueError(
                    "dropped annotation identities must be strings"
                )
        else:
            self._mapped_annotation_views(
                command,
                result,
                publication,
                raster_source,
            )

    def _validate_dependent_revision_pins(
        self,
        raw: Any,
    ) -> dict[str, dict[str, str]]:
        pins = _strict_object(
            raw,
            fields=frozenset({"spatial_annotations", "human_text_assertions"}),
            artifact="correction_transform_dependent_revision_pins",
        )
        result: dict[str, dict[str, str]] = {}
        for field, identity_field in (
            ("spatial_annotations", "annotation_id"),
            ("human_text_assertions", "assertion_id"),
        ):
            values = self._require_sequence(pins[field], field=field)
            identities: dict[str, str] = {}
            normalized_identities: set[str] = set()
            for value in values:
                pin = _strict_object(
                    value,
                    fields=frozenset({identity_field, "revision"}),
                    artifact="correction_transform_dependent_revision_pin",
                )
                if not isinstance(pin[identity_field], str) or not isinstance(
                    pin["revision"],
                    str,
                ):
                    raise ValueError("dependent revision pin values must be strings")
                identity = pin[identity_field]
                if field == "spatial_annotations":
                    SpatialAnnotationKey("correction-transform-pin", identity)
                else:
                    RasterArtifactKey("correction-transform-pin", identity)
                RasterLineageRef(
                    "correction-transform-pin",
                    pin["revision"],
                )
                normalized = identity.casefold()
                if normalized in normalized_identities:
                    raise ValueError(
                        "dependent revision pin identities must be unique"
                    )
                normalized_identities.add(normalized)
                identities[identity] = pin["revision"]
            result[field] = identities
        return result

    @staticmethod
    def _require_sequence(raw: Any, *, field: str) -> Sequence[Any]:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise ValueError(f"publication {field} must be a sequence")
        return raw

    def _read_json(
        self,
        path: Path,
        *,
        maximum: int,
        artifact: str,
    ) -> Any:
        payload, _digest, _byte_count = self._read_regular(
            path,
            maximum=maximum,
            artifact=artifact,
            collect=True,
        )
        return self._decode_json(payload, artifact=artifact)

    def _decode_json(self, payload: bytes, *, artifact: str) -> Any:
        try:
            return json.loads(
                payload.decode("ascii"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
        except (UnicodeError, TypeError, ValueError, RecursionError) as exc:
            raise _repository_error(
                "a correction transform document cannot be read",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc

    def _read_regular(
        self,
        path: Path,
        *,
        maximum: int,
        artifact: str,
        collect: bool,
        sink: Any = None,
    ) -> tuple[bytes, str, int]:
        if sink is not None and not callable(getattr(sink, "write", None)):
            raise TypeError("sink must expose write()")
        descriptor = -1
        try:
            authority = self._authority_snapshot(path, artifact=artifact)
            named_before = path.lstat()
            if (
                not stat.S_ISREG(named_before.st_mode)
                or named_before.st_nlink != 1
                or _is_redirecting_path(path)
            ):
                raise ValueError("target is not a private regular file")
            descriptor, opened_before = _open_verified_regular(
                path,
                named_before,
                authority=authority,
            )
            digest = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                total += len(block)
                if total > maximum:
                    raise ValueError("target exceeds its encoded size budget")
                digest.update(block)
                if collect:
                    chunks.append(block)
                if sink is not None:
                    sink.write(block)
            _finish_verified_regular(
                path,
                descriptor,
                named_before=named_before,
                opened_before=opened_before,
            )
            self._authority_snapshot(path, artifact=artifact)
            return (b"".join(chunks), digest.hexdigest(), total)
        except (OSError, TypeError, ValueError) as exc:
            raise _repository_error(
                "a correction transform storage object cannot be read",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _authority_snapshot(
        self,
        path: Path,
        *,
        artifact: str,
    ) -> _AuthoritySnapshot:
        target = self._safe_target(path, artifact=artifact)
        root = self._write_set.root
        relative = target.relative_to(root)
        try:
            named_root = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise _repository_error(
                "the correction transform authority root cannot be inspected",
                code="unsafe_correction_transform_path",
                artifact=artifact,
                cause=exc,
            ) from exc
        if _is_redirecting_path(root) or not stat.S_ISDIR(named_root.st_mode):
            raise _repository_error(
                "the correction transform authority root is unsafe",
                code="unsafe_correction_transform_path",
                artifact=artifact,
            )

        directories: list[_AuthorityDirectorySnapshot] = []
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a correction transform target crosses a redirecting path",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
            try:
                named_directory = current.lstat()
            except FileNotFoundError:
                named_directory = None
            except OSError as exc:
                raise _repository_error(
                    "a correction transform authority path cannot be inspected",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                    cause=exc,
                ) from exc
            if named_directory is not None and not stat.S_ISDIR(
                named_directory.st_mode
            ):
                raise _repository_error(
                    "a correction transform authority component is not a directory",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
            directories.append(_AuthorityDirectorySnapshot(current, named_directory))

        try:
            target.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a correction transform target escapes its workspace",
                code="unsafe_correction_transform_path",
                artifact=artifact,
                cause=exc,
            ) from exc
        return _AuthoritySnapshot(root, named_root, tuple(directories))

    def _path_exists(
        self,
        path: Path,
        *,
        artifact: str,
    ) -> bool:
        self._safe_target(path, artifact=artifact)
        if not os.path.lexists(path):
            return False
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise _repository_error(
                "a correction transform storage target cannot be inspected",
                code="invalid_correction_transform_storage",
                artifact=artifact,
                cause=exc,
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _is_redirecting_path(path)
        ):
            raise _repository_error(
                "a correction transform storage target is unsafe",
                code="invalid_correction_transform_storage",
                artifact=artifact,
            )
        return True

    def _safe_target(self, path: Path, *, artifact: str) -> Path:
        target = Path(path)
        try:
            relative = target.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                "a correction transform target escapes its workspace",
                code="unsafe_correction_transform_path",
                artifact=artifact,
                cause=exc,
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise _repository_error(
                    "a correction transform target is unsafe",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
            current = current / part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a correction transform target crosses a redirecting path",
                    code="unsafe_correction_transform_path",
                    artifact=artifact,
                )
        return target

    def _object_path(self, artifact_id: str) -> Path:
        return self._target(_OBJECT_ROOT / f"{_digest_text(artifact_id)}.bin")

    def _publication_path(self, operation_id: str) -> Path:
        return self._target(_PUBLICATION_ROOT / f"{_digest_text(operation_id)}.json")

    def _item_pointer_path(
        self,
        item_id: str,
        operation_id: str,
    ) -> Path:
        return self._target(
            _ITEM_POINTER_ROOT
            / _digest_text(item_id)
            / f"{_digest_text(operation_id)}.json"
        )

    def _receipt_path(self, operation_id: str) -> Path:
        return self._target(_RECEIPT_ROOT / f"{_digest_text(operation_id)}.json")

    def _target(self, relative: PurePosixPath) -> Path:
        return self._write_set.root.joinpath(*relative.parts)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._write_set.root).as_posix()


__all__ = [
    "CORRECTION_TRANSFORM_ITEM_POINTER_SCHEMA",
    "CORRECTION_TRANSFORM_ITEM_POINTER_VERSION",
    "CORRECTION_TRANSFORM_PUBLICATION_SCHEMA",
    "CORRECTION_TRANSFORM_PUBLICATION_VERSION",
    "CORRECTION_TRANSFORM_RECEIPT_SCHEMA",
    "CORRECTION_TRANSFORM_RECEIPT_VERSION",
    "CorrectionTransformOutputResolverPort",
    "FilesystemCorrectionTransformStore",
    "SourceSnapshotLookup",
]
