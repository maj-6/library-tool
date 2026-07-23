"""Recoverable filesystem repository for correction commands.

The engine owns command validation, optimistic concurrency, and receipt
construction.  This adapter owns the durable boundary: one locked aggregate is
staged in memory, then its complete correction snapshot and immutable
idempotency receipt are published in one :class:`RecoverableWriteSet`
transaction.

Item and operation identifiers never become path components.  Their SHA-256
digests address private aggregate and receipt documents beneath ``.engine``.
An injected loader supplies the first canonical aggregate; once an item has
been mutated, the durable snapshot is authoritative across repository
re-instantiation.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.corrections import (
    AnnotationCorrectionSnapshot,
    ArtifactCorrectionSnapshot,
    ArtifactMetadataAssertion,
    AssertArtifactMetadataCommand,
    AssignImageCategoryCommand,
    AssignRegionRoleCommand,
    ClearImageCategoryCommand,
    ClearManualCaptionCommand,
    ClearRegionRoleCommand,
    CorrectionAggregateSnapshot,
    CorrectionAuditEvent,
    CorrectionCommand,
    CorrectionInverse,
    CorrectionMutationReceipt,
    CorrectionReviewSnapshot,
    CorrectionTargetRevision,
    MarkAttentionCommand,
    MetadataAssertionOrigin,
    ReopenCorrectionsCommand,
    ResolveCorrectionsCommand,
    SetManualCaptionCommand,
)
from ...engine.errors import EngineError, RepositoryError
from ...engine.raster_artifacts import (
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    RasterArtifactKey,
)
from ...engine.spatial_annotations import (
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialRoleAssignment,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    RecoverableWriteTransaction,
    WriteSetError,
    _is_redirecting_path,
)


AggregateLoader: TypeAlias = Callable[[str], CorrectionAggregateSnapshot | None]
RevisionFactory: TypeAlias = Callable[[str, str], str]
Clock: TypeAlias = Callable[[], str]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]

CORRECTION_AGGREGATE_SCHEMA = "librarytool.correction-aggregate"
CORRECTION_AGGREGATE_VERSION = 1
CORRECTION_RECEIPT_SCHEMA = "librarytool.correction-receipt"
CORRECTION_RECEIPT_VERSION = 1

_AGGREGATE_ROOT = PurePosixPath(".engine/corrections/aggregates")
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/corrections")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_AGGREGATE_BYTES = 128 * 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024


def _default_revision(_kind: str, _target_id: str) -> str:
    return "correction-" + uuid.uuid4().hex


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _repository_error(
    message: str,
    *,
    code: str,
    error: Exception | None = None,
    details: Mapping[str, Any] | None = None,
    retryable: bool = False,
) -> RepositoryError:
    safe_details = dict(details or {})
    if error is not None:
        safe_details["cause_type"] = type(error).__name__
    return RepositoryError(
        message,
        code=code,
        details=safe_details,
        retryable=retryable,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _json_bytes(value: Any, *, artifact: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _repository_error(
            "a correction repository document cannot be serialized",
            code="invalid_correction_repository_document",
            error=exc,
            details={"artifact": artifact},
        ) from exc


def _read_json(
    path: Path,
    *,
    artifact: str,
    maximum_bytes: int,
) -> Any | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise _repository_error(
            "a correction repository document is not a regular file",
            code="invalid_correction_repository_document",
            details={"artifact": artifact},
        )
    try:
        payload = path.read_bytes()
        if len(payload) > maximum_bytes:
            raise ValueError("document exceeds its encoded size budget")
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (OSError, UnicodeError, TypeError, ValueError, RecursionError) as exc:
        raise _repository_error(
            "a correction repository document cannot be read",
            code="invalid_correction_repository_document",
            error=exc,
            details={"artifact": artifact},
        ) from exc


def _object(
    value: Any,
    *,
    artifact: str,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{artifact} must contain its exact schema fields")
    return value


def _array(value: Any, *, artifact: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{artifact} must be an array")
    return value


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
_CATEGORY_FIELDS = frozenset(
    {
        "category",
        "origin",
        "revision",
        "inherited_from_artifact_id",
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
_METADATA_FIELDS = frozenset(
    {"name", "value", "origin", "revision", "provenance"}
)
_ARTIFACT_FIELDS = frozenset(
    {
        "key",
        "revision",
        "source_artifact_id",
        "category_assignments",
        "caption_assertions",
        "role_assignments",
        "metadata_assertions",
        "extensions",
    }
)
_ANNOTATION_FIELDS = frozenset(
    {
        "key",
        "revision",
        "linked_artifact_id",
        "role_assignments",
        "extensions",
    }
)
_KEY_FIELDS = frozenset({"item_id", "artifact_id"})
_ANNOTATION_KEY_FIELDS = frozenset({"item_id", "annotation_id"})
_AUDIT_FIELDS = frozenset(
    {
        "operation_id",
        "action",
        "actor_id",
        "occurred_at",
        "before_state",
        "after_state",
        "reason",
        "comment",
    }
)
_REVIEW_FIELDS = frozenset({"revision", "state", "reason", "history"})
_AGGREGATE_FIELDS = frozenset(
    {"item_id", "revision", "artifacts", "annotations", "review"}
)
_AGGREGATE_ENVELOPE_FIELDS = frozenset(
    {"schema", "version", "item_id", "aggregate"}
)
_TARGET_FIELDS = frozenset(
    {
        "kind",
        "target_id",
        "before_revision",
        "after_revision",
    }
)
_INVERSE_FIELDS = frozenset(
    {
        "action",
        "expected_aggregate_revision",
        "expected_targets",
        "payload",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "action",
        "operation_id",
        "command_sha256",
        "item_id",
        "before_aggregate_revision",
        "after_aggregate_revision",
        "targets",
        "inverse",
    }
)
_RECEIPT_ENVELOPE_FIELDS = frozenset(
    {"schema", "version", "operation_id", "receipt"}
)


def _decode_provenance(value: Any) -> ArtifactProvenance:
    raw = _object(value, artifact="provenance", fields=_PROVENANCE_FIELDS)
    return ArtifactProvenance(**raw)


def _decode_category(value: Any) -> CategoryAssignment:
    raw = dict(_object(value, artifact="category", fields=_CATEGORY_FIELDS))
    raw["provenance"] = _decode_provenance(raw["provenance"])
    return CategoryAssignment(**raw)


def _decode_caption(value: Any) -> CaptionAssertion:
    raw = dict(_object(value, artifact="caption", fields=_CAPTION_FIELDS))
    raw["provenance"] = _decode_provenance(raw["provenance"])
    return CaptionAssertion(**raw)


def _decode_role(value: Any) -> SpatialRoleAssignment:
    raw = dict(_object(value, artifact="role", fields=_ROLE_FIELDS))
    raw["provenance"] = _decode_provenance(raw["provenance"])
    return SpatialRoleAssignment(**raw)


def _decode_metadata(value: Any) -> ArtifactMetadataAssertion:
    raw = dict(_object(value, artifact="metadata", fields=_METADATA_FIELDS))
    raw["provenance"] = _decode_provenance(raw["provenance"])
    return ArtifactMetadataAssertion(**raw)


def _decode_artifact(value: Any) -> ArtifactCorrectionSnapshot:
    raw = dict(_object(value, artifact="artifact", fields=_ARTIFACT_FIELDS))
    key = _object(raw["key"], artifact="artifact.key", fields=_KEY_FIELDS)
    raw["key"] = RasterArtifactKey(**key)
    raw["category_assignments"] = tuple(
        _decode_category(item)
        for item in _array(
            raw["category_assignments"],
            artifact="artifact.category_assignments",
        )
    )
    raw["caption_assertions"] = tuple(
        _decode_caption(item)
        for item in _array(
            raw["caption_assertions"],
            artifact="artifact.caption_assertions",
        )
    )
    raw["role_assignments"] = tuple(
        _decode_role(item)
        for item in _array(
            raw["role_assignments"],
            artifact="artifact.role_assignments",
        )
    )
    raw["metadata_assertions"] = tuple(
        _decode_metadata(item)
        for item in _array(
            raw["metadata_assertions"],
            artifact="artifact.metadata_assertions",
        )
    )
    return ArtifactCorrectionSnapshot(**raw)


def _decode_annotation(value: Any) -> AnnotationCorrectionSnapshot:
    raw = dict(_object(value, artifact="annotation", fields=_ANNOTATION_FIELDS))
    key = _object(
        raw["key"],
        artifact="annotation.key",
        fields=_ANNOTATION_KEY_FIELDS,
    )
    raw["key"] = SpatialAnnotationKey(**key)
    raw["role_assignments"] = tuple(
        _decode_role(item)
        for item in _array(
            raw["role_assignments"],
            artifact="annotation.role_assignments",
        )
    )
    return AnnotationCorrectionSnapshot(**raw)


def _decode_review(value: Any) -> CorrectionReviewSnapshot:
    raw = dict(_object(value, artifact="review", fields=_REVIEW_FIELDS))
    raw["history"] = tuple(
        CorrectionAuditEvent(
            **_object(item, artifact="review.history", fields=_AUDIT_FIELDS)
        )
        for item in _array(raw["history"], artifact="review.history")
    )
    return CorrectionReviewSnapshot(**raw)


def _decode_aggregate(value: Any) -> CorrectionAggregateSnapshot:
    raw = dict(_object(value, artifact="aggregate", fields=_AGGREGATE_FIELDS))
    raw["artifacts"] = tuple(
        _decode_artifact(item)
        for item in _array(raw["artifacts"], artifact="aggregate.artifacts")
    )
    raw["annotations"] = tuple(
        _decode_annotation(item)
        for item in _array(raw["annotations"], artifact="aggregate.annotations")
    )
    raw["review"] = _decode_review(raw["review"])
    return CorrectionAggregateSnapshot(**raw)


def _decode_target(value: Any) -> CorrectionTargetRevision:
    raw = _object(value, artifact="receipt.target", fields=_TARGET_FIELDS)
    return CorrectionTargetRevision(**raw)


def _decode_receipt(value: Any) -> CorrectionMutationReceipt:
    raw = dict(_object(value, artifact="receipt", fields=_RECEIPT_FIELDS))
    raw["targets"] = tuple(
        _decode_target(item)
        for item in _array(raw["targets"], artifact="receipt.targets")
    )
    inverse = dict(
        _object(raw["inverse"], artifact="receipt.inverse", fields=_INVERSE_FIELDS)
    )
    inverse["expected_targets"] = tuple(
        _decode_target(item)
        for item in _array(
            inverse["expected_targets"],
            artifact="receipt.inverse.expected_targets",
        )
    )
    raw["inverse"] = CorrectionInverse(**inverse)
    return CorrectionMutationReceipt(**raw)


def _replace_origin(
    values: Sequence[Any],
    origin: Any,
    replacement: Any | None,
) -> tuple[Any, ...]:
    retained = tuple(value for value in values if value.origin is not origin)
    if replacement is None:
        return retained
    return (*retained, replacement)


class FilesystemCorrectionRepository:
    """Open locked correction units backed by recoverable writes."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        load_aggregate: AggregateLoader | None = None,
        revision_factory: RevisionFactory | None = None,
        clock: Clock | None = None,
        lock_context_for: LockContextFactory | None = None,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (load_aggregate, "load_aggregate"),
            (revision_factory, "revision_factory"),
            (clock, "clock"),
            (lock_context_for, "lock_context_for"),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._load_aggregate = load_aggregate
        self._revision_factory = revision_factory or _default_revision
        self._clock = clock or _default_clock
        self._lock_context_for = lock_context_for or (lambda: nullcontext())
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except WriteSetError as exc:
                raise _repository_error(
                    "the correction repository could not recover",
                    code="correction_repository_recovery_failed",
                    error=exc,
                    retryable=True,
                ) from exc

    @contextmanager
    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> Iterator["FilesystemCorrectionUnitOfWork"]:
        self._identifier(operation_id, field_name="operation_id")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    unit = FilesystemCorrectionUnitOfWork(
                        self._write_set,
                        operation_id=operation_id,
                        load_aggregate=self._load_aggregate,
                        revision_factory=self._revision_factory,
                        clock=self._clock,
                        safe_target=self._safe_target,
                    )
                    try:
                        yield unit
                    finally:
                        unit.close()
        except WriteSetError as exc:
            raise _repository_error(
                "the correction repository workspace is unavailable",
                code=exc.code,
                error=exc,
                retryable=True,
            ) from exc

    def _safe_target(self, relative: str, *, artifact: str) -> Path:
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any("\\" in part or ":" in part for part in pure.parts)
        ):
            raise _repository_error(
                "a correction repository target is unsafe",
                code="unsafe_correction_repository_path",
                details={"artifact": artifact},
            )
        target = self._write_set.root.joinpath(*pure.parts)
        current = self._write_set.root
        for part in pure.parts:
            current = current / part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a correction repository target crosses a redirecting path",
                    code="unsafe_correction_repository_path",
                    details={"artifact": artifact},
                )
        try:
            target.resolve(strict=False).relative_to(self._write_set.root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a correction repository target escapes its root",
                code="unsafe_correction_repository_path",
                error=exc,
                details={"artifact": artifact},
            ) from exc
        return target

    @staticmethod
    def _identifier(value: Any, *, field_name: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise _repository_error(
                f"{field_name} is not a portable identifier",
                code="invalid_correction_repository_identity",
                details={"field": field_name},
            )
        return value


class FilesystemCorrectionUnitOfWork:
    """One locked aggregate snapshot and explicit commit boundary."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        load_aggregate: AggregateLoader | None,
        revision_factory: RevisionFactory,
        clock: Clock,
        safe_target: Callable[..., Path],
    ) -> None:
        self._write_set = write_set
        self._operation_id = operation_id
        self._load_aggregate = load_aggregate
        self._revision_factory = revision_factory
        self._clock = clock
        self._safe_target = safe_target
        self._loaded: dict[str, CorrectionAggregateSnapshot | None] = {}
        self._staged_before: CorrectionAggregateSnapshot | None = None
        self._staged_after: CorrectionAggregateSnapshot | None = None
        self._committed = False
        self._closed = False

    def receipt(self, operation_id: str) -> CorrectionMutationReceipt | None:
        self._ensure_open()
        if operation_id != self._operation_id:
            raise _repository_error(
                "the receipt request is outside this operation",
                code="receipt_scope_mismatch",
            )
        path = self._safe_target(
            self._receipt_relative(operation_id),
            artifact="correction_receipt",
        )
        raw = _read_json(
            path,
            artifact="correction_receipt",
            maximum_bytes=_MAX_RECEIPT_BYTES,
        )
        if raw is None:
            return None
        try:
            envelope = _object(
                raw,
                artifact="correction_receipt",
                fields=_RECEIPT_ENVELOPE_FIELDS,
            )
            if (
                envelope["schema"] != CORRECTION_RECEIPT_SCHEMA
                or envelope["version"] != CORRECTION_RECEIPT_VERSION
                or envelope["operation_id"] != operation_id
            ):
                raise ValueError("correction receipt envelope is invalid")
            receipt = _decode_receipt(envelope["receipt"])
            if receipt.operation_id != operation_id:
                raise ValueError("correction receipt has another operation")
            return receipt
        except (EngineError, TypeError, ValueError, KeyError) as exc:
            raise _repository_error(
                "a correction receipt is invalid",
                code="invalid_correction_receipt",
                error=exc,
            ) from exc

    def get(self, item_id: str) -> CorrectionAggregateSnapshot | None:
        self._ensure_open()
        FilesystemCorrectionRepository._identifier(
            item_id,
            field_name="item_id",
        )
        if item_id in self._loaded:
            return self._loaded[item_id]

        path = self._safe_target(
            self._aggregate_relative(item_id),
            artifact="correction_aggregate",
        )
        raw = _read_json(
            path,
            artifact="correction_aggregate",
            maximum_bytes=_MAX_AGGREGATE_BYTES,
        )
        if raw is not None:
            aggregate = self._aggregate_from_document(raw, item_id=item_id)
        else:
            aggregate = self._load_initial(item_id)
        self._loaded[item_id] = aggregate
        return aggregate

    def stage(
        self,
        current: CorrectionAggregateSnapshot,
        command: CorrectionCommand,
    ) -> CorrectionAggregateSnapshot:
        self._ensure_stageable()
        if not isinstance(current, CorrectionAggregateSnapshot):
            raise _repository_error(
                "the correction stage input is invalid",
                code="invalid_correction_repository_command",
            )
        command_item_id = getattr(command, "item_id", None)
        command_operation_id = getattr(command, "operation_id", None)
        if (
            command_item_id != current.item_id
            or command_operation_id != self._operation_id
        ):
            raise _repository_error(
                "the correction command is outside this unit",
                code="correction_repository_scope_mismatch",
            )
        stored = self.get(current.item_id)
        if stored != current:
            raise _repository_error(
                "the correction mutation is outside the locked snapshot",
                code="correction_repository_scope_mismatch",
                details={"item_id": current.item_id},
            )
        try:
            staged = self._apply(current, command)
        except RepositoryError:
            raise
        except (EngineError, KeyError, TypeError, ValueError) as exc:
            raise _repository_error(
                "the correction repository could not stage the command",
                code="invalid_correction_repository_command",
                error=exc,
            ) from exc
        self._staged_before = current
        self._staged_after = staged
        return staged

    def commit(self, receipt: CorrectionMutationReceipt) -> None:
        self._ensure_open()
        if self._committed:
            raise _repository_error(
                "the correction unit is already committed",
                code="correction_unit_committed",
            )
        before = self._staged_before
        after = self._staged_after
        if before is None or after is None:
            raise _repository_error(
                "no correction mutation has been staged",
                code="correction_mutation_not_staged",
            )
        self._validate_receipt(receipt, before=before, after=after)
        if self.receipt(self._operation_id) is not None:
            raise _repository_error(
                "a correction receipt already exists",
                code="correction_receipt_exists",
            )
        receipt_document = {
            "schema": CORRECTION_RECEIPT_SCHEMA,
            "version": CORRECTION_RECEIPT_VERSION,
            "operation_id": self._operation_id,
            "receipt": receipt.as_dict(),
        }
        aggregate_document = {
            "schema": CORRECTION_AGGREGATE_SCHEMA,
            "version": CORRECTION_AGGREGATE_VERSION,
            "item_id": after.item_id,
            "aggregate": after.as_dict(),
        }
        try:
            transaction = self._write_set.begin(
                operation_id=self._operation_id,
                scope="correction-command",
                metadata={
                    "action": receipt.action,
                    "item_id": after.item_id,
                },
            )
            self.stage_publication(
                transaction,
                receipt_document=receipt_document,
                aggregate_document=aggregate_document,
            )
            transaction.commit(receipt=receipt.as_dict())
        except WriteSetError as exc:
            raise _repository_error(
                "the correction repository transaction failed",
                code=exc.code,
                error=exc,
                retryable=True,
            ) from exc
        self._committed = True
        self._loaded[after.item_id] = after

    def stage_publication(
        self,
        transaction: RecoverableWriteTransaction,
        *,
        receipt_document: Mapping[str, Any],
        aggregate_document: Mapping[str, Any],
    ) -> None:
        """Stage a correction mutation into one caller-owned write set.

        The receipt is added before the aggregate, making the aggregate the
        final public state document and the first target removed on rollback.
        """

        before = self._staged_before
        after = self._staged_after
        if before is None or after is None:
            raise _repository_error(
                "no correction mutation has been staged",
                code="correction_mutation_not_staged",
            )
        if not isinstance(transaction, RecoverableWriteTransaction) or (
            transaction._owner is not self._write_set
        ):
            raise _repository_error(
                "the correction transaction belongs to another workspace",
                code="correction_repository_scope_mismatch",
            )
        transaction.stage_write(
            self._receipt_relative(self._operation_id),
            _json_bytes(receipt_document, artifact="correction_receipt"),
        )
        transaction.stage_write(
            self._aggregate_relative(after.item_id),
            _json_bytes(aggregate_document, artifact="correction_aggregate"),
        )

    def close(self) -> None:
        self._closed = True

    def _load_initial(
        self,
        item_id: str,
    ) -> CorrectionAggregateSnapshot | None:
        if self._load_aggregate is None:
            return None
        try:
            aggregate = self._load_aggregate(item_id)
        except RepositoryError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the correction aggregate loader failed",
                code="correction_aggregate_loader_failed",
                error=exc,
                retryable=True,
            ) from exc
        if aggregate is None:
            return None
        if not isinstance(aggregate, CorrectionAggregateSnapshot):
            raise _repository_error(
                "the correction aggregate loader returned invalid state",
                code="invalid_correction_snapshot",
            )
        if aggregate.item_id != item_id:
            raise _repository_error(
                "the correction aggregate loader returned another item",
                code="correction_repository_scope_mismatch",
            )
        return aggregate

    @staticmethod
    def _aggregate_from_document(
        value: Any,
        *,
        item_id: str,
    ) -> CorrectionAggregateSnapshot:
        try:
            envelope = _object(
                value,
                artifact="correction_aggregate",
                fields=_AGGREGATE_ENVELOPE_FIELDS,
            )
            if (
                envelope["schema"] != CORRECTION_AGGREGATE_SCHEMA
                or envelope["version"] != CORRECTION_AGGREGATE_VERSION
                or envelope["item_id"] != item_id
            ):
                raise ValueError("correction aggregate envelope is invalid")
            aggregate = _decode_aggregate(envelope["aggregate"])
            if aggregate.item_id != item_id:
                raise ValueError("correction aggregate has another item")
            return aggregate
        except (EngineError, TypeError, ValueError, KeyError) as exc:
            raise _repository_error(
                "a correction aggregate document is invalid",
                code="invalid_correction_snapshot",
                error=exc,
                details={"item_id": item_id},
            ) from exc

    def _apply(
        self,
        current: CorrectionAggregateSnapshot,
        command: CorrectionCommand,
    ) -> CorrectionAggregateSnapshot:
        artifacts = {
            value.key.artifact_id: value for value in current.artifacts
        }
        annotations = {
            value.key.annotation_id: value for value in current.annotations
        }
        review = current.review

        if isinstance(command, AssignImageCategoryCommand):
            before = artifacts[command.artifact_id]
            assignment = CategoryAssignment(
                command.category,
                AssignmentOrigin.MANUAL,
                self._revision("category", command.artifact_id),
                provenance=command.provenance,
            )
            artifacts[command.artifact_id] = replace(
                before,
                revision=self._revision("artifact", command.artifact_id),
                category_assignments=_replace_origin(
                    before.category_assignments,
                    AssignmentOrigin.MANUAL,
                    assignment,
                ),
            )
        elif isinstance(command, ClearImageCategoryCommand):
            before = artifacts[command.artifact_id]
            artifacts[command.artifact_id] = replace(
                before,
                revision=self._revision("artifact", command.artifact_id),
                category_assignments=_replace_origin(
                    before.category_assignments,
                    AssignmentOrigin.MANUAL,
                    None,
                ),
            )
        elif isinstance(command, SetManualCaptionCommand):
            before = artifacts[command.artifact_id]
            assertion = CaptionAssertion(
                command.text,
                CaptionOrigin.MANUAL,
                self._revision("caption", command.artifact_id),
                language=command.language,
                provenance=command.provenance,
            )
            artifacts[command.artifact_id] = replace(
                before,
                revision=self._revision("artifact", command.artifact_id),
                caption_assertions=_replace_origin(
                    before.caption_assertions,
                    CaptionOrigin.MANUAL,
                    assertion,
                ),
            )
        elif isinstance(command, ClearManualCaptionCommand):
            before = artifacts[command.artifact_id]
            artifacts[command.artifact_id] = replace(
                before,
                revision=self._revision("artifact", command.artifact_id),
                caption_assertions=_replace_origin(
                    before.caption_assertions,
                    CaptionOrigin.MANUAL,
                    None,
                ),
            )
        elif isinstance(command, AssertArtifactMetadataCommand):
            before = artifacts[command.artifact_id]
            changed_names = set(command.assertions) | set(command.clear_names)
            assertions = tuple(
                value
                for value in before.metadata_assertions
                if not (
                    value.origin is MetadataAssertionOrigin.MANUAL
                    and value.name in changed_names
                )
            )
            assertions += tuple(
                ArtifactMetadataAssertion(
                    name,
                    value,
                    MetadataAssertionOrigin.MANUAL,
                    self._revision("metadata", command.artifact_id),
                    command.provenance,
                )
                for name, value in command.assertions.items()
            )
            artifacts[command.artifact_id] = replace(
                before,
                revision=self._revision("artifact", command.artifact_id),
                metadata_assertions=assertions,
            )
        elif isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            before = annotations[command.annotation_id]
            assignment = None
            if isinstance(command, AssignRegionRoleCommand):
                assignment = SpatialRoleAssignment(
                    command.role,
                    RoleAssignmentOrigin.MANUAL,
                    self._revision("role", command.annotation_id),
                    provenance=command.provenance,
                )
            annotations[command.annotation_id] = replace(
                before,
                revision=self._revision("annotation", command.annotation_id),
                linked_artifact_id=(
                    before.linked_artifact_id or command.linked_artifact_id
                ),
                role_assignments=_replace_origin(
                    before.role_assignments,
                    RoleAssignmentOrigin.MANUAL,
                    assignment,
                ),
            )
            if command.linked_artifact_id:
                linked = artifacts[command.linked_artifact_id]
                linked_assignment = None
                if isinstance(command, AssignRegionRoleCommand):
                    linked_assignment = SpatialRoleAssignment(
                        command.role,
                        RoleAssignmentOrigin.MANUAL,
                        self._revision(
                            "artifact-role",
                            command.linked_artifact_id,
                        ),
                        provenance=command.provenance,
                    )
                artifacts[command.linked_artifact_id] = replace(
                    linked,
                    revision=self._revision(
                        "artifact",
                        command.linked_artifact_id,
                    ),
                    role_assignments=_replace_origin(
                        linked.role_assignments,
                        RoleAssignmentOrigin.MANUAL,
                        linked_assignment,
                    ),
                )
        elif isinstance(
            command,
            (
                MarkAttentionCommand,
                ResolveCorrectionsCommand,
                ReopenCorrectionsCommand,
            ),
        ):
            if isinstance(command, MarkAttentionCommand):
                action = "attention.mark"
                after_state = "needs_attention"
                reason = command.reason
                event_reason = command.reason
            elif isinstance(command, ResolveCorrectionsCommand):
                action = "attention.resolve"
                after_state = "resolved"
                reason = review.reason
                event_reason = ""
            else:
                action = "attention.reopen"
                after_state = "needs_attention"
                reason = review.reason
                event_reason = ""
            event = CorrectionAuditEvent(
                operation_id=command.operation_id,
                action=action,
                actor_id=command.actor_id,
                occurred_at=self._occurred_at(),
                before_state=review.state,
                after_state=after_state,
                reason=event_reason,
                comment=command.comment,
            )
            review = CorrectionReviewSnapshot(
                revision=self._revision("review", current.item_id),
                state=after_state,
                reason=reason,
                history=(*review.history, event),
            )
        else:
            raise _repository_error(
                "the correction command type is unsupported",
                code="invalid_correction_repository_command",
            )

        return CorrectionAggregateSnapshot(
            item_id=current.item_id,
            revision=self._revision("aggregate", current.item_id),
            artifacts=tuple(artifacts.values()),
            annotations=tuple(annotations.values()),
            review=review,
        )

    def _revision(self, kind: str, target_id: str) -> str:
        try:
            value = self._revision_factory(kind, target_id)
        except Exception as exc:
            raise _repository_error(
                "the correction repository could not allocate a revision",
                code="correction_revision_allocation_failed",
                error=exc,
                retryable=True,
            ) from exc
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or value != value.strip()
            or '"' in value
            or "\\" in value
            or any(character.isspace() for character in value)
        ):
            raise _repository_error(
                "the correction repository allocated an invalid revision",
                code="invalid_correction_revision",
                details={"kind": kind, "target_id": target_id},
            )
        return value

    def _occurred_at(self) -> str:
        try:
            value = self._clock()
        except Exception as exc:
            raise _repository_error(
                "the correction repository clock failed",
                code="correction_clock_failed",
                error=exc,
                retryable=True,
            ) from exc
        if not isinstance(value, str):
            raise _repository_error(
                "the correction repository clock returned invalid text",
                code="correction_clock_failed",
            )
        return value

    def _validate_receipt(
        self,
        receipt: CorrectionMutationReceipt,
        *,
        before: CorrectionAggregateSnapshot,
        after: CorrectionAggregateSnapshot,
    ) -> None:
        if not isinstance(receipt, CorrectionMutationReceipt):
            raise _repository_error(
                "the correction receipt is invalid",
                code="invalid_correction_receipt",
            )
        if (
            receipt.operation_id != self._operation_id
            or receipt.item_id != before.item_id
            or receipt.before_aggregate_revision != before.revision
            or receipt.after_aggregate_revision != after.revision
            or receipt.targets != self._changed_targets(before, after)
        ):
            raise _repository_error(
                "the correction receipt is outside the staged mutation",
                code="receipt_scope_mismatch",
            )

    @staticmethod
    def _changed_targets(
        before: CorrectionAggregateSnapshot,
        after: CorrectionAggregateSnapshot,
    ) -> tuple[CorrectionTargetRevision, ...]:
        before_artifacts = {
            value.key.artifact_id: value for value in before.artifacts
        }
        after_artifacts = {
            value.key.artifact_id: value for value in after.artifacts
        }
        before_annotations = {
            value.key.annotation_id: value for value in before.annotations
        }
        after_annotations = {
            value.key.annotation_id: value for value in after.annotations
        }
        if (
            set(before_artifacts) != set(after_artifacts)
            or set(before_annotations) != set(after_annotations)
            or before.item_id != after.item_id
        ):
            raise _repository_error(
                "the staged correction changed target identities",
                code="correction_repository_scope_mismatch",
            )
        targets = [
            CorrectionTargetRevision(
                "artifact",
                target_id,
                value.revision,
                after_artifacts[target_id].revision,
            )
            for target_id, value in before_artifacts.items()
            if value != after_artifacts[target_id]
        ]
        targets += [
            CorrectionTargetRevision(
                "annotation",
                target_id,
                value.revision,
                after_annotations[target_id].revision,
            )
            for target_id, value in before_annotations.items()
            if value != after_annotations[target_id]
        ]
        if before.review != after.review:
            targets.append(
                CorrectionTargetRevision(
                    "review",
                    before.item_id,
                    before.review.revision,
                    after.review.revision,
                )
            )
        return tuple(
            sorted(
                targets,
                key=lambda value: (value.kind.value, value.target_id),
            )
        )

    @staticmethod
    def _aggregate_relative(item_id: str) -> str:
        digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        return (_AGGREGATE_ROOT / f"{digest}.json").as_posix()

    @staticmethod
    def _receipt_relative(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return (_RECEIPT_ROOT / f"{digest}.json").as_posix()

    def _ensure_open(self) -> None:
        if self._closed:
            raise _repository_error(
                "the correction unit is closed",
                code="correction_unit_closed",
            )

    def _ensure_stageable(self) -> None:
        self._ensure_open()
        if self._committed:
            raise _repository_error(
                "the correction unit is already committed",
                code="correction_unit_committed",
            )
        if self._staged_after is not None:
            raise _repository_error(
                "a correction mutation is already staged",
                code="correction_mutation_already_staged",
            )


__all__ = [
    "CORRECTION_AGGREGATE_SCHEMA",
    "CORRECTION_AGGREGATE_VERSION",
    "CORRECTION_RECEIPT_SCHEMA",
    "CORRECTION_RECEIPT_VERSION",
    "FilesystemCorrectionRepository",
    "FilesystemCorrectionUnitOfWork",
]
