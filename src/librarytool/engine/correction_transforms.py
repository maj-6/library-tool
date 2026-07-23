"""Headless orchestration for immutable raster correction jobs.

This module owns portable commands, job registration, worker-facing ports, and
pure draft construction.  Persistence adapters must atomically compare the
source pins carried by :class:`CorrectionTransformCommitDraft` before
publishing its four immutable outputs.  OCR is deliberately a second outcome:
it can propose machine data after the image commit, but it cannot mutate the
human assertions carried in that commit.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import threading
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from PIL import Image

from librarytool.processing.raster import (
    ManualBinaryAdjustRecipe,
    RasterInputError,
    apply_perspective_transform,
    validate_normalized_quad,
)

from .errors import ConflictError, ValidationError
from .jobs import JobFailure, JobManager, JobOutput, JobProgress, JobView
from .raster_artifacts import (
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
)
from .spatial_annotations import (
    NormalizedPoint,
    RoleAssignmentOrigin,
    SpatialAnnotationView,
    SpatialRoleAssignment,
)


CORRECTION_TRANSFORM_JOB_KIND = "correction.transform"
CORRECTION_TRANSFORM_SCHEMA = "org.whl.correction-transform-command"
CORRECTION_TRANSFORM_VERSION = 1
CORRECTION_OUTPUT_KINDS = (
    "corrected-display",
    "ocr-ready",
    "thumbnail",
    "transform-manifest",
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_COMMAND_FIELDS = frozenset(
    {
        "schema",
        "version",
        "item_id",
        "artifact_id",
        "artifact_revision",
        "source_revision",
        "source_sha256",
        "quad",
        "adjustment",
        "rerun_ocr",
        "operation_id",
    }
)
_MANUAL_ADJUSTMENT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "algorithm",
        "contrast_percent",
        "brightness_percent",
        "threshold",
        "threshold_rule",
        "comparison",
    }
)


def _validation(message: str, *, code: str, field_name: str) -> ValidationError:
    return ValidationError(message, code=code, details={"field": field_name})


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a portable opaque identifier",
            code="invalid_correction_transform",
            field_name=field_name,
        )
    return value


def _revision(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or any(character.isspace() for character in value)
        or '"' in value
        or "\\" in value
    ):
        raise _validation(
            f"{field_name} must be a revision token",
            code="invalid_correction_transform",
            field_name=field_name,
        )
    return value


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a SHA-256 digest",
            code="invalid_correction_transform",
            field_name=field_name,
        )
    return value.casefold()


def _bounded_text(value: Any, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _validation(
            f"{field_name} must be a non-empty bounded string",
            code="invalid_correction_transform",
            field_name=field_name,
        )
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise _validation(
            f"{field_name} contains an unsafe character",
            code="invalid_correction_transform",
            field_name=field_name,
        )
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CorrectionTransformCommand:
    """A serializable correction request pinned to immutable source state."""

    item_id: str
    artifact_id: str
    artifact_revision: str
    source_revision: str
    source_sha256: str
    quad: tuple[tuple[float, float], ...]
    operation_id: str
    adjustment: ManualBinaryAdjustRecipe | None = None
    rerun_ocr: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "artifact_id",
            _identifier(self.artifact_id, "artifact_id"),
        )
        object.__setattr__(
            self,
            "artifact_revision",
            _revision(self.artifact_revision, "artifact_revision"),
        )
        object.__setattr__(
            self,
            "source_revision",
            _revision(self.source_revision, "source_revision"),
        )
        object.__setattr__(
            self,
            "source_sha256",
            _sha256(self.source_sha256, "source_sha256"),
        )
        try:
            quad = validate_normalized_quad(self.quad)
        except RasterInputError as exc:
            raise _validation(
                str(exc),
                code="invalid_correction_quad",
                field_name="quad",
            ) from exc
        object.__setattr__(self, "quad", quad)
        if self.adjustment is not None and not isinstance(
            self.adjustment,
            ManualBinaryAdjustRecipe,
        ):
            raise _validation(
                "adjustment must be a ManualBinaryAdjustRecipe or null",
                code="invalid_correction_transform",
                field_name="adjustment",
            )
        if not isinstance(self.rerun_ocr, bool):
            raise _validation(
                "rerun_ocr must be boolean",
                code="invalid_correction_transform",
                field_name="rerun_ocr",
            )
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )

    @property
    def key(self) -> RasterArtifactKey:
        return RasterArtifactKey(self.item_id, self.artifact_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CORRECTION_TRANSFORM_SCHEMA,
            "version": CORRECTION_TRANSFORM_VERSION,
            "item_id": self.item_id,
            "artifact_id": self.artifact_id,
            "artifact_revision": self.artifact_revision,
            "source_revision": self.source_revision,
            "source_sha256": self.source_sha256,
            "quad": [[x, y] for x, y in self.quad],
            "adjustment": (
                None if self.adjustment is None else self.adjustment.as_dict()
            ),
            "rerun_ocr": self.rerun_ocr,
            "operation_id": self.operation_id,
        }

    @property
    def serialized(self) -> bytes:
        return _canonical_json(self.as_dict())

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.serialized).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CorrectionTransformCommand":
        if not isinstance(payload, Mapping):
            raise _validation(
                "correction transform command must be an object",
                code="invalid_correction_transform",
                field_name="command",
            )
        payload_fields = frozenset(payload)
        if payload_fields != _COMMAND_FIELDS:
            raise ValidationError(
                "correction transform command fields must match its schema exactly",
                code="invalid_correction_transform",
                details={
                    "field": "command",
                    "missing": sorted(_COMMAND_FIELDS - payload_fields),
                    "unknown": sorted(str(value) for value in payload_fields - _COMMAND_FIELDS),
                },
            )
        version = payload.get("version")
        if (
            payload.get("schema") != CORRECTION_TRANSFORM_SCHEMA
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version != CORRECTION_TRANSFORM_VERSION
        ):
            raise _validation(
                "unsupported correction transform command schema",
                code="invalid_correction_transform",
                field_name="schema",
            )
        adjustment_payload = payload.get("adjustment")
        adjustment = None
        if adjustment_payload is not None:
            if not isinstance(adjustment_payload, Mapping):
                raise _validation(
                    "adjustment must be an object or null",
                    code="invalid_correction_transform",
                    field_name="adjustment",
                )
            adjustment_fields = frozenset(adjustment_payload)
            if adjustment_fields != _MANUAL_ADJUSTMENT_FIELDS:
                raise ValidationError(
                    "manual binary adjustment fields must match its schema exactly",
                    code="invalid_correction_transform",
                    details={
                        "field": "adjustment",
                        "missing": sorted(
                            _MANUAL_ADJUSTMENT_FIELDS - adjustment_fields
                        ),
                        "unknown": sorted(
                            str(value)
                            for value in adjustment_fields - _MANUAL_ADJUSTMENT_FIELDS
                        ),
                    },
                )
            adjustment_version = adjustment_payload.get("version")
            if (
                adjustment_payload.get("schema")
                != "org.whl.raster.manual-binary-adjust"
                or not isinstance(adjustment_version, int)
                or isinstance(adjustment_version, bool)
                or adjustment_version != 1
            ):
                raise _validation(
                    "unsupported manual binary adjustment schema",
                    code="invalid_correction_transform",
                    field_name="adjustment.schema",
                )
            try:
                adjustment = ManualBinaryAdjustRecipe(
                    contrast=adjustment_payload["contrast_percent"],
                    brightness=adjustment_payload["brightness_percent"],
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise _validation(
                    "adjustment contains invalid manual binary parameters",
                    code="invalid_correction_transform",
                    field_name="adjustment",
                ) from exc
            canonical_adjustment = adjustment.as_dict()
            mismatched_adjustment_fields = sorted(
                field_name
                for field_name in _MANUAL_ADJUSTMENT_FIELDS
                if (
                    type(adjustment_payload[field_name])
                    is not type(canonical_adjustment[field_name])
                    or adjustment_payload[field_name]
                    != canonical_adjustment[field_name]
                )
            )
            if mismatched_adjustment_fields:
                raise ValidationError(
                    "manual binary adjustment canonical recipe values do not match: "
                    + ", ".join(mismatched_adjustment_fields),
                    code="invalid_correction_transform",
                    details={
                        "field": f"adjustment.{mismatched_adjustment_fields[0]}",
                        "mismatched": mismatched_adjustment_fields,
                    },
                )
        try:
            return cls(
                item_id=payload["item_id"],
                artifact_id=payload["artifact_id"],
                artifact_revision=payload["artifact_revision"],
                source_revision=payload["source_revision"],
                source_sha256=payload["source_sha256"],
                quad=tuple(tuple(point) for point in payload["quad"]),
                operation_id=payload["operation_id"],
                adjustment=adjustment,
                rerun_ocr=payload["rerun_ocr"],
            )
        except KeyError as exc:
            raise _validation(
                f"correction transform command is missing {exc.args[0]}",
                code="invalid_correction_transform",
                field_name=str(exc.args[0]),
            ) from exc
        except TypeError as exc:
            raise _validation(
                "correction transform quad must contain coordinate pairs",
                code="invalid_correction_quad",
                field_name="quad",
            ) from exc


class HumanTextOrigin(str, Enum):
    MANUAL = "manual"
    IMPORTED = "imported"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class HumanTextAssertion:
    assertion_id: str
    revision: str
    text: str
    origin: HumanTextOrigin | str = HumanTextOrigin.MANUAL
    language: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assertion_id",
            _identifier(self.assertion_id, "assertion_id"),
        )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        object.__setattr__(self, "text", _bounded_text(self.text, "text", maximum=1_000_000))
        try:
            origin = HumanTextOrigin(self.origin)
        except (TypeError, ValueError) as exc:
            raise _validation(
                "text origin must be manual, imported, or verified",
                code="invalid_correction_transform",
                field_name="origin",
            ) from exc
        object.__setattr__(self, "origin", origin)
        if not isinstance(self.language, str) or len(self.language) > 64:
            raise _validation(
                "language must be a bounded string",
                code="invalid_correction_transform",
                field_name="language",
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "assertion_id": self.assertion_id,
            "revision": self.revision,
            "text": self.text,
            "origin": self.origin.value,
            "language": self.language,
        }


@dataclass(frozen=True, slots=True)
class CorrectionSourceSnapshot:
    """Immutable bytes and public annotations read from one source revision."""

    artifact: RasterArtifactView
    source_revision: str
    content: bytes
    annotations: tuple[SpatialAnnotationView, ...] = ()
    human_text_assertions: tuple[HumanTextAssertion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, RasterArtifactView):
            raise TypeError("artifact must be a RasterArtifactView")
        object.__setattr__(
            self,
            "source_revision",
            _revision(self.source_revision, "source_revision"),
        )
        if not isinstance(self.content, bytes):
            raise TypeError("source content must be immutable bytes")
        digest = hashlib.sha256(self.content).hexdigest()
        if digest != self.artifact.content_sha256:
            raise _validation(
                "source bytes do not match the artifact checksum",
                code="correction_source_checksum_mismatch",
                field_name="content",
            )
        annotations = tuple(self.annotations)
        if any(not isinstance(value, SpatialAnnotationView) for value in annotations):
            raise TypeError("annotations must contain SpatialAnnotationView values")
        if any(value.key.item_id != self.artifact.key.item_id for value in annotations):
            raise _validation(
                "annotation item identity does not match the source artifact",
                code="correction_annotation_source_mismatch",
                field_name="annotations",
            )
        annotation_ids = tuple(value.key.annotation_id for value in annotations)
        if len(set(annotation_ids)) != len(annotation_ids):
            raise _validation(
                "annotations must have unique identities",
                code="correction_annotation_source_mismatch",
                field_name="annotations",
            )
        object.__setattr__(self, "annotations", annotations)
        text = tuple(self.human_text_assertions)
        if any(not isinstance(value, HumanTextAssertion) for value in text):
            raise TypeError("human_text_assertions must contain HumanTextAssertion values")
        text_ids = tuple(value.assertion_id for value in text)
        if len(set(text_ids)) != len(text_ids):
            raise _validation(
                "human text assertions must have unique identities",
                code="correction_annotation_source_mismatch",
                field_name="human_text_assertions",
            )
        object.__setattr__(self, "human_text_assertions", text)

    @property
    def source_sha256(self) -> str:
        return self.artifact.content_sha256

    @property
    def dependent_revision_pins(self) -> dict[str, Any]:
        """Return the assertion state an atomic publication must compare.

        Raster bytes and the artifact aggregate have command-level pins. Spatial
        annotations and reviewed text are separate aggregates, so their
        identities and revisions must also remain stable from render through
        publication. Fresh containers keep this frozen snapshot free of mutable
        public fields while giving persistence adapters a JSON-ready CAS input.
        """

        return {
            "spatial_annotations": [
                {
                    "annotation_id": value.key.annotation_id,
                    "revision": value.revision,
                }
                for value in sorted(
                    self.annotations,
                    key=lambda annotation: annotation.key.annotation_id,
                )
            ],
            "human_text_assertions": [
                {
                    "assertion_id": value.assertion_id,
                    "revision": value.revision,
                }
                for value in sorted(
                    self.human_text_assertions,
                    key=lambda assertion: assertion.assertion_id,
                )
            ],
        }


@dataclass(frozen=True, slots=True)
class PreservedSpatialAssertions:
    annotation_id: str
    annotation_revision: str
    roles: tuple[SpatialRoleAssignment, ...] = ()
    captions: tuple[CaptionAssertion, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "annotation_revision": self.annotation_revision,
            "roles": [value.as_dict() for value in self.roles],
            "captions": [value.as_dict() for value in self.captions],
        }


@dataclass(frozen=True, slots=True)
class CorrectionHumanAssertions:
    """Human-owned state that a transform or OCR proposal cannot replace."""

    artifact_categories: tuple[CategoryAssignment, ...] = ()
    artifact_captions: tuple[CaptionAssertion, ...] = ()
    spatial: tuple[PreservedSpatialAssertions, ...] = ()
    text: tuple[HumanTextAssertion, ...] = ()

    @classmethod
    def from_source(cls, source: CorrectionSourceSnapshot) -> "CorrectionHumanAssertions":
        spatial: list[PreservedSpatialAssertions] = []
        for annotation in source.annotations:
            roles = tuple(
                value
                for value in annotation.role_assignments
                if value.origin is not RoleAssignmentOrigin.MACHINE
            )
            captions = tuple(
                value
                for value in annotation.caption_assertions
                if value.origin is not CaptionOrigin.MACHINE
            )
            if roles or captions:
                spatial.append(
                    PreservedSpatialAssertions(
                        annotation.key.annotation_id,
                        annotation.revision,
                        roles,
                        captions,
                    )
                )
        return cls(
            artifact_categories=tuple(
                value
                for value in source.artifact.category_assignments
                if value.origin is not AssignmentOrigin.SUGGESTED
            ),
            artifact_captions=tuple(
                value
                for value in source.artifact.caption_assertions
                if value.origin is not CaptionOrigin.MACHINE
            ),
            spatial=tuple(spatial),
            text=source.human_text_assertions,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_categories": [value.as_dict() for value in self.artifact_categories],
            "artifact_captions": [value.as_dict() for value in self.artifact_captions],
            "spatial": [value.as_dict() for value in self.spatial],
            "text": [value.as_dict() for value in self.text],
        }


@dataclass(frozen=True, slots=True)
class MappedSpatialAnnotationDraft:
    annotation_id: str
    source_revision: str
    points: tuple[NormalizedPoint, ...]
    order: int
    label: str
    role_assignments: tuple[SpatialRoleAssignment, ...]
    caption_assertions: tuple[CaptionAssertion, ...]
    linked_artifact_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "source_revision": self.source_revision,
            "coordinate_space": "corrected-output-normalized",
            "points": [point.as_dict() for point in self.points],
            "order": self.order,
            "label": self.label,
            "role_assignments": [value.as_dict() for value in self.role_assignments],
            "caption_assertions": [value.as_dict() for value in self.caption_assertions],
            "linked_artifact_ids": list(self.linked_artifact_ids),
        }


@dataclass(frozen=True, slots=True)
class CorrectionOutputDraft:
    kind: str
    media_type: str
    content: bytes
    dimensions: RasterDimensions | None
    provenance: ArtifactProvenance
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if self.kind not in CORRECTION_OUTPUT_KINDS:
            raise ValueError(f"unsupported correction output kind: {self.kind}")
        if not isinstance(self.media_type, str) or "/" not in self.media_type:
            raise ValueError("media_type must be a MIME type")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("output content must be non-empty immutable bytes")
        if self.media_type.startswith("image/") != isinstance(
            self.dimensions,
            RasterDimensions,
        ):
            raise ValueError("raster output dimensions must agree with media_type")
        if not isinstance(self.provenance, ArtifactProvenance):
            raise TypeError("provenance must be ArtifactProvenance")
        object.__setattr__(
            self,
            "content_sha256",
            hashlib.sha256(self.content).hexdigest(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "bytes": len(self.content),
            "dimensions": self.dimensions.as_dict() if self.dimensions else None,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CorrectionTransformCommitDraft:
    command: CorrectionTransformCommand
    source: CorrectionSourceSnapshot
    outputs: tuple[CorrectionOutputDraft, ...]
    mapped_annotations: tuple[MappedSpatialAnnotationDraft, ...]
    dropped_annotation_ids: tuple[str, ...]
    human_assertions: CorrectionHumanAssertions

    def __post_init__(self) -> None:
        if not isinstance(self.command, CorrectionTransformCommand):
            raise TypeError("commit draft command must be CorrectionTransformCommand")
        if not isinstance(self.source, CorrectionSourceSnapshot):
            raise TypeError("commit draft source must be CorrectionSourceSnapshot")
        outputs = tuple(self.outputs)
        if any(not isinstance(value, CorrectionOutputDraft) for value in outputs):
            raise TypeError("commit draft outputs must contain CorrectionOutputDraft values")
        object.__setattr__(self, "outputs", outputs)
        mapped_annotations = tuple(self.mapped_annotations)
        if any(
            not isinstance(value, MappedSpatialAnnotationDraft)
            for value in mapped_annotations
        ):
            raise TypeError(
                "mapped_annotations must contain MappedSpatialAnnotationDraft values"
            )
        object.__setattr__(self, "mapped_annotations", mapped_annotations)
        dropped_annotation_ids = tuple(self.dropped_annotation_ids)
        if any(not isinstance(value, str) for value in dropped_annotation_ids):
            raise TypeError("dropped_annotation_ids must contain strings")
        object.__setattr__(
            self,
            "dropped_annotation_ids",
            dropped_annotation_ids,
        )
        if not isinstance(self.human_assertions, CorrectionHumanAssertions):
            raise TypeError("human_assertions must be CorrectionHumanAssertions")
        kinds = tuple(value.kind for value in outputs)
        if len(kinds) != len(CORRECTION_OUTPUT_KINDS) or set(kinds) != set(
            CORRECTION_OUTPUT_KINDS
        ):
            raise ValueError("commit draft must contain each correction output exactly once")

    def output(self, kind: str) -> CorrectionOutputDraft:
        return next(value for value in self.outputs if value.kind == kind)


@dataclass(frozen=True, slots=True)
class CommittedCorrectionOutput:
    kind: str
    artifact_id: str
    artifact_revision: str
    content_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in CORRECTION_OUTPUT_KINDS:
            raise ValueError(f"unsupported correction output kind: {self.kind}")
        object.__setattr__(
            self,
            "artifact_id",
            _identifier(self.artifact_id, "artifact_id"),
        )
        object.__setattr__(
            self,
            "artifact_revision",
            _revision(self.artifact_revision, "artifact_revision"),
        )
        object.__setattr__(
            self,
            "content_sha256",
            _sha256(self.content_sha256, "content_sha256"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "artifact_revision": self.artifact_revision,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class CorrectionTransformCommitResult:
    operation_id: str
    outputs: tuple[CommittedCorrectionOutput, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )
        outputs = tuple(self.outputs)
        if any(not isinstance(value, CommittedCorrectionOutput) for value in outputs):
            raise TypeError(
                "commit result outputs must contain CommittedCorrectionOutput values"
            )
        object.__setattr__(self, "outputs", outputs)
        kinds = tuple(value.kind for value in outputs)
        if len(kinds) != len(CORRECTION_OUTPUT_KINDS) or set(kinds) != set(
            CORRECTION_OUTPUT_KINDS
        ):
            raise ValueError("commit result must contain each correction output exactly once")

    def output(self, kind: str) -> CommittedCorrectionOutput:
        return next(value for value in self.outputs if value.kind == kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "outputs": [value.as_dict() for value in self.outputs],
        }


@runtime_checkable
class CorrectionTransformStorePort(Protocol):
    """Source reader and atomic compare-and-swap output publisher.

    ``commit_transform`` must compare artifact revision, source revision, and
    checksum from ``draft.command``, plus
    ``draft.source.dependent_revision_pins``, in the same transaction that
    publishes the outputs. Every output must have a new, distinct artifact
    identity; the source artifact is never updated in place. Replaying an
    operation ID with the same command must return the original result; reusing
    it for a different command must conflict.
    """

    def load_source(self, key: RasterArtifactKey) -> CorrectionSourceSnapshot: ...

    def commit_transform(
        self,
        draft: CorrectionTransformCommitDraft,
    ) -> CorrectionTransformCommitResult: ...


@runtime_checkable
class CorrectionTransformHooksPort(Protocol):
    def is_cancelled(self) -> bool: ...

    def report_progress(self, progress: JobProgress) -> None: ...


class OcrFollowupState(str, Enum):
    NOT_REQUESTED = "not_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class OcrFollowupRequest:
    operation_id: str
    item_id: str
    source: CommittedCorrectionOutput

    def __post_init__(self) -> None:
        if self.source.kind != "ocr-ready":
            raise ValueError("OCR follow-up source must be the committed OCR-ready output")

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "item_id": self.item_id,
            "source": self.source.as_dict(),
            "publication_policy": "machine-proposal-only",
        }


@dataclass(frozen=True, slots=True)
class OcrFollowupOutcome:
    state: OcrFollowupState | str
    source: CommittedCorrectionOutput | None = None
    proposal_ref: str = ""
    failure: JobFailure | None = None

    def __post_init__(self) -> None:
        try:
            state = OcrFollowupState(self.state)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid OCR follow-up state") from exc
        object.__setattr__(self, "state", state)
        if state is OcrFollowupState.NOT_REQUESTED:
            if self.source is not None or self.proposal_ref or self.failure is not None:
                raise ValueError("not-requested OCR outcome cannot contain result data")
        else:
            if not isinstance(self.source, CommittedCorrectionOutput):
                raise ValueError("OCR outcome must pin its committed OCR-ready source")
            if self.source.kind != "ocr-ready":
                raise ValueError("OCR outcome source must be OCR-ready")
        if state is OcrFollowupState.SUCCEEDED:
            _identifier(self.proposal_ref, "proposal_ref")
            if self.failure is not None:
                raise ValueError("successful OCR outcome cannot contain a failure")
        elif self.proposal_ref:
            raise ValueError("only successful OCR outcomes may contain a proposal reference")
        if state is OcrFollowupState.FAILED and not isinstance(self.failure, JobFailure):
            raise ValueError("failed OCR outcome requires JobFailure")
        if state is not OcrFollowupState.FAILED and self.failure is not None:
            raise ValueError("only failed OCR outcomes may contain JobFailure")

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "source": self.source.as_dict() if self.source else None,
            "proposal_ref": self.proposal_ref,
            "failure": self.failure.as_dict() if self.failure else None,
        }


@runtime_checkable
class OcrFollowupPort(Protocol):
    """Produce a machine proposal; never write canonical text or assertions."""

    def run_ocr_followup(
        self,
        request: OcrFollowupRequest,
        hooks: CorrectionTransformHooksPort,
    ) -> OcrFollowupOutcome: ...


@runtime_checkable
class OcrFollowupOutcomePort(Protocol):
    """Persist the OCR outcome separately from the committed image."""

    def record_ocr_followup(
        self,
        operation_id: str,
        outcome: OcrFollowupOutcome,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class QueuedCorrectionTransform:
    job: JobView
    command_sha256: str
    created: bool

    @property
    def job_id(self) -> str:
        return self.job.job_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.as_dict(),
            "command_sha256": self.command_sha256,
            "created": self.created,
        }


class CorrectionTransformService:
    """Register exactly one logical JobManager job per operation ID."""

    def __init__(self, jobs: JobManager) -> None:
        self._jobs = jobs
        self._lock = threading.Lock()

    @staticmethod
    def job_id_for(operation_id: str) -> str:
        operation_id = _identifier(operation_id, "operation_id")
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
        return f"correction-transform-{digest}"

    def queue(self, command: CorrectionTransformCommand) -> QueuedCorrectionTransform:
        if not isinstance(command, CorrectionTransformCommand):
            raise TypeError("command must be CorrectionTransformCommand")
        job_id = self.job_id_for(command.operation_id)
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None:
                return self._replayed(existing, command)
            record: MutableMapping[str, Any] = {
                "id": job_id,
                "kind": CORRECTION_TRANSFORM_JOB_KIND,
                "status": "queued",
                "subject": {
                    "item_id": command.item_id,
                    "source_id": command.artifact_id,
                },
                "total": 6,
                "progress": JobProgress(0, 6, "phase", "queued").as_dict(),
                "input_revisions": {
                    "artifact_id": command.artifact_id,
                    "artifact_revision": command.artifact_revision,
                    "source_revision": command.source_revision,
                    "source_sha256": command.source_sha256,
                    "operation_id": command.operation_id,
                    "command_sha256": command.fingerprint,
                },
                # Private worker input. JobManager's public projection does not
                # persist this field; a durable command adapter remains an
                # integration responsibility.
                "command": command.as_dict(),
            }
            try:
                self._jobs.track(record, CORRECTION_TRANSFORM_JOB_KIND)
                created = True
            except ConflictError:
                winner = self._jobs.get(job_id)
                if winner is None:
                    raise
                return self._replayed(winner, command)
            view = self._jobs.view(job_id)
            if view is None:  # pragma: no cover - JobManager invariant
                raise RuntimeError("tracked correction job is unavailable")
            return QueuedCorrectionTransform(view, command.fingerprint, created)

    @staticmethod
    def _replayed(
        existing: Mapping[str, Any],
        command: CorrectionTransformCommand,
    ) -> QueuedCorrectionTransform:
        inputs = existing.get("input_revisions")
        fingerprint = inputs.get("command_sha256") if isinstance(inputs, Mapping) else None
        operation_id = inputs.get("operation_id") if isinstance(inputs, Mapping) else None
        subject = existing.get("subject")
        item_id = subject.get("item_id") if isinstance(subject, Mapping) else None
        source_id = subject.get("source_id") if isinstance(subject, Mapping) else None
        if (
            existing.get("kind") != CORRECTION_TRANSFORM_JOB_KIND
            or fingerprint != command.fingerprint
            or operation_id != command.operation_id
            or item_id != command.item_id
            or source_id != command.artifact_id
        ):
            raise ConflictError(
                "operation ID is already bound to a different correction command",
                code="correction_operation_conflict",
                details={"operation_id": command.operation_id},
            )
        return QueuedCorrectionTransform(
            JobManager.view_of(existing),
            command.fingerprint,
            False,
        )


class CorrectionTransformCancelled(RuntimeError):
    """Cooperative cancellation signal used by worker/provider adapters."""


class _JobManagerHooks:
    def __init__(self, jobs: JobManager, record: MutableMapping[str, Any]) -> None:
        self._jobs = jobs
        self._record = record

    def is_cancelled(self) -> bool:
        return self._jobs.is_cancelled(self._record)

    def report_progress(self, progress: JobProgress) -> None:
        with self._jobs.lock:
            status = "cancelling" if self.is_cancelled() else "running"
            self._jobs.transition_locked(
                self._record,
                status,
                done=progress.completed,
                total=progress.total,
                progress=progress.as_dict(),
                note=progress.phase,
            )


class _CombinedHooks:
    def __init__(
        self,
        primary: CorrectionTransformHooksPort,
        observer: CorrectionTransformHooksPort | None,
    ) -> None:
        self._primary = primary
        self._observer = observer

    def is_cancelled(self) -> bool:
        return self._primary.is_cancelled() or bool(
            self._observer and self._observer.is_cancelled()
        )

    def report_progress(self, progress: JobProgress) -> None:
        self._primary.report_progress(progress)
        if self._observer is not None:
            self._observer.report_progress(progress)


@dataclass(frozen=True, slots=True)
class CorrectionTransformRunResult:
    job_id: str
    operation_id: str
    image_commit: CorrectionTransformCommitResult | None
    ocr_followup: OcrFollowupOutcome
    cancelled_before_commit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "operation_id": self.operation_id,
            "image_commit": self.image_commit.as_dict() if self.image_commit else None,
            "ocr_followup": self.ocr_followup.as_dict(),
            "cancelled_before_commit": self.cancelled_before_commit,
        }


def _ensure_source_pin(
    command: CorrectionTransformCommand,
    source: CorrectionSourceSnapshot,
) -> None:
    actual = {
        "item_id": source.artifact.key.item_id,
        "artifact_id": source.artifact.key.artifact_id,
        "artifact_revision": source.artifact.revision,
        "source_revision": source.source_revision,
        "source_sha256": source.source_sha256,
    }
    expected = {
        "item_id": command.item_id,
        "artifact_id": command.artifact_id,
        "artifact_revision": command.artifact_revision,
        "source_revision": command.source_revision,
        "source_sha256": command.source_sha256,
    }
    if actual != expected:
        raise ConflictError(
            "correction source changed before commit",
            code="correction_source_stale",
            details={"expected": expected, "actual": actual},
        )


def _ensure_dependent_revision_pins(
    expected: CorrectionSourceSnapshot,
    actual: CorrectionSourceSnapshot,
) -> None:
    expected_pins = expected.dependent_revision_pins
    actual_pins = actual.dependent_revision_pins
    if actual_pins != expected_pins:
        raise ConflictError(
            "correction assertions changed before commit",
            code="correction_assertions_stale",
            details={"expected": expected_pins, "actual": actual_pins},
        )


def _map_point(matrix: Sequence[float], point: NormalizedPoint) -> tuple[float, float]:
    denominator = matrix[6] * point.x + matrix[7] * point.y + matrix[8]
    if not math.isfinite(denominator) or abs(denominator) <= 1e-12:
        raise ValueError("annotation intersects an invalid homography horizon")
    x = (matrix[0] * point.x + matrix[1] * point.y + matrix[2]) / denominator
    y = (matrix[3] * point.x + matrix[4] * point.y + matrix[5]) / denominator
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("annotation mapping produced a non-finite coordinate")
    return x, y


def _clip_polygon(points: Sequence[tuple[float, float]]) -> tuple[NormalizedPoint, ...]:
    work = list(points)

    def clip(
        values: list[tuple[float, float]],
        *,
        axis: int,
        bound: float,
        keep_greater: bool,
    ) -> list[tuple[float, float]]:
        if not values:
            return []

        def inside(point: tuple[float, float]) -> bool:
            return point[axis] >= bound if keep_greater else point[axis] <= bound

        def intersection(
            start: tuple[float, float],
            end: tuple[float, float],
        ) -> tuple[float, float]:
            delta = end[axis] - start[axis]
            if abs(delta) <= 1e-15:
                return (bound, start[1]) if axis == 0 else (start[0], bound)
            ratio = (bound - start[axis]) / delta
            other_axis = 1 - axis
            other = start[other_axis] + ratio * (end[other_axis] - start[other_axis])
            return (bound, other) if axis == 0 else (other, bound)

        output: list[tuple[float, float]] = []
        previous = values[-1]
        previous_inside = inside(previous)
        for current in values:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    output.append(intersection(previous, current))
                output.append(current)
            elif previous_inside:
                output.append(intersection(previous, current))
            previous, previous_inside = current, current_inside
        return output

    for axis, bound, keep_greater in (
        (0, 0.0, True),
        (0, 1.0, False),
        (1, 0.0, True),
        (1, 1.0, False),
    ):
        work = clip(work, axis=axis, bound=bound, keep_greater=keep_greater)

    deduplicated: list[tuple[float, float]] = []
    for x, y in work:
        point = (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))
        if not deduplicated or math.dist(point, deduplicated[-1]) > 1e-12:
            deduplicated.append(point)
    if len(deduplicated) > 1 and math.dist(deduplicated[0], deduplicated[-1]) <= 1e-12:
        deduplicated.pop()
    if len(deduplicated) < 3:
        return ()
    area_twice = sum(
        point[0] * deduplicated[(index + 1) % len(deduplicated)][1]
        - deduplicated[(index + 1) % len(deduplicated)][0] * point[1]
        for index, point in enumerate(deduplicated)
    )
    if abs(area_twice) <= 1e-12:
        return ()
    return tuple(NormalizedPoint(x, y) for x, y in deduplicated)


def _map_annotations(
    source: CorrectionSourceSnapshot,
    homography: Sequence[float],
) -> tuple[tuple[MappedSpatialAnnotationDraft, ...], tuple[str, ...]]:
    mapped: list[MappedSpatialAnnotationDraft] = []
    dropped: list[str] = []
    source_canvas_id = source.artifact.source.canvas_id
    source_canvas_revision = source.artifact.source.canvas_revision
    for annotation in source.annotations:
        source_ref = source.artifact.source
        annotation_ref = annotation.source
        if (
            not source_canvas_revision
            or annotation_ref.representation_id != source_ref.representation_id
            or annotation_ref.representation_revision != source_ref.representation_revision
            or annotation_ref.canvas_id != source_canvas_id
            or annotation_ref.canvas_revision != source_canvas_revision
            or annotation.selector.coordinate_space_revision != source_canvas_revision
        ):
            raise ConflictError(
                "annotation coordinate space does not match the correction source",
                code="correction_annotation_source_mismatch",
                details={"annotation_id": annotation.key.annotation_id},
            )
        points = _clip_polygon(
            tuple(_map_point(homography, point) for point in annotation.selector.points)
        )
        if not points:
            dropped.append(annotation.key.annotation_id)
            continue
        mapped.append(
            MappedSpatialAnnotationDraft(
                annotation_id=annotation.key.annotation_id,
                source_revision=annotation.revision,
                points=points,
                order=annotation.order,
                label=annotation.label,
                role_assignments=annotation.role_assignments,
                caption_assertions=annotation.caption_assertions,
                linked_artifact_ids=annotation.linked_artifact_ids,
            )
        )
    return tuple(mapped), tuple(dropped)


def _thumbnail_png(content: bytes, *, maximum_edge: int) -> tuple[bytes, RasterDimensions]:
    with Image.open(io.BytesIO(content)) as opened:
        image = opened.copy()
    image.thumbnail((maximum_edge, maximum_edge), Image.Resampling.LANCZOS)
    image.info.clear()
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue(), RasterDimensions(image.width, image.height)


def _build_commit_draft(
    command: CorrectionTransformCommand,
    source: CorrectionSourceSnapshot,
    *,
    thumbnail_max_edge: int,
) -> CorrectionTransformCommitDraft:
    transformed = apply_perspective_transform(
        source.content,
        command.quad,
        source_revision=command.source_revision,
        adjustment=command.adjustment,
    )
    mapped, dropped = _map_annotations(
        source,
        transformed.source_to_output_homography,
    )
    human = CorrectionHumanAssertions.from_source(source)
    provenance = ArtifactProvenance(
        origin="transform",
        recipe_revision="correction-transform-v1",
        operation_id=command.operation_id,
    )
    display_dimensions = RasterDimensions(
        transformed.output_width,
        transformed.output_height,
    )
    thumbnail, thumbnail_dimensions = _thumbnail_png(
        transformed.output_png,
        maximum_edge=thumbnail_max_edge,
    )
    display = CorrectionOutputDraft(
        "corrected-display",
        "image/png",
        transformed.output_png,
        display_dimensions,
        provenance,
    )
    ocr_ready = CorrectionOutputDraft(
        "ocr-ready",
        "image/png",
        transformed.output_png,
        display_dimensions,
        provenance,
    )
    thumbnail_draft = CorrectionOutputDraft(
        "thumbnail",
        "image/png",
        thumbnail,
        thumbnail_dimensions,
        provenance,
    )
    correction_manifest = {
        "schema": "org.whl.correction-transform",
        "version": 1,
        "operation_id": command.operation_id,
        "command": command.as_dict(),
        "dependent_revision_pins": source.dependent_revision_pins,
        "raster_transform": transformed.transform_manifest,
        "outputs": [
            display.as_dict(),
            ocr_ready.as_dict(),
            thumbnail_draft.as_dict(),
        ],
        "annotation_mapping": {
            "mapped": len(mapped),
            "dropped_annotation_ids": list(dropped),
            "policy": "projective-map-and-unit-square-clip",
        },
        "human_assertions": {
            "artifact_categories": len(human.artifact_categories),
            "artifact_captions": len(human.artifact_captions),
            "spatial": len(human.spatial),
            "text": len(human.text),
            "policy": "carry-separately-never-overwrite",
        },
        "rerun_ocr": command.rerun_ocr,
        "ocr_publication_policy": "machine-proposal-only",
    }
    manifest = CorrectionOutputDraft(
        "transform-manifest",
        "application/json",
        _canonical_json(correction_manifest),
        None,
        provenance,
    )
    return CorrectionTransformCommitDraft(
        command,
        source,
        (display, ocr_ready, thumbnail_draft, manifest),
        mapped,
        dropped,
        human,
    )


class CorrectionTransformWorker:
    """Execute a queued transform and publish OCR only as a follow-up outcome."""

    def __init__(
        self,
        jobs: JobManager,
        store: CorrectionTransformStorePort,
        *,
        ocr: OcrFollowupPort | None = None,
        ocr_outcomes: OcrFollowupOutcomePort | None = None,
        thumbnail_max_edge: int = 512,
    ) -> None:
        if not isinstance(thumbnail_max_edge, int) or thumbnail_max_edge < 16:
            raise ValueError("thumbnail_max_edge must be an integer of at least 16")
        self._jobs = jobs
        self._store = store
        self._ocr = ocr
        self._ocr_outcomes = ocr_outcomes
        self._thumbnail_max_edge = thumbnail_max_edge

    def run(
        self,
        command: CorrectionTransformCommand,
        *,
        hooks: CorrectionTransformHooksPort | None = None,
    ) -> CorrectionTransformRunResult:
        job_id = CorrectionTransformService.job_id_for(command.operation_id)
        with self._jobs.lock:
            record = self._jobs.records.get(job_id)
            if record is None:
                raise ValidationError(
                    "correction transform job is not queued",
                    code="correction_job_not_found",
                    details={"job_id": job_id},
                )
            state = str(record.get("state") or record.get("status") or "")
            if record.get("_correction_worker_claimed") or state not in {
                "queued",
                "cancelling",
            }:
                raise ConflictError(
                    "correction transform job is already claimed or terminal",
                    code="correction_job_already_claimed",
                    details={"job_id": job_id, "state": state},
                )
            input_revisions = record.get("input_revisions")
            expected_fingerprint = (
                input_revisions.get("command_sha256")
                if isinstance(input_revisions, Mapping)
                else None
            )
            if expected_fingerprint != command.fingerprint:
                raise ConflictError(
                    "queued job does not match the supplied correction command",
                    code="correction_operation_conflict",
                    details={"operation_id": command.operation_id},
                )
            record["_correction_worker_claimed"] = True
        combined_hooks = _CombinedHooks(_JobManagerHooks(self._jobs, record), hooks)
        if combined_hooks.is_cancelled():
            return self._cancel_before_commit(command, job_id, record)
        with self._jobs.lock:
            cancelled_at_start = self._jobs.is_cancelled(record)
            if not cancelled_at_start:
                self._jobs.transition_locked(record, "running")
        if cancelled_at_start:
            return self._cancel_before_commit(command, job_id, record)

        try:
            combined_hooks.report_progress(JobProgress(1, 6, "phase", "validating-source"))
            source = self._store.load_source(command.key)
            _ensure_source_pin(command, source)
            input_revisions = dict(record.get("input_revisions") or {})
            input_revisions["dependent_assertions"] = (
                source.dependent_revision_pins
            )
            with self._jobs.lock:
                status = (
                    "cancelling"
                    if self._jobs.is_cancelled(record)
                    else "running"
                )
                self._jobs.transition_locked(
                    record,
                    status,
                    input_revisions=input_revisions,
                )
            if combined_hooks.is_cancelled():
                raise CorrectionTransformCancelled

            combined_hooks.report_progress(JobProgress(2, 6, "phase", "transforming"))
            draft = _build_commit_draft(
                command,
                source,
                thumbnail_max_edge=self._thumbnail_max_edge,
            )
            if combined_hooks.is_cancelled():
                raise CorrectionTransformCancelled

            combined_hooks.report_progress(JobProgress(3, 6, "phase", "revalidating-source"))
            latest = self._store.load_source(command.key)
            _ensure_source_pin(command, latest)
            _ensure_dependent_revision_pins(source, latest)
            if combined_hooks.is_cancelled():
                raise CorrectionTransformCancelled

            combined_hooks.report_progress(JobProgress(4, 6, "phase", "committing-image"))
            # The port must repeat this CAS atomically with publication; the
            # reload above supplies an early, user-readable conflict.
            commit = self._store.commit_transform(draft)
            self._validate_commit_result(command, draft, commit)
        except CorrectionTransformCancelled:
            return self._cancel_before_commit(command, job_id, record)
        except Exception as exc:
            self._fail_job(record, exc)
            raise

        job_outputs = [
            JobOutput(value.kind, value.artifact_id).as_dict()
            for value in commit.outputs
        ]
        self._jobs.transition(
            record,
            "running",
            outputs=job_outputs,
            progress=JobProgress(5, 6, "phase", "image-committed").as_dict(),
            done=5,
            total=6,
            note="image committed",
        )

        outcome = self._run_ocr_followup(command, commit, combined_hooks)
        recorder_failure: Exception | None = None
        if self._ocr_outcomes is not None:
            try:
                self._ocr_outcomes.record_ocr_followup(command.operation_id, outcome)
            except Exception as exc:  # the image commit is intentionally final
                recorder_failure = exc

        if outcome.state is OcrFollowupState.SUCCEEDED:
            job_outputs.append(JobOutput("ocr-proposal", outcome.proposal_ref).as_dict())
        has_followup_error = outcome.state in {
            OcrFollowupState.FAILED,
            OcrFollowupState.CANCELLED,
        } or recorder_failure is not None
        note = "correction complete"
        if outcome.state is OcrFollowupState.FAILED:
            note = "image committed; OCR follow-up failed"
        elif outcome.state is OcrFollowupState.CANCELLED:
            note = "image committed; OCR follow-up cancelled"
        if recorder_failure is not None:
            note = "image committed; OCR outcome recording failed"
        self._jobs.transition(
            record,
            "done (with errors)" if has_followup_error else "done",
            outputs=job_outputs,
            progress=JobProgress(6, 6, "phase", "complete").as_dict(),
            done=6,
            total=6,
            errors=1 if has_followup_error else 0,
            note=note,
        )
        return CorrectionTransformRunResult(
            job_id,
            command.operation_id,
            commit,
            outcome,
        )

    def _run_ocr_followup(
        self,
        command: CorrectionTransformCommand,
        commit: CorrectionTransformCommitResult,
        hooks: CorrectionTransformHooksPort,
    ) -> OcrFollowupOutcome:
        if not command.rerun_ocr:
            return OcrFollowupOutcome(OcrFollowupState.NOT_REQUESTED)
        source = commit.output("ocr-ready")
        if hooks.is_cancelled():
            return OcrFollowupOutcome(OcrFollowupState.CANCELLED, source=source)
        try:
            hooks.report_progress(JobProgress(5, 6, "phase", "ocr-follow-up"))
            if self._ocr is None:
                return OcrFollowupOutcome(
                    OcrFollowupState.FAILED,
                    source=source,
                    failure=JobFailure(
                        "ocr_followup_unavailable",
                        "OCR follow-up provider is unavailable",
                        retryable=True,
                    ),
                )
            outcome = self._ocr.run_ocr_followup(
                OcrFollowupRequest(command.operation_id, command.item_id, source),
                hooks,
            )
            if not isinstance(outcome, OcrFollowupOutcome):
                raise TypeError("OCR follow-up port returned an invalid outcome")
            if outcome.state is OcrFollowupState.NOT_REQUESTED:
                raise ValueError("requested OCR follow-up cannot return not_requested")
            if outcome.source != source:
                raise ValueError("OCR follow-up outcome does not pin the requested rendition")
            return outcome
        except CorrectionTransformCancelled:
            return OcrFollowupOutcome(OcrFollowupState.CANCELLED, source=source)
        except Exception as exc:
            return OcrFollowupOutcome(
                OcrFollowupState.FAILED,
                source=source,
                failure=JobFailure(
                    "ocr_followup_failed",
                    str(exc) or type(exc).__name__,
                    retryable=True,
                    details={"exception": type(exc).__name__},
                ),
            )

    @staticmethod
    def _validate_commit_result(
        command: CorrectionTransformCommand,
        draft: CorrectionTransformCommitDraft,
        commit: CorrectionTransformCommitResult,
    ) -> None:
        if not isinstance(commit, CorrectionTransformCommitResult):
            raise TypeError("correction store returned an invalid commit result")
        if commit.operation_id != command.operation_id:
            raise ConflictError(
                "correction commit operation does not match its command",
                code="correction_commit_mismatch",
            )
        committed_artifact_ids = tuple(
            output.artifact_id for output in commit.outputs
        )
        if (
            command.artifact_id in committed_artifact_ids
            or len(set(committed_artifact_ids)) != len(committed_artifact_ids)
        ):
            raise ConflictError(
                "correction outputs must use new, distinct artifact identities",
                code="correction_commit_mismatch",
                details={
                    "source_artifact_id": command.artifact_id,
                    "output_artifact_ids": list(committed_artifact_ids),
                },
            )
        for output in draft.outputs:
            committed = commit.output(output.kind)
            if committed.content_sha256 != output.content_sha256:
                raise ConflictError(
                    "correction commit checksum does not match its immutable draft",
                    code="correction_commit_mismatch",
                    details={"kind": output.kind},
                )

    def _cancel_before_commit(
        self,
        command: CorrectionTransformCommand,
        job_id: str,
        record: MutableMapping[str, Any],
    ) -> CorrectionTransformRunResult:
        self._jobs.transition(
            record,
            "cancelled",
            note="cancelled before image commit",
            outputs=[],
        )
        return CorrectionTransformRunResult(
            job_id,
            command.operation_id,
            None,
            OcrFollowupOutcome(OcrFollowupState.NOT_REQUESTED),
            cancelled_before_commit=True,
        )

    def _fail_job(self, record: MutableMapping[str, Any], exc: Exception) -> None:
        if hasattr(exc, "as_dict"):
            failure = exc.as_dict()  # type: ignore[union-attr]
        else:
            failure = {
                "code": "correction_transform_failed",
                "message": str(exc) or type(exc).__name__,
                "retryable": False,
                "details": {"exception": type(exc).__name__},
            }
        self._jobs.transition(
            record,
            "failed",
            error=str(failure.get("message") or "correction transform failed"),
            failure=failure,
            note="correction transform failed before image commit",
            outputs=[],
        )


__all__ = [
    "CORRECTION_OUTPUT_KINDS",
    "CORRECTION_TRANSFORM_JOB_KIND",
    "CommittedCorrectionOutput",
    "CorrectionHumanAssertions",
    "CorrectionOutputDraft",
    "CorrectionSourceSnapshot",
    "CorrectionTransformCancelled",
    "CorrectionTransformCommand",
    "CorrectionTransformCommitDraft",
    "CorrectionTransformCommitResult",
    "CorrectionTransformHooksPort",
    "CorrectionTransformRunResult",
    "CorrectionTransformService",
    "CorrectionTransformStorePort",
    "CorrectionTransformWorker",
    "HumanTextAssertion",
    "HumanTextOrigin",
    "MappedSpatialAnnotationDraft",
    "OcrFollowupOutcome",
    "OcrFollowupOutcomePort",
    "OcrFollowupPort",
    "OcrFollowupRequest",
    "OcrFollowupState",
    "PreservedSpatialAssertions",
    "QueuedCorrectionTransform",
]
