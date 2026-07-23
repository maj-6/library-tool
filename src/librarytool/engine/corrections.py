"""Framework-neutral correction aggregate and mutation service.

The aggregate is an assertion overlay over raster artifacts and spatial
annotations.  Machine and human assertions remain separate.  Image-category
inheritance follows source-artifact edges at query time, so changing a source
never requires browser-side fan-out writes.

Repositories own persistence and revision generation.  A unit of work must
stage a complete aggregate and atomically publish it with the replay receipt;
this is what makes a linked figure-region/extracted-artifact change one
transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ContextManager, Literal, Protocol, TypeAlias

from .errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from .raster_artifacts import (
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    IMAGE_CATEGORIES,
    JsonMapping,
    RasterArtifactKey,
    _EMPTY_MAPPING,
    _extensions,
    _identifier,
    _revision,
    _safe_text,
    _thaw,
    _typed_values,
)
from .spatial_annotations import (
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialRoleAssignment,
    canonical_spatial_role,
)


CorrectionAction: TypeAlias = Literal[
    "category.assign",
    "category.clear",
    "role.assign",
    "role.clear",
    "caption.set",
    "caption.clear",
    "metadata.assert",
    "attention.mark",
    "attention.resolve",
    "attention.reopen",
]
ReviewAuditAction: TypeAlias = Literal[
    "attention.mark",
    "attention.resolve",
    "attention.reopen",
    "attention.clear",
]

_ACTIONS = frozenset(
    {
        "category.assign",
        "category.clear",
        "role.assign",
        "role.clear",
        "caption.set",
        "caption.clear",
        "metadata.assert",
        "attention.mark",
        "attention.resolve",
        "attention.reopen",
    }
)
_INVERSE_ACTIONS = _ACTIONS | {"attention.clear"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_METADATA_ASSERTIONS = 128
MAX_AUDIT_EVENTS = 100_000


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            _thaw(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValidationError(
            "correction command is not portable JSON",
            code="invalid_correction_command",
        ) from exc


def _command_hash(value: JsonMapping) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _command_strings(value: Any, *field_names: str) -> None:
    for field_name in field_names:
        if not isinstance(getattr(value, field_name), str):
            raise TypeError(f"{field_name} must be a string")


def _sequence(value: Any, item_type: type, field_name: str, maximum: int) -> tuple:
    return _typed_values(value, item_type, field_name, maximum=maximum)


class MetadataAssertionOrigin(str, Enum):
    MANUAL = "manual"
    MACHINE = "machine"
    IMPORTED = "imported"


class ReviewState(str, Enum):
    CLEAR = "clear"
    NEEDS_ATTENTION = "needs_attention"
    RESOLVED = "resolved"


class EffectiveCategoryOrigin(str, Enum):
    MANUAL = "manual"
    INHERITED = "inherited"
    SUGGESTED = "suggested"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class EffectiveImageCategory:
    category: str
    origin: EffectiveCategoryOrigin | str
    assignment_revision: str = ""
    inherited_from_artifact_id: str = ""

    def __post_init__(self) -> None:
        if self.category not in IMAGE_CATEGORIES:
            raise ValidationError(
                "effective category is not in the canonical image vocabulary",
                code="invalid_correction_snapshot",
            )
        try:
            origin = EffectiveCategoryOrigin(self.origin)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "effective category origin is invalid",
                code="invalid_correction_snapshot",
            ) from exc
        object.__setattr__(self, "origin", origin)
        if self.assignment_revision:
            object.__setattr__(
                self,
                "assignment_revision",
                _revision(self.assignment_revision, "assignment_revision"),
            )
        if origin is EffectiveCategoryOrigin.INHERITED:
            object.__setattr__(
                self,
                "inherited_from_artifact_id",
                _identifier(
                    self.inherited_from_artifact_id,
                    "inherited_from_artifact_id",
                ),
            )
        elif self.inherited_from_artifact_id:
            raise ValidationError(
                "only inherited effective categories name a source artifact",
                code="invalid_correction_snapshot",
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "origin": self.origin.value,
            "assignment_revision": self.assignment_revision,
            "inherited_from_artifact_id": self.inherited_from_artifact_id,
        }


@dataclass(frozen=True, slots=True)
class ArtifactMetadataAssertion:
    name: str
    value: Any
    origin: MetadataAssertionOrigin | str
    revision: str
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "metadata.name"))
        try:
            origin = MetadataAssertionOrigin(self.origin)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "metadata assertion origin is invalid",
                code="invalid_metadata_assertion",
            ) from exc
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        frozen = _extensions({"value": self.value}, "metadata.assertion")
        object.__setattr__(self, "value", frozen["value"])
        if not isinstance(self.provenance, ArtifactProvenance):
            raise TypeError("provenance must be ArtifactProvenance")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": _thaw(self.value),
            "origin": self.origin.value,
            "revision": self.revision,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ArtifactCorrectionSnapshot:
    key: RasterArtifactKey
    revision: str
    source_artifact_id: str = ""
    category_assignments: tuple[CategoryAssignment, ...] = ()
    caption_assertions: tuple[CaptionAssertion, ...] = ()
    role_assignments: tuple[SpatialRoleAssignment, ...] = ()
    metadata_assertions: tuple[ArtifactMetadataAssertion, ...] = ()
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if not isinstance(self.key, RasterArtifactKey):
            raise TypeError("key must be RasterArtifactKey")
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        if self.source_artifact_id:
            source_id = _identifier(self.source_artifact_id, "source_artifact_id")
            if source_id == self.key.artifact_id:
                raise ValidationError(
                    "an artifact cannot inherit from itself",
                    code="invalid_category_inheritance",
                )
            object.__setattr__(self, "source_artifact_id", source_id)

        categories = _sequence(
            self.category_assignments,
            CategoryAssignment,
            "category_assignments",
            len(AssignmentOrigin),
        )
        if any(value.origin is AssignmentOrigin.INHERITED for value in categories):
            raise ValidationError(
                "inherited category assertions are computed, not persisted",
                code="invalid_category_inheritance",
            )
        origins = [value.origin for value in categories]
        if len(origins) != len(set(origins)):
            raise ValidationError(
                "category assertion origins must be unique",
                code="invalid_correction_snapshot",
            )
        object.__setattr__(self, "category_assignments", categories)

        captions = _sequence(
            self.caption_assertions,
            CaptionAssertion,
            "caption_assertions",
            len(CaptionOrigin),
        )
        origins = [value.origin for value in captions]
        if len(origins) != len(set(origins)):
            raise ValidationError(
                "caption assertion origins must be unique",
                code="invalid_correction_snapshot",
            )
        object.__setattr__(self, "caption_assertions", captions)

        roles = _sequence(
            self.role_assignments,
            SpatialRoleAssignment,
            "role_assignments",
            len(RoleAssignmentOrigin),
        )
        origins = [value.origin for value in roles]
        if len(origins) != len(set(origins)):
            raise ValidationError(
                "role assertion origins must be unique",
                code="invalid_correction_snapshot",
            )
        object.__setattr__(self, "role_assignments", roles)

        metadata = _sequence(
            self.metadata_assertions,
            ArtifactMetadataAssertion,
            "metadata_assertions",
            MAX_METADATA_ASSERTIONS,
        )
        identities = [(value.name, value.origin) for value in metadata]
        if len(identities) != len(set(identities)):
            raise ValidationError(
                "metadata assertion names must be unique per origin",
                code="invalid_metadata_assertion",
            )
        object.__setattr__(self, "metadata_assertions", metadata)
        object.__setattr__(self, "extensions", _extensions(self.extensions))

    def category(self, origin: AssignmentOrigin) -> CategoryAssignment | None:
        return next(
            (value for value in self.category_assignments if value.origin is origin),
            None,
        )

    def caption(self, origin: CaptionOrigin) -> CaptionAssertion | None:
        return next(
            (value for value in self.caption_assertions if value.origin is origin),
            None,
        )

    def role(self, origin: RoleAssignmentOrigin) -> SpatialRoleAssignment | None:
        return next(
            (value for value in self.role_assignments if value.origin is origin),
            None,
        )

    def metadata(
        self,
        name: str,
        origin: MetadataAssertionOrigin,
    ) -> ArtifactMetadataAssertion | None:
        return next(
            (
                value
                for value in self.metadata_assertions
                if value.name == name and value.origin is origin
            ),
            None,
        )

    def effective_metadata(self) -> dict[str, Any]:
        names = {value.name for value in self.metadata_assertions}
        result: dict[str, Any] = {}
        for name in sorted(names):
            for origin in (
                MetadataAssertionOrigin.MANUAL,
                MetadataAssertionOrigin.IMPORTED,
                MetadataAssertionOrigin.MACHINE,
            ):
                assertion = self.metadata(name, origin)
                if assertion is not None:
                    result[name] = _thaw(assertion.value)
                    break
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "revision": self.revision,
            "source_artifact_id": self.source_artifact_id,
            "category_assignments": [
                value.as_dict() for value in self.category_assignments
            ],
            "caption_assertions": [
                value.as_dict() for value in self.caption_assertions
            ],
            "role_assignments": [value.as_dict() for value in self.role_assignments],
            "metadata_assertions": [
                value.as_dict() for value in self.metadata_assertions
            ],
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class AnnotationCorrectionSnapshot:
    key: SpatialAnnotationKey
    revision: str
    linked_artifact_id: str = ""
    role_assignments: tuple[SpatialRoleAssignment, ...] = ()
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if not isinstance(self.key, SpatialAnnotationKey):
            raise TypeError("key must be SpatialAnnotationKey")
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        if self.linked_artifact_id:
            object.__setattr__(
                self,
                "linked_artifact_id",
                _identifier(self.linked_artifact_id, "linked_artifact_id"),
            )
        roles = _sequence(
            self.role_assignments,
            SpatialRoleAssignment,
            "role_assignments",
            len(RoleAssignmentOrigin),
        )
        origins = [value.origin for value in roles]
        if len(origins) != len(set(origins)):
            raise ValidationError(
                "role assertion origins must be unique",
                code="invalid_correction_snapshot",
            )
        object.__setattr__(self, "role_assignments", roles)
        object.__setattr__(self, "extensions", _extensions(self.extensions))

    def role(self, origin: RoleAssignmentOrigin) -> SpatialRoleAssignment | None:
        return next(
            (value for value in self.role_assignments if value.origin is origin),
            None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "revision": self.revision,
            "linked_artifact_id": self.linked_artifact_id,
            "role_assignments": [value.as_dict() for value in self.role_assignments],
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class CorrectionAuditEvent:
    operation_id: str
    action: ReviewAuditAction
    actor_id: str
    occurred_at: str
    before_state: ReviewState | str
    after_state: ReviewState | str
    reason: str = ""
    comment: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )
        if self.action not in {
            "attention.mark",
            "attention.resolve",
            "attention.reopen",
            "attention.clear",
        }:
            raise ValidationError(
                "audit action is invalid",
                code="invalid_correction_audit",
            )
        object.__setattr__(self, "actor_id", _identifier(self.actor_id, "actor_id"))
        object.__setattr__(
            self,
            "occurred_at",
            _safe_text(
                self.occurred_at,
                "occurred_at",
                maximum=128,
                allow_empty=False,
            ),
        )
        try:
            before = ReviewState(self.before_state)
            after = ReviewState(self.after_state)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "audit state is invalid",
                code="invalid_correction_audit",
            ) from exc
        expected = {
            "attention.mark": (ReviewState.CLEAR, ReviewState.NEEDS_ATTENTION),
            "attention.resolve": (
                ReviewState.NEEDS_ATTENTION,
                ReviewState.RESOLVED,
            ),
            "attention.reopen": (
                ReviewState.RESOLVED,
                ReviewState.NEEDS_ATTENTION,
            ),
            "attention.clear": (
                ReviewState.NEEDS_ATTENTION,
                ReviewState.CLEAR,
            ),
        }[self.action]
        if (before, after) != expected:
            raise ValidationError(
                "audit states do not match the action",
                code="invalid_correction_audit",
            )
        object.__setattr__(self, "before_state", before)
        object.__setattr__(self, "after_state", after)
        object.__setattr__(
            self,
            "reason",
            _safe_text(self.reason, "reason", maximum=2048),
        )
        object.__setattr__(
            self,
            "comment",
            _safe_text(self.comment, "comment", maximum=8192),
        )
        if self.action == "attention.mark" and not self.reason.strip():
            raise ValidationError(
                "attention reason is required",
                code="invalid_correction_audit",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "actor_id": self.actor_id,
            "occurred_at": self.occurred_at,
            "before_state": self.before_state.value,
            "after_state": self.after_state.value,
            "reason": self.reason,
            "comment": self.comment,
        }


@dataclass(frozen=True, slots=True)
class CorrectionReviewSnapshot:
    revision: str
    state: ReviewState | str = ReviewState.CLEAR
    reason: str = ""
    history: tuple[CorrectionAuditEvent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        try:
            state = ReviewState(self.state)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "review state is invalid",
                code="invalid_correction_review",
            ) from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "reason",
            _safe_text(self.reason, "reason", maximum=2048),
        )
        if state is ReviewState.CLEAR and self.reason:
            raise ValidationError(
                "a clear review cannot have an attention reason",
                code="invalid_correction_review",
            )
        if state is not ReviewState.CLEAR and not self.reason.strip():
            raise ValidationError(
                "attention and resolved reviews retain their reason",
                code="invalid_correction_review",
            )
        history = _sequence(
            self.history,
            CorrectionAuditEvent,
            "history",
            MAX_AUDIT_EVENTS,
        )
        operation_ids = [event.operation_id for event in history]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValidationError(
                "audit operation IDs must be unique",
                code="invalid_correction_audit",
            )
        if any(
            history[index - 1].after_state is not history[index].before_state
            for index in range(1, len(history))
        ):
            raise ValidationError(
                "review audit events must form a continuous state history",
                code="invalid_correction_audit",
            )
        if history and history[-1].after_state is not state:
            raise ValidationError(
                "review state must match its last audit event",
                code="invalid_correction_audit",
            )
        object.__setattr__(self, "history", history)

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "state": self.state.value,
            "reason": self.reason,
            "history": [event.as_dict() for event in self.history],
        }


@dataclass(frozen=True, slots=True)
class CorrectionAggregateSnapshot:
    item_id: str
    revision: str
    artifacts: tuple[ArtifactCorrectionSnapshot, ...]
    annotations: tuple[AnnotationCorrectionSnapshot, ...]
    review: CorrectionReviewSnapshot

    def __post_init__(self) -> None:
        item_id = _identifier(self.item_id, "item_id")
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        artifacts = _sequence(
            self.artifacts,
            ArtifactCorrectionSnapshot,
            "artifacts",
            100_000,
        )
        annotations = _sequence(
            self.annotations,
            AnnotationCorrectionSnapshot,
            "annotations",
            1_000_000,
        )
        if any(value.key.item_id != item_id for value in artifacts) or any(
            value.key.item_id != item_id for value in annotations
        ):
            raise ValidationError(
                "correction targets must belong to their aggregate",
                code="correction_scope_mismatch",
            )
        artifact_ids = [value.key.artifact_id.casefold() for value in artifacts]
        annotation_ids = [value.key.annotation_id.casefold() for value in annotations]
        if len(artifact_ids) != len(set(artifact_ids)) or len(annotation_ids) != len(
            set(annotation_ids)
        ):
            raise ValidationError(
                "correction target identities must be unique ignoring case",
                code="duplicate_correction_target",
            )
        object.__setattr__(
            self,
            "artifacts",
            tuple(sorted(artifacts, key=lambda value: value.key.artifact_id)),
        )
        object.__setattr__(
            self,
            "annotations",
            tuple(sorted(annotations, key=lambda value: value.key.annotation_id)),
        )
        if not isinstance(self.review, CorrectionReviewSnapshot):
            raise TypeError("review must be CorrectionReviewSnapshot")

        exact_artifacts = {value.key.artifact_id: value for value in artifacts}
        for artifact in artifacts:
            if artifact.source_artifact_id and artifact.source_artifact_id not in exact_artifacts:
                raise ValidationError(
                    "a category source artifact does not exist",
                    code="invalid_category_inheritance",
                )
        for annotation in annotations:
            if (
                annotation.linked_artifact_id
                and annotation.linked_artifact_id not in exact_artifacts
            ):
                raise ValidationError(
                    "a linked artifact does not exist",
                    code="invalid_correction_link",
                )
        for artifact in artifacts:
            seen: set[str] = set()
            current = artifact
            while current.source_artifact_id:
                if current.key.artifact_id in seen:
                    raise ValidationError(
                        "category inheritance contains a cycle",
                        code="invalid_category_inheritance",
                    )
                seen.add(current.key.artifact_id)
                current = exact_artifacts[current.source_artifact_id]

    def artifact(self, artifact_id: str) -> ArtifactCorrectionSnapshot | None:
        return next(
            (value for value in self.artifacts if value.key.artifact_id == artifact_id),
            None,
        )

    def annotation(
        self,
        annotation_id: str,
    ) -> AnnotationCorrectionSnapshot | None:
        return next(
            (
                value
                for value in self.annotations
                if value.key.annotation_id == annotation_id
            ),
            None,
        )

    def effective_category(self, artifact_id: str) -> EffectiveImageCategory:
        artifact = self.artifact(artifact_id)
        if artifact is None:
            raise NotFoundError(
                "the raster artifact does not exist",
                code="artifact_not_found",
                details={"item_id": self.item_id, "artifact_id": artifact_id},
            )
        manual = artifact.category(AssignmentOrigin.MANUAL)
        if manual is not None:
            return EffectiveImageCategory(
                manual.category,
                EffectiveCategoryOrigin.MANUAL,
                manual.revision,
            )
        if artifact.source_artifact_id:
            source = self.effective_category(artifact.source_artifact_id)
            if source.origin is not EffectiveCategoryOrigin.DEFAULT:
                return EffectiveImageCategory(
                    source.category,
                    EffectiveCategoryOrigin.INHERITED,
                    source.assignment_revision,
                    artifact.source_artifact_id,
                )
        suggested = artifact.category(AssignmentOrigin.SUGGESTED)
        if suggested is not None:
            return EffectiveImageCategory(
                suggested.category,
                EffectiveCategoryOrigin.SUGGESTED,
                suggested.revision,
            )
        return EffectiveImageCategory("other", EffectiveCategoryOrigin.DEFAULT)

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "revision": self.revision,
            "artifacts": [value.as_dict() for value in self.artifacts],
            "annotations": [value.as_dict() for value in self.annotations],
            "review": self.review.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class AssignImageCategoryCommand:
    item_id: str
    artifact_id: str
    expected_artifact_revision: str
    category: str
    operation_id: str
    provenance: ArtifactProvenance = field(
        default_factory=lambda: ArtifactProvenance(origin="human")
    )

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "artifact_id",
            "expected_artifact_revision",
            "category",
            "operation_id",
        )
        if not isinstance(self.provenance, ArtifactProvenance):
            raise TypeError("provenance must be ArtifactProvenance")


@dataclass(frozen=True, slots=True)
class ClearImageCategoryCommand:
    item_id: str
    artifact_id: str
    expected_artifact_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "artifact_id",
            "expected_artifact_revision",
            "operation_id",
        )


@dataclass(frozen=True, slots=True)
class AssignRegionRoleCommand:
    item_id: str
    annotation_id: str
    expected_annotation_revision: str
    role: str
    operation_id: str
    linked_artifact_id: str = ""
    expected_linked_artifact_revision: str = ""
    provenance: ArtifactProvenance = field(
        default_factory=lambda: ArtifactProvenance(origin="human")
    )

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "annotation_id",
            "expected_annotation_revision",
            "role",
            "operation_id",
            "linked_artifact_id",
            "expected_linked_artifact_revision",
        )
        if bool(self.linked_artifact_id) != bool(
            self.expected_linked_artifact_revision
        ):
            raise ValueError(
                "linked artifact identity and revision must be supplied together"
            )
        if not isinstance(self.provenance, ArtifactProvenance):
            raise TypeError("provenance must be ArtifactProvenance")


@dataclass(frozen=True, slots=True)
class ClearRegionRoleCommand:
    item_id: str
    annotation_id: str
    expected_annotation_revision: str
    operation_id: str
    linked_artifact_id: str = ""
    expected_linked_artifact_revision: str = ""

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "annotation_id",
            "expected_annotation_revision",
            "operation_id",
            "linked_artifact_id",
            "expected_linked_artifact_revision",
        )
        if bool(self.linked_artifact_id) != bool(
            self.expected_linked_artifact_revision
        ):
            raise ValueError(
                "linked artifact identity and revision must be supplied together"
            )


@dataclass(frozen=True, slots=True)
class SetManualCaptionCommand:
    item_id: str
    artifact_id: str
    expected_artifact_revision: str
    text: str
    operation_id: str
    language: str = ""
    provenance: ArtifactProvenance = field(
        default_factory=lambda: ArtifactProvenance(origin="human")
    )

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "artifact_id",
            "expected_artifact_revision",
            "text",
            "operation_id",
            "language",
        )
        if not isinstance(self.provenance, ArtifactProvenance):
            raise TypeError("provenance must be ArtifactProvenance")


@dataclass(frozen=True, slots=True)
class ClearManualCaptionCommand:
    item_id: str
    artifact_id: str
    expected_artifact_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "artifact_id",
            "expected_artifact_revision",
            "operation_id",
        )


@dataclass(frozen=True, slots=True)
class AssertArtifactMetadataCommand:
    item_id: str
    artifact_id: str
    expected_artifact_revision: str
    operation_id: str
    assertions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)
    clear_names: tuple[str, ...] = ()
    provenance: ArtifactProvenance = field(
        default_factory=lambda: ArtifactProvenance(origin="human")
    )

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "artifact_id",
            "expected_artifact_revision",
            "operation_id",
        )
        assertions = _extensions(self.assertions, "assertions")
        if len(assertions) > MAX_METADATA_ASSERTIONS:
            raise ValueError("too many metadata assertions")
        for name in assertions:
            _identifier(name, "metadata.name")
        if isinstance(self.clear_names, (str, bytes)) or not isinstance(
            self.clear_names,
            Sequence,
        ):
            raise TypeError("clear_names must be a sequence")
        clear_names = tuple(_identifier(value, "metadata.name") for value in self.clear_names)
        if len(clear_names) > MAX_METADATA_ASSERTIONS or len(clear_names) != len(
            set(clear_names)
        ):
            raise ValueError("clear_names must be bounded and unique")
        if set(assertions) & set(clear_names):
            raise ValueError("metadata names cannot be asserted and cleared together")
        if not assertions and not clear_names:
            raise ValueError("at least one metadata assertion or clear is required")
        if not isinstance(self.provenance, ArtifactProvenance):
            raise TypeError("provenance must be ArtifactProvenance")
        object.__setattr__(self, "assertions", assertions)
        object.__setattr__(self, "clear_names", clear_names)


@dataclass(frozen=True, slots=True)
class MarkAttentionCommand:
    item_id: str
    expected_review_revision: str
    reason: str
    actor_id: str
    operation_id: str
    comment: str = ""

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "expected_review_revision",
            "reason",
            "actor_id",
            "operation_id",
            "comment",
        )


@dataclass(frozen=True, slots=True)
class ResolveCorrectionsCommand:
    item_id: str
    expected_review_revision: str
    actor_id: str
    operation_id: str
    comment: str = ""

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "expected_review_revision",
            "actor_id",
            "operation_id",
            "comment",
        )


@dataclass(frozen=True, slots=True)
class ReopenCorrectionsCommand:
    item_id: str
    expected_review_revision: str
    actor_id: str
    operation_id: str
    comment: str = ""

    def __post_init__(self) -> None:
        _command_strings(
            self,
            "item_id",
            "expected_review_revision",
            "actor_id",
            "operation_id",
            "comment",
        )


CorrectionCommand: TypeAlias = (
    AssignImageCategoryCommand
    | ClearImageCategoryCommand
    | AssignRegionRoleCommand
    | ClearRegionRoleCommand
    | SetManualCaptionCommand
    | ClearManualCaptionCommand
    | AssertArtifactMetadataCommand
    | MarkAttentionCommand
    | ResolveCorrectionsCommand
    | ReopenCorrectionsCommand
)


class CorrectionTargetKind(str, Enum):
    ARTIFACT = "artifact"
    ANNOTATION = "annotation"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class CorrectionTargetRevision:
    kind: CorrectionTargetKind | str
    target_id: str
    before_revision: str
    after_revision: str

    def __post_init__(self) -> None:
        try:
            kind = CorrectionTargetKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "correction target kind is invalid",
                code="invalid_correction_receipt",
            ) from exc
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "target_id", _identifier(self.target_id, "target_id"))
        object.__setattr__(
            self,
            "before_revision",
            _revision(self.before_revision, "before_revision"),
        )
        object.__setattr__(
            self,
            "after_revision",
            _revision(self.after_revision, "after_revision"),
        )
        if self.before_revision == self.after_revision:
            raise ValidationError(
                "a correction target revision did not advance",
                code="invalid_correction_receipt",
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "target_id": self.target_id,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
        }


@dataclass(frozen=True, slots=True)
class CorrectionInverse:
    """Data needed to apply an inverse as a new conditional operation.

    Review inverses append another audit event; consumers must never truncate
    history by replacing the stored review snapshot.
    """

    action: str
    expected_aggregate_revision: str
    expected_targets: tuple[CorrectionTargetRevision, ...]
    payload: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if self.action not in _INVERSE_ACTIONS:
            raise ValidationError(
                "inverse action is invalid",
                code="invalid_correction_inverse",
            )
        object.__setattr__(
            self,
            "expected_aggregate_revision",
            _revision(
                self.expected_aggregate_revision,
                "expected_aggregate_revision",
            ),
        )
        targets = _sequence(
            self.expected_targets,
            CorrectionTargetRevision,
            "expected_targets",
            3,
        )
        identities = [(value.kind, value.target_id) for value in targets]
        if len(identities) != len(set(identities)):
            raise ValidationError(
                "inverse target identities must be unique",
                code="invalid_correction_inverse",
            )
        object.__setattr__(self, "expected_targets", targets)
        object.__setattr__(
            self,
            "payload",
            _extensions(self.payload, "inverse.payload"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "expected_aggregate_revision": self.expected_aggregate_revision,
            "expected_targets": [value.as_dict() for value in self.expected_targets],
            "payload": _thaw(self.payload),
        }


@dataclass(frozen=True, slots=True)
class CorrectionMutationReceipt:
    action: CorrectionAction
    operation_id: str
    command_sha256: str
    item_id: str
    before_aggregate_revision: str
    after_aggregate_revision: str
    targets: tuple[CorrectionTargetRevision, ...]
    inverse: CorrectionInverse

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValidationError(
                "receipt action is invalid",
                code="invalid_correction_receipt",
            )
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )
        if not isinstance(self.command_sha256, str) or not _SHA256_RE.fullmatch(
            self.command_sha256
        ):
            raise ValidationError(
                "receipt command fingerprint is invalid",
                code="invalid_correction_receipt",
            )
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "before_aggregate_revision",
            _revision(
                self.before_aggregate_revision,
                "before_aggregate_revision",
            ),
        )
        object.__setattr__(
            self,
            "after_aggregate_revision",
            _revision(self.after_aggregate_revision, "after_aggregate_revision"),
        )
        if self.before_aggregate_revision == self.after_aggregate_revision:
            raise ValidationError(
                "aggregate revision did not advance",
                code="invalid_correction_receipt",
            )
        targets = _sequence(
            self.targets,
            CorrectionTargetRevision,
            "targets",
            3,
        )
        if not targets:
            raise ValidationError(
                "a correction receipt requires at least one target",
                code="invalid_correction_receipt",
            )
        identities = [(value.kind, value.target_id) for value in targets]
        if len(identities) != len(set(identities)):
            raise ValidationError(
                "receipt target identities must be unique",
                code="invalid_correction_receipt",
            )
        object.__setattr__(self, "targets", targets)
        if not isinstance(self.inverse, CorrectionInverse):
            raise TypeError("inverse must be CorrectionInverse")
        if self.inverse.expected_aggregate_revision != self.after_aggregate_revision:
            raise ValidationError(
                "inverse aggregate pin does not match the receipt",
                code="invalid_correction_receipt",
            )
        if self.inverse.expected_targets != targets:
            raise ValidationError(
                "inverse target pins do not match the receipt",
                code="invalid_correction_receipt",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "operation_id": self.operation_id,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "before_aggregate_revision": self.before_aggregate_revision,
            "after_aggregate_revision": self.after_aggregate_revision,
            "targets": [value.as_dict() for value in self.targets],
            "inverse": self.inverse.as_dict(),
        }

    def as_public_dict(self) -> dict[str, Any]:
        value = self.as_dict()
        value.pop("command_sha256")
        return value


@dataclass(frozen=True, slots=True)
class CorrectionCommandResult:
    receipt: CorrectionMutationReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CorrectionMutationReceipt):
            raise TypeError("receipt must be CorrectionMutationReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_public_dict(),
        }


class CorrectionUnitOfWorkPort(Protocol):
    """One isolated correction aggregate and atomic publication boundary."""

    def receipt(self, operation_id: str) -> CorrectionMutationReceipt | None: ...

    def get(self, item_id: str) -> CorrectionAggregateSnapshot | None: ...

    def stage(
        self,
        current: CorrectionAggregateSnapshot,
        command: CorrectionCommand,
    ) -> CorrectionAggregateSnapshot: ...

    def commit(self, receipt: CorrectionMutationReceipt) -> None: ...


class CorrectionRepositoryPort(Protocol):
    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> ContextManager[CorrectionUnitOfWorkPort]: ...


class CorrectionService:
    """CAS-protected, replay-safe correction mutations."""

    def __init__(self, repository: CorrectionRepositoryPort) -> None:
        self._repository = repository

    def assign_category(
        self,
        command: AssignImageCategoryCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, AssignImageCategoryCommand)

    def clear_category(
        self,
        command: ClearImageCategoryCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, ClearImageCategoryCommand)

    def assign_region_role(
        self,
        command: AssignRegionRoleCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, AssignRegionRoleCommand)

    def clear_region_role(
        self,
        command: ClearRegionRoleCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, ClearRegionRoleCommand)

    def set_manual_caption(
        self,
        command: SetManualCaptionCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, SetManualCaptionCommand)

    def clear_manual_caption(
        self,
        command: ClearManualCaptionCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, ClearManualCaptionCommand)

    def assert_artifact_metadata(
        self,
        command: AssertArtifactMetadataCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, AssertArtifactMetadataCommand)

    def mark_attention(
        self,
        command: MarkAttentionCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, MarkAttentionCommand)

    def resolve(
        self,
        command: ResolveCorrectionsCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, ResolveCorrectionsCommand)

    def reopen(
        self,
        command: ReopenCorrectionsCommand,
    ) -> CorrectionCommandResult:
        return self._typed_execute(command, ReopenCorrectionsCommand)

    def _typed_execute(self, command: Any, expected_type: type) -> CorrectionCommandResult:
        if not isinstance(command, expected_type):
            raise ValidationError(
                f"command must be {expected_type.__name__}",
                code="invalid_correction_command",
            )
        return self._execute(command)

    def _execute(self, command: CorrectionCommand) -> CorrectionCommandResult:
        item_id = self._item_id(command.item_id)
        operation_id = self._operation_id(command.operation_id)
        action = self._action(command)
        payload = self._command_payload(command, item_id=item_id)
        fingerprint = _command_hash(payload)
        try:
            with self._repository.unit_of_work(operation_id=operation_id) as unit:
                replay = self._replay(
                    unit,
                    operation_id=operation_id,
                    command_sha256=fingerprint,
                    action=action,
                    item_id=item_id,
                )
                if replay is not None:
                    return replay
                current = self._current(unit, item_id)
                self._preflight(current, command)
                staged = self._aggregate(unit.stage(current, command), required=True)
                assert staged is not None
                targets = self._validate_staged(current, staged, command)
                inverse = self._inverse(current, staged, command, targets)
                receipt = CorrectionMutationReceipt(
                    action=action,
                    operation_id=operation_id,
                    command_sha256=fingerprint,
                    item_id=item_id,
                    before_aggregate_revision=current.revision,
                    after_aggregate_revision=staged.revision,
                    targets=targets,
                    inverse=inverse,
                )
                unit.commit(receipt)
                return CorrectionCommandResult(receipt)
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the correction repository failed",
                code="correction_repository_unavailable",
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc

    @staticmethod
    def _action(command: CorrectionCommand) -> CorrectionAction:
        actions: tuple[tuple[type, CorrectionAction], ...] = (
            (AssignImageCategoryCommand, "category.assign"),
            (ClearImageCategoryCommand, "category.clear"),
            (AssignRegionRoleCommand, "role.assign"),
            (ClearRegionRoleCommand, "role.clear"),
            (SetManualCaptionCommand, "caption.set"),
            (ClearManualCaptionCommand, "caption.clear"),
            (AssertArtifactMetadataCommand, "metadata.assert"),
            (MarkAttentionCommand, "attention.mark"),
            (ResolveCorrectionsCommand, "attention.resolve"),
            (ReopenCorrectionsCommand, "attention.reopen"),
        )
        for command_type, action in actions:
            if isinstance(command, command_type):
                return action
        raise ValidationError(
            "unsupported correction command",
            code="invalid_correction_command",
        )

    @staticmethod
    def _item_id(value: str) -> str:
        try:
            return _identifier(value, "item_id")
        except EngineError as exc:
            raise ValidationError(
                "item id must be a portable identifier",
                code="invalid_item_id",
            ) from exc

    @staticmethod
    def _operation_id(value: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an operation id is required",
                code="operation_id_required",
                details={"field": "operation_id"},
            )
        try:
            return _identifier(value, "operation_id")
        except EngineError as exc:
            raise ValidationError(
                "operation id must be a portable identifier",
                code="invalid_operation_id",
            ) from exc

    @staticmethod
    def _expected_revision(value: str, field_name: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an expected target revision is required",
                code="target_revision_required",
                details={"field": field_name},
            )
        try:
            return _revision(value, field_name)
        except EngineError as exc:
            raise ValidationError(
                "expected target revision is invalid",
                code="invalid_target_revision",
                details={"field": field_name},
            ) from exc

    @classmethod
    def _command_payload(
        cls,
        command: CorrectionCommand,
        *,
        item_id: str,
    ) -> dict[str, Any]:
        action = cls._action(command)
        value: dict[str, Any] = {"action": action, "item_id": item_id}
        if isinstance(command, (AssignImageCategoryCommand, ClearImageCategoryCommand)):
            value.update(
                {
                    "artifact_id": _identifier(command.artifact_id, "artifact_id"),
                    "expected_artifact_revision": cls._expected_revision(
                        command.expected_artifact_revision,
                        "expected_artifact_revision",
                    ),
                }
            )
            if isinstance(command, AssignImageCategoryCommand):
                assignment = CategoryAssignment(
                    command.category,
                    AssignmentOrigin.MANUAL,
                    "command-validation-r1",
                    provenance=command.provenance,
                )
                value.update(
                    {
                        "category": assignment.category,
                        "provenance": command.provenance.as_dict(),
                    }
                )
        elif isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            value.update(
                {
                    "annotation_id": _identifier(
                        command.annotation_id,
                        "annotation_id",
                    ),
                    "expected_annotation_revision": cls._expected_revision(
                        command.expected_annotation_revision,
                        "expected_annotation_revision",
                    ),
                    "linked_artifact_id": command.linked_artifact_id,
                    "expected_linked_artifact_revision": (
                        command.expected_linked_artifact_revision
                    ),
                }
            )
            if command.linked_artifact_id:
                _identifier(command.linked_artifact_id, "linked_artifact_id")
                cls._expected_revision(
                    command.expected_linked_artifact_revision,
                    "expected_linked_artifact_revision",
                )
            if isinstance(command, AssignRegionRoleCommand):
                value["role"] = canonical_spatial_role(command.role)
                value["provenance"] = command.provenance.as_dict()
        elif isinstance(command, (SetManualCaptionCommand, ClearManualCaptionCommand)):
            value.update(
                {
                    "artifact_id": _identifier(command.artifact_id, "artifact_id"),
                    "expected_artifact_revision": cls._expected_revision(
                        command.expected_artifact_revision,
                        "expected_artifact_revision",
                    ),
                }
            )
            if isinstance(command, SetManualCaptionCommand):
                assertion = CaptionAssertion(
                    command.text,
                    CaptionOrigin.MANUAL,
                    "command-validation-r1",
                    language=command.language,
                    provenance=command.provenance,
                )
                value.update(
                    {
                        "text": assertion.text,
                        "language": assertion.language,
                        "provenance": command.provenance.as_dict(),
                    }
                )
        elif isinstance(command, AssertArtifactMetadataCommand):
            value.update(
                {
                    "artifact_id": _identifier(command.artifact_id, "artifact_id"),
                    "expected_artifact_revision": cls._expected_revision(
                        command.expected_artifact_revision,
                        "expected_artifact_revision",
                    ),
                    "assertions": _thaw(command.assertions),
                    "clear_names": list(command.clear_names),
                    "provenance": command.provenance.as_dict(),
                }
            )
        else:
            assert isinstance(
                command,
                (
                    MarkAttentionCommand,
                    ResolveCorrectionsCommand,
                    ReopenCorrectionsCommand,
                ),
            )
            value.update(
                {
                    "expected_review_revision": cls._expected_revision(
                        command.expected_review_revision,
                        "expected_review_revision",
                    ),
                    "actor_id": _identifier(command.actor_id, "actor_id"),
                    "comment": _safe_text(
                        command.comment,
                        "comment",
                        maximum=8192,
                    ),
                }
            )
            if isinstance(command, MarkAttentionCommand):
                value["reason"] = _safe_text(
                    command.reason,
                    "reason",
                    maximum=2048,
                    allow_empty=False,
                )
        return value

    @staticmethod
    def _aggregate(
        value: Any,
        *,
        required: bool = False,
    ) -> CorrectionAggregateSnapshot | None:
        if value is None and not required:
            return None
        if not isinstance(value, CorrectionAggregateSnapshot):
            raise RepositoryError(
                "the correction repository returned an invalid aggregate",
                code="invalid_correction_snapshot",
            )
        return value

    def _current(
        self,
        unit: CorrectionUnitOfWorkPort,
        item_id: str,
    ) -> CorrectionAggregateSnapshot:
        current = self._aggregate(unit.get(item_id))
        if current is None:
            raise NotFoundError(
                "the correction aggregate does not exist",
                code="correction_aggregate_not_found",
                details={"item_id": item_id},
            )
        if current.item_id != item_id:
            raise RepositoryError(
                "the correction repository returned another item",
                code="correction_repository_scope_mismatch",
            )
        return current

    @staticmethod
    def _artifact(
        aggregate: CorrectionAggregateSnapshot,
        artifact_id: str,
    ) -> ArtifactCorrectionSnapshot:
        artifact = aggregate.artifact(artifact_id)
        if artifact is None:
            alias = next(
                (
                    value
                    for value in aggregate.artifacts
                    if value.key.artifact_id.casefold() == artifact_id.casefold()
                ),
                None,
            )
            if alias is not None:
                raise ConflictError(
                    "the artifact identity differs only by case",
                    code="artifact_identity_alias",
                    details={
                        "requested_artifact_id": artifact_id,
                        "current_artifact_id": alias.key.artifact_id,
                    },
                )
            raise NotFoundError(
                "the raster artifact does not exist",
                code="artifact_not_found",
                details={
                    "item_id": aggregate.item_id,
                    "artifact_id": artifact_id,
                },
            )
        return artifact

    @staticmethod
    def _annotation(
        aggregate: CorrectionAggregateSnapshot,
        annotation_id: str,
    ) -> AnnotationCorrectionSnapshot:
        annotation = aggregate.annotation(annotation_id)
        if annotation is None:
            alias = next(
                (
                    value
                    for value in aggregate.annotations
                    if value.key.annotation_id.casefold() == annotation_id.casefold()
                ),
                None,
            )
            if alias is not None:
                raise ConflictError(
                    "the annotation identity differs only by case",
                    code="annotation_identity_alias",
                    details={
                        "requested_annotation_id": annotation_id,
                        "current_annotation_id": alias.key.annotation_id,
                    },
                )
            raise NotFoundError(
                "the spatial annotation does not exist",
                code="annotation_not_found",
                details={
                    "item_id": aggregate.item_id,
                    "annotation_id": annotation_id,
                },
            )
        return annotation

    @staticmethod
    def _match_revision(
        *,
        kind: str,
        target_id: str,
        current_revision: str,
        expected_revision: str,
    ) -> None:
        if current_revision != expected_revision:
            raise ConflictError(
                f"the {kind} changed elsewhere",
                code=f"{kind}_revision_conflict",
                details={
                    f"{kind}_id": target_id,
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )

    def _preflight(
        self,
        current: CorrectionAggregateSnapshot,
        command: CorrectionCommand,
    ) -> None:
        if isinstance(command, (AssignImageCategoryCommand, ClearImageCategoryCommand)):
            artifact = self._artifact(current, command.artifact_id)
            self._match_revision(
                kind="artifact",
                target_id=artifact.key.artifact_id,
                current_revision=artifact.revision,
                expected_revision=command.expected_artifact_revision,
            )
            if (
                isinstance(command, ClearImageCategoryCommand)
                and artifact.category(AssignmentOrigin.MANUAL) is None
            ):
                raise ConflictError(
                    "the artifact has no manual category to clear",
                    code="manual_category_not_found",
                    details={"artifact_id": artifact.key.artifact_id},
                )
            return
        if isinstance(command, (SetManualCaptionCommand, ClearManualCaptionCommand)):
            artifact = self._artifact(current, command.artifact_id)
            self._match_revision(
                kind="artifact",
                target_id=artifact.key.artifact_id,
                current_revision=artifact.revision,
                expected_revision=command.expected_artifact_revision,
            )
            if (
                isinstance(command, ClearManualCaptionCommand)
                and artifact.caption(CaptionOrigin.MANUAL) is None
            ):
                raise ConflictError(
                    "the artifact has no manual caption to clear",
                    code="manual_caption_not_found",
                    details={"artifact_id": artifact.key.artifact_id},
                )
            return
        if isinstance(command, AssertArtifactMetadataCommand):
            artifact = self._artifact(current, command.artifact_id)
            self._match_revision(
                kind="artifact",
                target_id=artifact.key.artifact_id,
                current_revision=artifact.revision,
                expected_revision=command.expected_artifact_revision,
            )
            if any(
                artifact.metadata(name, MetadataAssertionOrigin.MANUAL) is None
                for name in command.clear_names
            ):
                raise ConflictError(
                    "a requested manual metadata assertion does not exist",
                    code="manual_metadata_not_found",
                    details={"artifact_id": artifact.key.artifact_id},
                )
            return
        if isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            annotation = self._annotation(current, command.annotation_id)
            self._match_revision(
                kind="annotation",
                target_id=annotation.key.annotation_id,
                current_revision=annotation.revision,
                expected_revision=command.expected_annotation_revision,
            )
            if (
                isinstance(command, ClearRegionRoleCommand)
                and annotation.role(RoleAssignmentOrigin.MANUAL) is None
            ):
                raise ConflictError(
                    "the annotation has no manual role to clear",
                    code="manual_role_not_found",
                    details={"annotation_id": annotation.key.annotation_id},
                )
            role = (
                canonical_spatial_role(command.role)
                if isinstance(command, AssignRegionRoleCommand)
                else ""
            )
            linked_id = annotation.linked_artifact_id
            supplied_id = command.linked_artifact_id
            if linked_id and not supplied_id:
                raise PreconditionRequiredError(
                    "the linked artifact revision is required",
                    code="linked_artifact_revision_required",
                    details={"linked_artifact_id": linked_id},
                )
            if linked_id and supplied_id != linked_id:
                raise ConflictError(
                    "the annotation is linked to another artifact",
                    code="linked_artifact_conflict",
                    details={
                        "annotation_id": annotation.key.annotation_id,
                        "current_linked_artifact_id": linked_id,
                        "requested_linked_artifact_id": supplied_id,
                    },
                )
            if not linked_id and role == "figure" and not supplied_id:
                raise PreconditionRequiredError(
                    "a figure role requires its extracted artifact revision",
                    code="linked_artifact_revision_required",
                    details={"annotation_id": annotation.key.annotation_id},
                )
            if not linked_id and supplied_id and role != "figure":
                raise ConflictError(
                    "only a figure assignment may introduce an artifact link",
                    code="unexpected_linked_artifact",
                    details={"annotation_id": annotation.key.annotation_id},
                )
            if supplied_id:
                linked = self._artifact(current, supplied_id)
                self._match_revision(
                    kind="artifact",
                    target_id=linked.key.artifact_id,
                    current_revision=linked.revision,
                    expected_revision=command.expected_linked_artifact_revision,
                )
            return

        assert isinstance(
            command,
            (
                MarkAttentionCommand,
                ResolveCorrectionsCommand,
                ReopenCorrectionsCommand,
            ),
        )
        self._match_revision(
            kind="review",
            target_id=current.item_id,
            current_revision=current.review.revision,
            expected_revision=command.expected_review_revision,
        )
        expected_state = {
            MarkAttentionCommand: ReviewState.CLEAR,
            ResolveCorrectionsCommand: ReviewState.NEEDS_ATTENTION,
            ReopenCorrectionsCommand: ReviewState.RESOLVED,
        }[type(command)]
        if current.review.state is not expected_state:
            raise ConflictError(
                "the review is not in the required state",
                code="correction_review_state_conflict",
                details={
                    "item_id": current.item_id,
                    "required_state": expected_state.value,
                    "current_state": current.review.state.value,
                },
            )

    @staticmethod
    def _same_except(before: Any, after: Any, *fields: str) -> bool:
        before_value = before.as_dict()
        after_value = after.as_dict()
        for field_name in fields:
            before_value.pop(field_name, None)
            after_value.pop(field_name, None)
        return _canonical(before_value) == _canonical(after_value)

    @staticmethod
    def _non_manual(values: Sequence[Any], manual_origin: Enum) -> tuple[Any, ...]:
        return tuple(value for value in values if value.origin is not manual_origin)

    @staticmethod
    def _same_values(left: Sequence[Any], right: Sequence[Any]) -> bool:
        return _canonical([value.as_dict() for value in left]) == _canonical(
            [value.as_dict() for value in right]
        )

    @staticmethod
    def _same_provenance(
        left: ArtifactProvenance,
        right: ArtifactProvenance,
    ) -> bool:
        return _canonical(left.as_dict()) == _canonical(right.as_dict())

    def _validate_staged(
        self,
        current: CorrectionAggregateSnapshot,
        staged: CorrectionAggregateSnapshot,
        command: CorrectionCommand,
    ) -> tuple[CorrectionTargetRevision, ...]:
        if staged.item_id != current.item_id:
            raise RepositoryError(
                "the correction repository staged another item",
                code="correction_repository_scope_mismatch",
            )
        if staged.revision == current.revision:
            raise RepositoryError(
                "the correction aggregate revision did not advance",
                code="correction_revision_not_advanced",
            )

        artifact_ids: set[str] = set()
        annotation_ids: set[str] = set()
        review_changed = False
        if isinstance(
            command,
            (
                AssignImageCategoryCommand,
                ClearImageCategoryCommand,
                SetManualCaptionCommand,
                ClearManualCaptionCommand,
                AssertArtifactMetadataCommand,
            ),
        ):
            artifact_ids.add(command.artifact_id)
        elif isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            annotation_ids.add(command.annotation_id)
            if command.linked_artifact_id:
                artifact_ids.add(command.linked_artifact_id)
        else:
            review_changed = True

        before_artifacts = {value.key.artifact_id: value for value in current.artifacts}
        after_artifacts = {value.key.artifact_id: value for value in staged.artifacts}
        before_annotations = {
            value.key.annotation_id: value for value in current.annotations
        }
        after_annotations = {
            value.key.annotation_id: value for value in staged.annotations
        }
        if set(before_artifacts) != set(after_artifacts) or set(
            before_annotations
        ) != set(after_annotations):
            raise RepositoryError(
                "the correction repository changed target identities",
                code="correction_repository_content_mismatch",
            )
        if any(
            before_artifacts[key] != after_artifacts[key]
            for key in before_artifacts
            if key not in artifact_ids
        ) or any(
            before_annotations[key] != after_annotations[key]
            for key in before_annotations
            if key not in annotation_ids
        ):
            raise RepositoryError(
                "the correction repository changed an unrelated target",
                code="correction_repository_content_mismatch",
            )
        if review_changed:
            if current.artifacts != staged.artifacts or current.annotations != staged.annotations:
                raise RepositoryError(
                    "a review mutation changed artifact assertions",
                    code="correction_repository_content_mismatch",
                )
        elif current.review != staged.review:
            raise RepositoryError(
                "an assertion mutation changed correction review state",
                code="correction_repository_content_mismatch",
            )

        if isinstance(command, (AssignImageCategoryCommand, ClearImageCategoryCommand)):
            self._validate_category(
                before_artifacts[command.artifact_id],
                after_artifacts[command.artifact_id],
                command,
            )
        elif isinstance(command, (SetManualCaptionCommand, ClearManualCaptionCommand)):
            self._validate_caption(
                before_artifacts[command.artifact_id],
                after_artifacts[command.artifact_id],
                command,
            )
        elif isinstance(command, AssertArtifactMetadataCommand):
            self._validate_metadata(
                before_artifacts[command.artifact_id],
                after_artifacts[command.artifact_id],
                command,
            )
        elif isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            self._validate_role(
                before_annotations[command.annotation_id],
                after_annotations[command.annotation_id],
                (
                    before_artifacts.get(command.linked_artifact_id)
                    if command.linked_artifact_id
                    else None
                ),
                (
                    after_artifacts.get(command.linked_artifact_id)
                    if command.linked_artifact_id
                    else None
                ),
                command,
            )
        else:
            self._validate_review(current.review, staged.review, command)

        changes: list[CorrectionTargetRevision] = []
        for artifact_id in artifact_ids:
            changes.append(
                CorrectionTargetRevision(
                    CorrectionTargetKind.ARTIFACT,
                    artifact_id,
                    before_artifacts[artifact_id].revision,
                    after_artifacts[artifact_id].revision,
                )
            )
        for annotation_id in annotation_ids:
            changes.append(
                CorrectionTargetRevision(
                    CorrectionTargetKind.ANNOTATION,
                    annotation_id,
                    before_annotations[annotation_id].revision,
                    after_annotations[annotation_id].revision,
                )
            )
        if review_changed:
            changes.append(
                CorrectionTargetRevision(
                    CorrectionTargetKind.REVIEW,
                    current.item_id,
                    current.review.revision,
                    staged.review.revision,
                )
            )
        return tuple(sorted(changes, key=lambda value: (value.kind.value, value.target_id)))

    def _validate_category(
        self,
        before: ArtifactCorrectionSnapshot,
        after: ArtifactCorrectionSnapshot,
        command: AssignImageCategoryCommand | ClearImageCategoryCommand,
    ) -> None:
        left_non_manual = self._non_manual(
            before.category_assignments,
            AssignmentOrigin.MANUAL,
        )
        right_non_manual = self._non_manual(
            after.category_assignments,
            AssignmentOrigin.MANUAL,
        )
        if not self._same_except(
            before,
            after,
            "revision",
            "category_assignments",
        ) or not self._same_values(left_non_manual, right_non_manual):
            raise RepositoryError(
                "the repository staged unrelated artifact changes",
                code="correction_repository_content_mismatch",
            )
        manual = after.category(AssignmentOrigin.MANUAL)
        if isinstance(command, AssignImageCategoryCommand):
            previous_manual = before.category(AssignmentOrigin.MANUAL)
            if (
                manual is None
                or manual.category != command.category
                or not self._same_provenance(manual.provenance, command.provenance)
                or (
                    previous_manual is not None
                    and manual.revision == previous_manual.revision
                )
            ):
                raise RepositoryError(
                    "the repository staged another category assignment",
                    code="correction_repository_content_mismatch",
                )
        elif manual is not None:
            raise RepositoryError(
                "the repository did not clear the manual category",
                code="correction_repository_content_mismatch",
            )

    def _validate_caption(
        self,
        before: ArtifactCorrectionSnapshot,
        after: ArtifactCorrectionSnapshot,
        command: SetManualCaptionCommand | ClearManualCaptionCommand,
    ) -> None:
        left_non_manual = self._non_manual(
            before.caption_assertions,
            CaptionOrigin.MANUAL,
        )
        right_non_manual = self._non_manual(
            after.caption_assertions,
            CaptionOrigin.MANUAL,
        )
        if not self._same_except(
            before,
            after,
            "revision",
            "caption_assertions",
        ) or not self._same_values(left_non_manual, right_non_manual):
            raise RepositoryError(
                "the repository changed machine caption assertions",
                code="correction_repository_content_mismatch",
            )
        manual = after.caption(CaptionOrigin.MANUAL)
        if isinstance(command, SetManualCaptionCommand):
            previous_manual = before.caption(CaptionOrigin.MANUAL)
            if (
                manual is None
                or manual.text != command.text
                or manual.language != command.language
                or not self._same_provenance(manual.provenance, command.provenance)
                or (
                    previous_manual is not None
                    and manual.revision == previous_manual.revision
                )
            ):
                raise RepositoryError(
                    "the repository staged another manual caption",
                    code="correction_repository_content_mismatch",
                )
        elif manual is not None:
            raise RepositoryError(
                "the repository did not clear the manual caption",
                code="correction_repository_content_mismatch",
            )

    def _validate_metadata(
        self,
        before: ArtifactCorrectionSnapshot,
        after: ArtifactCorrectionSnapshot,
        command: AssertArtifactMetadataCommand,
    ) -> None:
        left_non_manual = self._non_manual(
            before.metadata_assertions,
            MetadataAssertionOrigin.MANUAL,
        )
        right_non_manual = self._non_manual(
            after.metadata_assertions,
            MetadataAssertionOrigin.MANUAL,
        )
        if not self._same_except(
            before,
            after,
            "revision",
            "metadata_assertions",
        ) or not self._same_values(left_non_manual, right_non_manual):
            raise RepositoryError(
                "the repository changed machine metadata assertions",
                code="correction_repository_content_mismatch",
            )
        changed_names = set(command.assertions) | set(command.clear_names)
        before_unchanged = tuple(
            value
            for value in before.metadata_assertions
            if value.origin is MetadataAssertionOrigin.MANUAL
            and value.name not in changed_names
        )
        after_unchanged = tuple(
            value
            for value in after.metadata_assertions
            if value.origin is MetadataAssertionOrigin.MANUAL
            and value.name not in changed_names
        )
        if not self._same_values(before_unchanged, after_unchanged):
            raise RepositoryError(
                "the repository changed unrelated manual metadata",
                code="correction_repository_content_mismatch",
            )
        for name, expected in command.assertions.items():
            assertion = after.metadata(name, MetadataAssertionOrigin.MANUAL)
            previous_assertion = before.metadata(
                name,
                MetadataAssertionOrigin.MANUAL,
            )
            if (
                assertion is None
                or _canonical(assertion.value) != _canonical(expected)
                or not self._same_provenance(
                    assertion.provenance,
                    command.provenance,
                )
                or (
                    previous_assertion is not None
                    and assertion.revision == previous_assertion.revision
                )
            ):
                raise RepositoryError(
                    "the repository staged another metadata assertion",
                    code="correction_repository_content_mismatch",
                )
        if any(
            after.metadata(name, MetadataAssertionOrigin.MANUAL) is not None
            for name in command.clear_names
        ):
            raise RepositoryError(
                "the repository did not clear manual metadata",
                code="correction_repository_content_mismatch",
            )

    def _validate_role(
        self,
        before: AnnotationCorrectionSnapshot,
        after: AnnotationCorrectionSnapshot,
        before_artifact: ArtifactCorrectionSnapshot | None,
        after_artifact: ArtifactCorrectionSnapshot | None,
        command: AssignRegionRoleCommand | ClearRegionRoleCommand,
    ) -> None:
        left_non_manual = self._non_manual(
            before.role_assignments,
            RoleAssignmentOrigin.MANUAL,
        )
        right_non_manual = self._non_manual(
            after.role_assignments,
            RoleAssignmentOrigin.MANUAL,
        )
        if not self._same_except(
            before,
            after,
            "revision",
            "role_assignments",
            "linked_artifact_id",
        ) or not self._same_values(left_non_manual, right_non_manual):
            raise RepositoryError(
                "the repository changed machine region roles",
                code="correction_repository_content_mismatch",
            )
        expected_link = before.linked_artifact_id or command.linked_artifact_id
        if after.linked_artifact_id != expected_link:
            raise RepositoryError(
                "the repository staged another artifact link",
                code="correction_repository_content_mismatch",
            )
        manual = after.role(RoleAssignmentOrigin.MANUAL)
        expected_role = (
            canonical_spatial_role(command.role)
            if isinstance(command, AssignRegionRoleCommand)
            else ""
        )
        if isinstance(command, AssignRegionRoleCommand):
            previous_manual = before.role(RoleAssignmentOrigin.MANUAL)
            if (
                manual is None
                or manual.role != expected_role
                or not self._same_provenance(manual.provenance, command.provenance)
                or (
                    previous_manual is not None
                    and manual.revision == previous_manual.revision
                )
            ):
                raise RepositoryError(
                    "the repository staged another region role",
                    code="correction_repository_content_mismatch",
                )
        elif manual is not None:
            raise RepositoryError(
                "the repository did not clear the manual region role",
                code="correction_repository_content_mismatch",
            )
        if command.linked_artifact_id:
            if before_artifact is None or after_artifact is None:
                raise RepositoryError(
                    "the repository omitted the linked artifact mutation",
                    code="correction_repository_content_mismatch",
                )
            left_non_manual = self._non_manual(
                before_artifact.role_assignments,
                RoleAssignmentOrigin.MANUAL,
            )
            right_non_manual = self._non_manual(
                after_artifact.role_assignments,
                RoleAssignmentOrigin.MANUAL,
            )
            if not self._same_except(
                before_artifact,
                after_artifact,
                "revision",
                "role_assignments",
            ) or not self._same_values(left_non_manual, right_non_manual):
                raise RepositoryError(
                    "the repository changed unrelated linked artifact state",
                    code="correction_repository_content_mismatch",
            )
            artifact_manual = after_artifact.role(RoleAssignmentOrigin.MANUAL)
            if isinstance(command, AssignRegionRoleCommand):
                previous_artifact_manual = before_artifact.role(
                    RoleAssignmentOrigin.MANUAL
                )
                if (
                    artifact_manual is None
                    or artifact_manual.role != expected_role
                    or not self._same_provenance(
                        artifact_manual.provenance,
                        command.provenance,
                    )
                    or (
                        previous_artifact_manual is not None
                        and artifact_manual.revision
                        == previous_artifact_manual.revision
                    )
                ):
                    raise RepositoryError(
                        "linked artifact role was not updated atomically",
                        code="correction_repository_content_mismatch",
                    )
            elif artifact_manual is not None:
                raise RepositoryError(
                    "linked artifact role was not cleared atomically",
                    code="correction_repository_content_mismatch",
                )

    def _validate_review(
        self,
        before: CorrectionReviewSnapshot,
        after: CorrectionReviewSnapshot,
        command: MarkAttentionCommand
        | ResolveCorrectionsCommand
        | ReopenCorrectionsCommand,
    ) -> None:
        if after.revision == before.revision or after.history[:-1] != before.history:
            raise RepositoryError(
                "the repository did not append review history",
                code="correction_repository_content_mismatch",
            )
        if len(after.history) != len(before.history) + 1:
            raise RepositoryError(
                "the repository changed review audit history",
                code="correction_repository_content_mismatch",
            )
        event = after.history[-1]
        action = self._action(command)
        expected_state = {
            "attention.mark": ReviewState.NEEDS_ATTENTION,
            "attention.resolve": ReviewState.RESOLVED,
            "attention.reopen": ReviewState.NEEDS_ATTENTION,
        }[action]
        expected_reason = command.reason if isinstance(command, MarkAttentionCommand) else before.reason
        if (
            event.operation_id != command.operation_id
            or event.action != action
            or event.actor_id != command.actor_id
            or event.comment != command.comment
            or event.reason != (command.reason if isinstance(command, MarkAttentionCommand) else "")
            or event.before_state is not before.state
            or event.after_state is not expected_state
            or after.state is not expected_state
            or after.reason != expected_reason
        ):
            raise RepositoryError(
                "the repository staged another review transition",
                code="correction_repository_content_mismatch",
            )

    def _inverse(
        self,
        current: CorrectionAggregateSnapshot,
        staged: CorrectionAggregateSnapshot,
        command: CorrectionCommand,
        targets: tuple[CorrectionTargetRevision, ...],
    ) -> CorrectionInverse:
        action: str
        payload: dict[str, Any]
        if isinstance(command, (AssignImageCategoryCommand, ClearImageCategoryCommand)):
            before = self._artifact(current, command.artifact_id)
            manual = before.category(AssignmentOrigin.MANUAL)
            if manual is None:
                action = "category.clear"
                payload = {"artifact_id": command.artifact_id}
            else:
                action = "category.assign"
                payload = {
                    "artifact_id": command.artifact_id,
                    "assignment": manual.as_dict(),
                }
        elif isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            before = self._annotation(current, command.annotation_id)
            manual = before.role(RoleAssignmentOrigin.MANUAL)
            linked_before = (
                self._artifact(current, command.linked_artifact_id).role(
                    RoleAssignmentOrigin.MANUAL
                )
                if command.linked_artifact_id
                else None
            )
            if manual is None:
                action = "role.clear"
                payload = {
                    "annotation_id": command.annotation_id,
                    "linked_artifact_id": command.linked_artifact_id,
                    "linked_assignment": (
                        linked_before.as_dict() if linked_before is not None else None
                    ),
                }
            else:
                action = "role.assign"
                payload = {
                    "annotation_id": command.annotation_id,
                    "assignment": manual.as_dict(),
                    "linked_artifact_id": command.linked_artifact_id,
                    "linked_assignment": (
                        linked_before.as_dict() if linked_before is not None else None
                    ),
                }
        elif isinstance(command, (SetManualCaptionCommand, ClearManualCaptionCommand)):
            before = self._artifact(current, command.artifact_id)
            manual = before.caption(CaptionOrigin.MANUAL)
            if manual is None:
                action = "caption.clear"
                payload = {"artifact_id": command.artifact_id}
            else:
                action = "caption.set"
                payload = {
                    "artifact_id": command.artifact_id,
                    "assertion": manual.as_dict(),
                }
        elif isinstance(command, AssertArtifactMetadataCommand):
            before = self._artifact(current, command.artifact_id)
            names = sorted(set(command.assertions) | set(command.clear_names))
            prior = [
                assertion.as_dict()
                for name in names
                if (
                    assertion := before.metadata(
                        name,
                        MetadataAssertionOrigin.MANUAL,
                    )
                )
                is not None
            ]
            prior_names = {value["name"] for value in prior}
            action = "metadata.assert"
            payload = {
                "artifact_id": command.artifact_id,
                "restore_assertions": prior,
                "clear_names": [name for name in names if name not in prior_names],
            }
        elif isinstance(command, MarkAttentionCommand):
            action = "attention.clear"
            payload = {
                "reason": command.reason,
                "append_audit": True,
            }
        elif isinstance(command, ResolveCorrectionsCommand):
            action = "attention.reopen"
            payload = {
                "reason": current.review.reason,
                "append_audit": True,
            }
        else:
            assert isinstance(command, ReopenCorrectionsCommand)
            action = "attention.resolve"
            payload = {
                "reason": current.review.reason,
                "append_audit": True,
            }
        return CorrectionInverse(
            action=action,
            expected_aggregate_revision=staged.revision,
            expected_targets=targets,
            payload=payload,
        )

    @staticmethod
    def _replay(
        unit: CorrectionUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        action: CorrectionAction,
        item_id: str,
    ) -> CorrectionCommandResult | None:
        prior = unit.receipt(operation_id)
        if prior is None:
            return None
        if not isinstance(prior, CorrectionMutationReceipt):
            raise RepositoryError(
                "the correction repository returned an invalid receipt",
                code="invalid_correction_receipt",
            )
        if prior.operation_id != operation_id:
            raise RepositoryError(
                "the correction repository returned another operation",
                code="receipt_scope_mismatch",
            )
        if prior.command_sha256 != command_sha256 or prior.action != action:
            raise ConflictError(
                "operation id was already used for another correction",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        if prior.item_id != item_id:
            raise RepositoryError(
                "the correction receipt has the wrong item scope",
                code="receipt_scope_mismatch",
            )
        return CorrectionCommandResult(prior, replayed=True)


__all__ = [
    "AnnotationCorrectionSnapshot",
    "ArtifactCorrectionSnapshot",
    "ArtifactMetadataAssertion",
    "AssertArtifactMetadataCommand",
    "AssignImageCategoryCommand",
    "AssignRegionRoleCommand",
    "ClearImageCategoryCommand",
    "ClearManualCaptionCommand",
    "ClearRegionRoleCommand",
    "CorrectionAction",
    "CorrectionAggregateSnapshot",
    "CorrectionAuditEvent",
    "CorrectionCommand",
    "CorrectionCommandResult",
    "CorrectionInverse",
    "CorrectionMutationReceipt",
    "CorrectionRepositoryPort",
    "CorrectionReviewSnapshot",
    "CorrectionService",
    "CorrectionTargetKind",
    "CorrectionTargetRevision",
    "CorrectionUnitOfWorkPort",
    "EffectiveCategoryOrigin",
    "EffectiveImageCategory",
    "MarkAttentionCommand",
    "MetadataAssertionOrigin",
    "ReopenCorrectionsCommand",
    "ResolveCorrectionsCommand",
    "ReviewState",
    "ReviewAuditAction",
    "SetManualCaptionCommand",
]
