"""Versioned portable backup contracts for private book curation.

The bundle is deliberately a manifest plus members rather than a JSON cell
containing Markdown.  Filesystem/ZIP concerns live in the adapter; this module
owns the bounded, transport-neutral values the adapter validates before any
Desktop store is changed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal

from .errors import ConflictError, ValidationError
from .scan_assessments import ScanAssessmentKey, ScanAssessmentView


PORTABLE_BOOK_BUNDLE_VERSION = 1
PORTABLE_BOOK_BUNDLE_SCHEMA = (
    f"librarytool.portable-book-bundle/{PORTABLE_BOOK_BUNDLE_VERSION}"
)
PORTABLE_BOOK_BUNDLE_MANIFEST = "bundle.json"
PORTABLE_BOOK_BUNDLE_MEDIA_TYPE = "application/zip"
PORTABLE_BOOK_COPY_FIELDS = (
    "marked_price",
    "scan_priority",
    "scan_verdict",
)
PORTABLE_BOOK_SCAN_PRIORITIES = (
    "n/s (no scan)",
    "Low",
    "Medium",
    "High",
)
MAX_PORTABLE_BOOK_RECORDS = 10_000
MAX_PORTABLE_BOOK_METADATA_BYTES = 2 * 1024 * 1024
MAX_PORTABLE_BOOK_SOURCE_EVIDENCE_BYTES = 4096
MAX_PORTABLE_BOOK_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_PORTABLE_BOOK_JSON_DEPTH = 32
MAX_PORTABLE_BOOK_JSON_NODES = 100_000
MAX_PORTABLE_BOOK_STRING_CHARACTERS = 1_000_000

_RECORD_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,255}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$",
    re.ASCII,
)


class PortableBookBundleError(ValidationError):
    """The portable archive is malformed, unsupported, or unsafe."""

    default_code = "invalid_portable_book_bundle"


class PortableBookBundleConflict(ConflictError):
    """A pinned Desktop record or assessment no longer matches."""

    default_code = "portable_book_bundle_conflict"


def _invalid(message: str, *, field: str, code: str = "invalid_portable_book_bundle"):
    raise PortableBookBundleError(
        message,
        code=code,
        details={"field": field},
    )


def portable_book_canonical_json(value: Any) -> bytes:
    """Encode strict canonical UTF-8 JSON used by bundle hashes."""

    def plain(candidate: Any) -> Any:
        if isinstance(candidate, Mapping):
            return {name: plain(nested) for name, nested in candidate.items()}
        if isinstance(candidate, (list, tuple)):
            return [plain(nested) for nested in candidate]
        return candidate

    try:
        return json.dumps(
            plain(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PortableBookBundleError(
            "portable book metadata is not strict UTF-8 JSON",
            code="invalid_portable_book_json",
        ) from exc


def _json_clone(value: Any, *, field: str) -> Any:
    nodes = 0

    def inspect(candidate: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_PORTABLE_BOOK_JSON_NODES:
            _invalid("portable metadata contains too many values", field=field)
        if depth > MAX_PORTABLE_BOOK_JSON_DEPTH:
            _invalid("portable metadata is nested too deeply", field=field)
        if candidate is None or isinstance(candidate, bool):
            return
        if isinstance(candidate, int):
            if abs(candidate) > 9_007_199_254_740_991:
                _invalid("portable metadata integer is not interoperable", field=field)
            return
        if isinstance(candidate, float):
            # canonical encoding below rejects infinities/NaN.  Preserve normal
            # JSON numbers instead of coercing unknown extension data.
            return
        if isinstance(candidate, str):
            if len(candidate) > MAX_PORTABLE_BOOK_STRING_CHARACTERS:
                _invalid("portable metadata string is too long", field=field)
            if any(0xD800 <= ord(character) <= 0xDFFF for character in candidate):
                _invalid("portable metadata contains invalid Unicode", field=field)
            return
        if isinstance(candidate, Mapping):
            for name, nested in candidate.items():
                if not isinstance(name, str) or not name:
                    _invalid(
                        "portable metadata keys must be non-empty strings", field=field
                    )
                inspect(name, depth + 1)
                inspect(nested, depth + 1)
            return
        if isinstance(candidate, (list, tuple)):
            for nested in candidate:
                inspect(nested, depth + 1)
            return
        _invalid("portable metadata contains a non-JSON value", field=field)

    inspect(value, 0)
    encoded = portable_book_canonical_json(value)
    if len(encoded) > MAX_PORTABLE_BOOK_METADATA_BYTES:
        _invalid("portable book metadata exceeds its byte limit", field=field)
    return json.loads(encoded.decode("utf-8"))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {name: _freeze_json(nested) for name, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _bounded_token(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _RECORD_VERSION_RE.fullmatch(value):
        _invalid(
            f"{field} must be a portable non-empty version token",
            field=field,
        )
    return value


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        _invalid(f"{field} must be a lower-case SHA-256 digest", field=field)
    return value


def _timestamp(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not _RFC3339_RE.fullmatch(value)
    ):
        _invalid(f"{field} must be an RFC 3339 timestamp", field=field)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        _invalid(f"{field} must be an RFC 3339 timestamp", field=field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid(f"{field} must include a UTC offset", field=field)
    return value


def portable_copy_curation(metadata: Mapping[str, Any]) -> dict[str, str]:
    """Validate and copy only the three short private curation fields.

    Missing keys remain missing.  In particular, a missing/blank priority is
    never rewritten to the substantive ``n/s (no scan)`` value.
    """

    if not isinstance(metadata, Mapping):
        _invalid("book metadata must be an object", field="metadata")
    result: dict[str, str] = {}
    for field in PORTABLE_BOOK_COPY_FIELDS:
        if field not in metadata:
            continue
        value = metadata[field]
        if not isinstance(value, str):
            _invalid(f"{field} must be a string", field=field)
        if field == "marked_price":
            normalized = value.strip()
            if normalized != value:
                _invalid("marked_price must have outer whitespace trimmed", field=field)
        elif field == "scan_priority":
            normalized = value
            if normalized and normalized not in PORTABLE_BOOK_SCAN_PRIORITIES:
                _invalid("scan_priority is not an exact supported value", field=field)
        else:
            normalized = value.strip()
            if normalized != value:
                _invalid("scan_verdict must have outer whitespace trimmed", field=field)
            if any(character in _LINE_BREAKS for character in normalized):
                _invalid("scan_verdict must be a single line", field=field)
            if len(normalized) > 500:
                _invalid("scan_verdict exceeds 500 characters", field=field)
        result[field] = normalized
    return result


@dataclass(frozen=True, slots=True)
class PortableBookAuthority:
    """The exact active catalogue storage selected for one source reference."""

    storage_kind: Literal["manual_entries", "whl_builds", "ch_library"]
    storage_id: str
    canonical_item_id: str = ""
    capture_id: str = ""

    def __post_init__(self) -> None:
        if self.storage_kind not in {"manual_entries", "whl_builds", "ch_library"}:
            _invalid("portable authority storage kind is invalid", field="authority")
        _bounded_token(self.storage_id, field="authority.storage_id")
        for field_name in ("canonical_item_id", "capture_id"):
            value = getattr(self, field_name)
            if value:
                _bounded_token(value, field=f"authority.{field_name}")
            elif not isinstance(value, str):
                _invalid(
                    f"authority.{field_name} must be a string",
                    field=f"authority.{field_name}",
                )

    def as_dict(self) -> dict[str, str]:
        return {
            "storage_kind": self.storage_kind,
            "storage_id": self.storage_id,
            "canonical_item_id": self.canonical_item_id,
            "capture_id": self.capture_id,
        }


@dataclass(frozen=True, slots=True)
class PortableBookRecord:
    """One exact Desktop book snapshot and its optional Markdown artifact."""

    source: ScanAssessmentKey
    record_version: str
    source_sha256: str
    source_evidence: Mapping[str, Any]
    source_metadata: Mapping[str, Any]
    authority: PortableBookAuthority
    metadata: Mapping[str, Any]
    assessment: ScanAssessmentView | None = None
    book_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, ScanAssessmentKey):
            raise TypeError("source must be a ScanAssessmentKey")
        object.__setattr__(
            self,
            "record_version",
            _bounded_token(self.record_version, field="record_version"),
        )
        object.__setattr__(
            self,
            "source_sha256",
            _sha256(self.source_sha256, field="source_sha256"),
        )
        if self.book_id:
            _bounded_token(self.book_id, field="book_id")
        elif not isinstance(self.book_id, str):
            _invalid("book_id must be a string", field="book_id")
        cloned = _json_clone(self.metadata, field="metadata")
        if not isinstance(cloned, dict):
            _invalid("book metadata must be an object", field="metadata")
        portable_copy_curation(cloned)
        object.__setattr__(self, "metadata", _freeze_json(cloned))
        evidence = _json_clone(self.source_evidence, field="source_evidence")
        if not isinstance(evidence, dict):
            _invalid("source evidence must be an object", field="source_evidence")
        if (
            len(portable_book_canonical_json(evidence))
            > MAX_PORTABLE_BOOK_SOURCE_EVIDENCE_BYTES
        ):
            _invalid(
                "source evidence exceeds its byte limit",
                field="source_evidence",
            )
        object.__setattr__(self, "source_evidence", _freeze_json(evidence))
        source_metadata = _json_clone(self.source_metadata, field="source_metadata")
        if not isinstance(source_metadata, dict):
            _invalid("source metadata must be an object", field="source_metadata")
        object.__setattr__(self, "source_metadata", _freeze_json(source_metadata))
        if not isinstance(self.authority, PortableBookAuthority):
            raise TypeError("authority must be a PortableBookAuthority")
        if self.source.namespace == "manual_entries":
            expected_kinds = {"manual_entries", "whl_builds"}
        else:
            expected_kinds = {"ch_library"}
        if self.authority.storage_kind not in expected_kinds:
            _invalid(
                "portable authority does not match its source namespace",
                field="authority.storage_kind",
            )
        if self.assessment is not None:
            if not isinstance(self.assessment, ScanAssessmentView):
                raise TypeError("assessment must be a ScanAssessmentView or None")
            if self.assessment.key != self.source:
                _invalid(
                    "assessment identity does not match its book source",
                    field="assessment",
                    code="portable_assessment_identity_mismatch",
                )
            if (
                self.assessment.manifest.provenance.source_row_sha256
                != self.source_sha256
            ):
                _invalid(
                    "assessment provenance does not match its source hash",
                    field="assessment.provenance.source_row_sha256",
                    code="portable_assessment_source_mismatch",
                )
            if (
                self.assessment.manifest.canonical_item_id
                != self.authority.canonical_item_id
                or self.assessment.manifest.capture_id != self.authority.capture_id
            ):
                _invalid(
                    "assessment aliases do not match the resolved authority",
                    field="assessment",
                    code="portable_assessment_alias_mismatch",
                )

    @property
    def copy_curation(self) -> Mapping[str, str]:
        return MappingProxyType(portable_copy_curation(self.metadata))

    @property
    def metadata_sha256(self) -> str:
        return hashlib.sha256(portable_book_canonical_json(self.metadata)).hexdigest()


@dataclass(frozen=True, slots=True)
class PortableBookBundle:
    schema: str
    created_at: str
    records: tuple[PortableBookRecord, ...]
    archive_sha256: str = ""

    def __post_init__(self) -> None:
        if self.schema != PORTABLE_BOOK_BUNDLE_SCHEMA:
            _invalid(
                "portable book bundle schema is unsupported",
                field="schema",
                code="unsupported_portable_book_bundle_schema",
            )
        object.__setattr__(
            self,
            "created_at",
            _timestamp(self.created_at, field="created_at"),
        )
        if not isinstance(self.records, tuple):
            raise TypeError("records must be a tuple")
        if not self.records or len(self.records) > MAX_PORTABLE_BOOK_RECORDS:
            _invalid(
                "portable bundle record count is outside its limit", field="records"
            )
        seen: set[ScanAssessmentKey] = set()
        for record in self.records:
            if not isinstance(record, PortableBookRecord):
                raise TypeError("records must contain PortableBookRecord values")
            if record.source in seen:
                _invalid(
                    "portable bundle contains a duplicate source reference",
                    field="records",
                    code="duplicate_portable_book_source",
                )
            seen.add(record.source)
        if self.archive_sha256:
            _sha256(self.archive_sha256, field="archive_sha256")


PortableImportDisposition = Literal[
    "create", "update", "delete", "unchanged", "conflict"
]


@dataclass(frozen=True, slots=True)
class PortableImportPin:
    """Explicit current-target CAS values; ``None`` means must not exist."""

    record_version: str | None
    assessment_revision: str | None
    source_record_version: str | None = None

    def __post_init__(self) -> None:
        if self.record_version is not None:
            _bounded_token(self.record_version, field="record_version")
        if self.assessment_revision is not None:
            _bounded_token(self.assessment_revision, field="assessment_revision")
        if self.source_record_version is not None:
            _bounded_token(
                self.source_record_version,
                field="source_record_version",
            )

    def as_dict(self) -> dict[str, str | None]:
        return {
            "record_version": self.record_version,
            "assessment_revision": self.assessment_revision,
            "source_record_version": self.source_record_version,
        }


@dataclass(frozen=True, slots=True)
class PortableImportAction:
    source: ScanAssessmentKey
    metadata: PortableImportDisposition
    assessment: PortableImportDisposition
    current_record_version: str | None
    current_assessment_revision: str | None
    result_record_version: str | None = None
    result_assessment_revision: str | None = None
    assessment_sha256: str | None = None
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source, ScanAssessmentKey):
            raise TypeError("source must be a ScanAssessmentKey")
        dispositions = {"create", "update", "delete", "unchanged", "conflict"}
        if self.metadata not in dispositions or self.assessment not in dispositions:
            _invalid("portable import disposition is invalid", field="actions")
        if self.current_record_version is not None:
            _bounded_token(self.current_record_version, field="current_record_version")
        if self.current_assessment_revision is not None:
            _bounded_token(
                self.current_assessment_revision,
                field="current_assessment_revision",
            )
        if self.result_record_version is not None:
            _bounded_token(self.result_record_version, field="result_record_version")
        if self.result_assessment_revision is not None:
            _bounded_token(
                self.result_assessment_revision,
                field="result_assessment_revision",
            )
        if self.assessment_sha256 is not None:
            _sha256(self.assessment_sha256, field="assessment_sha256")
        if not isinstance(self.conflicts, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.conflicts
        ):
            _invalid("portable import conflicts are invalid", field="conflicts")

    @property
    def committable(self) -> bool:
        return not self.conflicts and "conflict" not in {
            self.metadata,
            self.assessment,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.as_dict(),
            "metadata": self.metadata,
            "assessment": self.assessment,
            "current_record_version": self.current_record_version,
            "current_assessment_revision": self.current_assessment_revision,
            "result_record_version": self.result_record_version,
            "result_assessment_revision": self.result_assessment_revision,
            "assessment_sha256": self.assessment_sha256,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True, slots=True)
class PortableBookImportPlan:
    bundle: PortableBookBundle
    pins: Mapping[ScanAssessmentKey, PortableImportPin]
    actions: tuple[PortableImportAction, ...]
    state_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.state_sha256, field="state_sha256")
        object.__setattr__(self, "pins", MappingProxyType(dict(self.pins)))

    @property
    def committable(self) -> bool:
        return all(action.committable for action in self.actions)

    @property
    def counts(self) -> Mapping[str, Mapping[str, int]]:
        names = ("create", "update", "delete", "unchanged", "conflict")
        counts = {
            "metadata": {name: 0 for name in names},
            "assessment": {name: 0 for name in names},
        }
        for action in self.actions:
            counts["metadata"][action.metadata] += 1
            counts["assessment"][action.assessment] += 1
        return MappingProxyType(
            {category: MappingProxyType(values) for category, values in counts.items()}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "librarytool.portable-book-import-plan/1",
            "bundle_sha256": self.bundle.archive_sha256,
            "state_sha256": self.state_sha256,
            "committable": self.committable,
            "counts": {
                category: dict(values) for category, values in self.counts.items()
            },
            "actions": [action.as_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class PortableBookImportReceipt:
    operation_sha256: str
    bundle_sha256: str
    state_sha256: str
    created_at: str
    actions: tuple[PortableImportAction, ...]
    replayed: bool = False

    def __post_init__(self) -> None:
        _sha256(self.operation_sha256, field="operation_sha256")
        _sha256(self.bundle_sha256, field="bundle_sha256")
        _sha256(self.state_sha256, field="state_sha256")
        _timestamp(self.created_at, field="created_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "librarytool.portable-book-import-receipt/1",
            "operation_sha256": self.operation_sha256,
            "bundle_sha256": self.bundle_sha256,
            "state_sha256": self.state_sha256,
            "created_at": self.created_at,
            "actions": [action.as_dict() for action in self.actions],
            "replayed": self.replayed,
        }


__all__ = [
    "MAX_PORTABLE_BOOK_BUNDLE_BYTES",
    "MAX_PORTABLE_BOOK_METADATA_BYTES",
    "MAX_PORTABLE_BOOK_RECORDS",
    "MAX_PORTABLE_BOOK_SOURCE_EVIDENCE_BYTES",
    "PORTABLE_BOOK_BUNDLE_MANIFEST",
    "PORTABLE_BOOK_BUNDLE_MEDIA_TYPE",
    "PORTABLE_BOOK_BUNDLE_SCHEMA",
    "PORTABLE_BOOK_BUNDLE_VERSION",
    "PORTABLE_BOOK_COPY_FIELDS",
    "PORTABLE_BOOK_SCAN_PRIORITIES",
    "PortableBookAuthority",
    "PortableBookBundle",
    "PortableBookBundleConflict",
    "PortableBookBundleError",
    "PortableBookImportPlan",
    "PortableBookImportReceipt",
    "PortableBookRecord",
    "PortableImportAction",
    "PortableImportPin",
    "portable_book_canonical_json",
    "portable_copy_curation",
]
