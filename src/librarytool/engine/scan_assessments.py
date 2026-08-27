"""Versioned source-reference contracts for scan-assessment reasoning.

Scan assessments are mutable, copy-curation artifacts.  Their identity is the
original catalogue source reference rather than a Corrections item id because
manual entries and checked-library rows do not necessarily have canonical
items.  The contract deliberately contains no storage locator: an adapter may
use the source reference to locate bytes, but paths are never engine data.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from .errors import (
    NotFoundError,
    RepositoryError,
    ValidationError,
)


SCAN_ASSESSMENT_CONTRACT_VERSION = 1
SCAN_ASSESSMENT_SCHEMA = (
    f"librarytool.scan-assessment/{SCAN_ASSESSMENT_CONTRACT_VERSION}"
)
SCAN_ASSESSMENT_ARTIFACT_ID = "scan-assessment"
SCAN_ASSESSMENT_MEDIA_TYPE = "text/markdown"

# Long reasoning is intentionally bounded independently from the short verdict
# field.  One MiB leaves ample room for reviewed Markdown while keeping every
# integrity check and API response predictably memory-bounded.
MAX_SCAN_ASSESSMENT_BYTES = 1024 * 1024
MAX_SCAN_ASSESSMENT_MANIFEST_BYTES = 64 * 1024
MAX_SCAN_ASSESSMENT_PROVENANCE_CHARACTERS = 512

_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", re.ASCII)
_SOURCE_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$",
    re.ASCII,
)
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$", re.ASCII)
_REVISION_RE = re.compile(r"^sa-[0-9a-f]{64}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_OPERATION_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    re.ASCII,
)
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$",
    re.ASCII,
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "namespace",
        "source_id",
        "artifact_id",
        "media_type",
        "content_sha256",
        "byte_size",
        "revision",
        "created_at",
        "updated_at",
        "provenance",
        "canonical_item_id",
        "capture_id",
    }
)
_PROVENANCE_FIELDS = frozenset(
    {
        "review_record_uuid",
        "source_database",
        "source_snapshot",
        "source_row_sha256",
    }
)


def _validation(message: str, *, code: str, field_name: str) -> ValidationError:
    return ValidationError(message, code=code, details={"field": field_name})


def _portable_source_segment(
    value: Any,
    *,
    field_name: str,
    pattern: re.Pattern[str],
) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _validation(
            f"{field_name} must be a portable source-reference segment",
            code="invalid_scan_assessment_identity",
            field_name=field_name,
        )
    # The grammar already excludes separators, percent escapes, whitespace,
    # NULs, drive prefixes, and a leading dot.  Keep the traversal checks
    # explicit so future grammar changes cannot accidentally admit them.
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise _validation(
            f"{field_name} must not be a path",
            code="invalid_scan_assessment_identity",
            field_name=field_name,
        )
    return value


def _sha256(value: Any, *, field_name: str, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a lower-case SHA-256 digest",
            code="invalid_scan_assessment_manifest",
            field_name=field_name,
        )
    return value


def _revision(value: Any, *, field_name: str = "revision") -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a scan-assessment revision",
            code="invalid_scan_assessment_revision",
            field_name=field_name,
        )
    return value


def _timestamp(value: Any, *, field_name: str) -> tuple[str, datetime]:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not _RFC3339_RE.fullmatch(value)
    ):
        raise _validation(
            f"{field_name} must be an RFC 3339 timestamp",
            code="invalid_scan_assessment_manifest",
            field_name=field_name,
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise _validation(
            f"{field_name} must be an RFC 3339 timestamp",
            code="invalid_scan_assessment_manifest",
            field_name=field_name,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _validation(
            f"{field_name} must include a UTC offset",
            code="invalid_scan_assessment_manifest",
            field_name=field_name,
        )
    return value, parsed


def _bounded_public_provenance(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise _validation(
            f"{field_name} must be a string",
            code="invalid_scan_assessment_provenance",
            field_name=field_name,
        )
    result = value.strip()
    if len(result) > MAX_SCAN_ASSESSMENT_PROVENANCE_CHARACTERS:
        raise _validation(
            f"{field_name} is too long",
            code="invalid_scan_assessment_provenance",
            field_name=field_name,
        )
    if any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in result
    ):
        raise _validation(
            f"{field_name} contains an unsafe character",
            code="invalid_scan_assessment_provenance",
            field_name=field_name,
        )
    # Provenance records logical database/snapshot names, not local locators.
    if (
        result in {".", ".."}
        or "/" in result
        or "\\" in result
        or re.match(r"^[A-Za-z]:", result)
        or result.casefold().startswith("file:")
    ):
        raise _validation(
            f"{field_name} must not expose a filesystem path",
            code="private_scan_assessment_locator",
            field_name=field_name,
        )
    return result


def validate_scan_assessment_revision(value: Any) -> str:
    """Validate one public optimistic-concurrency token."""

    return _revision(value, field_name="expected_revision")


def validate_scan_assessment_operation_id(value: Any) -> str:
    """Validate the required idempotency key used by a mutation."""

    if not isinstance(value, str) or not _OPERATION_ID_RE.fullmatch(value):
        raise _validation(
            "operation_id must be a portable non-empty idempotency key",
            code="invalid_scan_assessment_operation_id",
            field_name="operation_id",
        )
    return value


def scan_assessment_locator_digest(key: "ScanAssessmentKey") -> str:
    """Return the mandated opaque locator without exposing a path."""

    if not isinstance(key, ScanAssessmentKey):
        raise TypeError("key must be a ScanAssessmentKey")
    material = key.namespace.encode("utf-8") + b"\0" + key.source_id.encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class ScanAssessmentKey:
    namespace: str
    source_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "namespace",
            _portable_source_segment(
                self.namespace,
                field_name="namespace",
                pattern=_NAMESPACE_RE,
            ),
        )
        object.__setattr__(
            self,
            "source_id",
            _portable_source_segment(
                self.source_id,
                field_name="source_id",
                pattern=_SOURCE_ID_RE,
            ),
        )

    @property
    def source_reference(self) -> str:
        return f"{self.namespace}:{self.source_id}"

    def as_dict(self) -> dict[str, str]:
        return {"namespace": self.namespace, "source_id": self.source_id}


@dataclass(frozen=True, slots=True)
class ScanAssessmentProvenance:
    review_record_uuid: str = ""
    source_database: str = ""
    source_snapshot: str = ""
    source_row_sha256: str = ""

    def __post_init__(self) -> None:
        record_uuid = self.review_record_uuid
        if record_uuid:
            try:
                parsed = uuid.UUID(record_uuid)
            except (AttributeError, ValueError) as exc:
                raise _validation(
                    "review_record_uuid must be a UUID",
                    code="invalid_scan_assessment_provenance",
                    field_name="review_record_uuid",
                ) from exc
            record_uuid = str(parsed)
        elif not isinstance(record_uuid, str):
            raise _validation(
                "review_record_uuid must be a string",
                code="invalid_scan_assessment_provenance",
                field_name="review_record_uuid",
            )
        object.__setattr__(self, "review_record_uuid", record_uuid)
        object.__setattr__(
            self,
            "source_database",
            _bounded_public_provenance(
                self.source_database,
                field_name="source_database",
            ),
        )
        object.__setattr__(
            self,
            "source_snapshot",
            _bounded_public_provenance(
                self.source_snapshot,
                field_name="source_snapshot",
            ),
        )
        object.__setattr__(
            self,
            "source_row_sha256",
            _sha256(
                self.source_row_sha256,
                field_name="source_row_sha256",
                optional=True,
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "review_record_uuid": self.review_record_uuid,
            "source_database": self.source_database,
            "source_snapshot": self.source_snapshot,
            "source_row_sha256": self.source_row_sha256,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ScanAssessmentProvenance":
        if not isinstance(value, Mapping) or frozenset(value) != _PROVENANCE_FIELDS:
            raise _validation(
                "provenance fields must match the scan-assessment schema",
                code="invalid_scan_assessment_manifest",
                field_name="provenance",
            )
        return cls(
            review_record_uuid=value["review_record_uuid"],
            source_database=value["source_database"],
            source_snapshot=value["source_snapshot"],
            source_row_sha256=value["source_row_sha256"],
        )


def _alias(value: Any, *, field_name: str) -> str:
    if value == "":
        return ""
    return _portable_source_segment(
        value,
        field_name=field_name,
        pattern=_ALIAS_RE,
    )


def _markdown_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise _validation(
            "text must be UTF-8 Markdown",
            code="invalid_scan_assessment_text",
            field_name="text",
        )
    if any(
        ord(character) == 0 or 0xD800 <= ord(character) <= 0xDFFF for character in value
    ):
        raise _validation(
            "text contains an unsafe Unicode value",
            code="invalid_scan_assessment_text",
            field_name="text",
        )
    try:
        payload = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _validation(
            "text must be valid UTF-8 Markdown",
            code="invalid_scan_assessment_text",
            field_name="text",
        ) from exc
    if len(payload) > MAX_SCAN_ASSESSMENT_BYTES:
        raise _validation(
            "scan-assessment Markdown exceeds the byte limit",
            code="scan_assessment_too_large",
            field_name="text",
        )
    return payload


@dataclass(frozen=True, slots=True)
class ScanAssessmentDraft:
    text: str
    provenance: ScanAssessmentProvenance = field(
        default_factory=ScanAssessmentProvenance
    )
    canonical_item_id: str = ""
    capture_id: str = ""

    def __post_init__(self) -> None:
        _markdown_bytes(self.text)
        if not isinstance(self.provenance, ScanAssessmentProvenance):
            raise TypeError("provenance must be ScanAssessmentProvenance")
        object.__setattr__(
            self,
            "canonical_item_id",
            _alias(self.canonical_item_id, field_name="canonical_item_id"),
        )
        object.__setattr__(
            self,
            "capture_id",
            _alias(self.capture_id, field_name="capture_id"),
        )

    @property
    def utf8_bytes(self) -> bytes:
        return _markdown_bytes(self.text)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "provenance": self.provenance.as_dict(),
            "canonical_item_id": self.canonical_item_id,
            "capture_id": self.capture_id,
        }


@dataclass(frozen=True, slots=True)
class ScanAssessmentManifest:
    key: ScanAssessmentKey
    content_sha256: str
    byte_size: int
    revision: str
    created_at: str
    updated_at: str
    provenance: ScanAssessmentProvenance = field(
        default_factory=ScanAssessmentProvenance
    )
    canonical_item_id: str = ""
    capture_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.key, ScanAssessmentKey):
            raise TypeError("key must be a ScanAssessmentKey")
        object.__setattr__(
            self,
            "content_sha256",
            _sha256(self.content_sha256, field_name="content_sha256"),
        )
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
            or self.byte_size > MAX_SCAN_ASSESSMENT_BYTES
        ):
            raise _validation(
                "byte_size is outside the scan-assessment limit",
                code="invalid_scan_assessment_manifest",
                field_name="byte_size",
            )
        object.__setattr__(self, "revision", _revision(self.revision))
        created_at, created = _timestamp(self.created_at, field_name="created_at")
        updated_at, updated = _timestamp(self.updated_at, field_name="updated_at")
        if updated < created:
            raise _validation(
                "updated_at must not precede created_at",
                code="invalid_scan_assessment_manifest",
                field_name="updated_at",
            )
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        if not isinstance(self.provenance, ScanAssessmentProvenance):
            raise TypeError("provenance must be ScanAssessmentProvenance")
        object.__setattr__(
            self,
            "canonical_item_id",
            _alias(self.canonical_item_id, field_name="canonical_item_id"),
        )
        object.__setattr__(
            self,
            "capture_id",
            _alias(self.capture_id, field_name="capture_id"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCAN_ASSESSMENT_SCHEMA,
            "namespace": self.key.namespace,
            "source_id": self.key.source_id,
            "artifact_id": SCAN_ASSESSMENT_ARTIFACT_ID,
            "media_type": SCAN_ASSESSMENT_MEDIA_TYPE,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provenance": self.provenance.as_dict(),
            "canonical_item_id": self.canonical_item_id,
            "capture_id": self.capture_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ScanAssessmentManifest":
        if not isinstance(value, Mapping) or frozenset(value) != _MANIFEST_FIELDS:
            raise _validation(
                "manifest fields must match the scan-assessment schema",
                code="invalid_scan_assessment_manifest",
                field_name="manifest",
            )
        if (
            value["schema"] != SCAN_ASSESSMENT_SCHEMA
            or value["artifact_id"] != SCAN_ASSESSMENT_ARTIFACT_ID
            or value["media_type"] != SCAN_ASSESSMENT_MEDIA_TYPE
        ):
            raise _validation(
                "the scan-assessment manifest schema is unsupported",
                code="unsupported_scan_assessment_schema",
                field_name="schema",
            )
        return cls(
            key=ScanAssessmentKey(value["namespace"], value["source_id"]),
            content_sha256=value["content_sha256"],
            byte_size=value["byte_size"],
            revision=value["revision"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            provenance=ScanAssessmentProvenance.from_dict(value["provenance"]),
            canonical_item_id=value["canonical_item_id"],
            capture_id=value["capture_id"],
        )


@dataclass(frozen=True, slots=True)
class ScanAssessmentView:
    manifest: ScanAssessmentManifest
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ScanAssessmentManifest):
            raise TypeError("manifest must be ScanAssessmentManifest")
        payload = _markdown_bytes(self.text)
        if len(payload) != self.manifest.byte_size:
            raise _validation(
                "text byte length does not match its manifest",
                code="scan_assessment_size_mismatch",
                field_name="text",
            )
        if hashlib.sha256(payload).hexdigest() != self.manifest.content_sha256:
            raise _validation(
                "text digest does not match its manifest",
                code="scan_assessment_hash_mismatch",
                field_name="text",
            )

    @property
    def key(self) -> ScanAssessmentKey:
        return self.manifest.key

    @property
    def revision(self) -> str:
        return self.manifest.revision

    def as_dict(self) -> dict[str, Any]:
        return {"manifest": self.manifest.as_dict(), "text": self.text}


class ScanAssessmentIntegrityError(RepositoryError):
    """Stored assessment bytes cannot be trusted or represented safely."""

    default_code = "scan_assessment_integrity_error"


def scan_assessment_not_found(key: ScanAssessmentKey) -> NotFoundError:
    return NotFoundError(
        "the scan assessment does not exist",
        code="scan_assessment_not_found",
        details=key.as_dict(),
    )


@runtime_checkable
class ScanAssessmentRepositoryPort(Protocol):
    """Atomic, CAS-protected storage keyed by an original source reference."""

    def read(self, key: ScanAssessmentKey) -> ScanAssessmentView | None: ...

    def create(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        operation_id: str,
    ) -> ScanAssessmentView: ...

    def update(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        expected_revision: str,
        operation_id: str,
    ) -> ScanAssessmentView: ...

    def delete(
        self,
        key: ScanAssessmentKey,
        expected_revision: str,
        operation_id: str,
    ) -> str: ...


class ScanAssessmentService:
    """Validate service inputs and expose source-reference CRUD operations."""

    def __init__(self, repository: ScanAssessmentRepositoryPort) -> None:
        if not isinstance(repository, ScanAssessmentRepositoryPort):
            raise TypeError("repository must implement ScanAssessmentRepositoryPort")
        self._repository = repository

    def find(self, key: ScanAssessmentKey) -> ScanAssessmentView | None:
        self._key(key)
        return self._repository.read(key)

    def get(self, key: ScanAssessmentKey) -> ScanAssessmentView:
        current = self.find(key)
        if current is None:
            raise scan_assessment_not_found(key)
        return current

    def create(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        operation_id: str,
    ) -> ScanAssessmentView:
        self._key(key)
        self._draft(draft)
        return self._repository.create(
            key,
            draft,
            validate_scan_assessment_operation_id(operation_id),
        )

    def update(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        expected_revision: str,
        operation_id: str,
    ) -> ScanAssessmentView:
        self._key(key)
        self._draft(draft)
        return self._repository.update(
            key,
            draft,
            validate_scan_assessment_revision(expected_revision),
            validate_scan_assessment_operation_id(operation_id),
        )

    def delete(
        self,
        key: ScanAssessmentKey,
        expected_revision: str,
        operation_id: str,
    ) -> str:
        self._key(key)
        return self._repository.delete(
            key,
            validate_scan_assessment_revision(expected_revision),
            validate_scan_assessment_operation_id(operation_id),
        )

    @staticmethod
    def _key(value: Any) -> None:
        if not isinstance(value, ScanAssessmentKey):
            raise TypeError("key must be a ScanAssessmentKey")

    @staticmethod
    def _draft(value: Any) -> None:
        if not isinstance(value, ScanAssessmentDraft):
            raise TypeError("draft must be a ScanAssessmentDraft")


def canonical_scan_assessment_json(value: Mapping[str, Any]) -> bytes:
    """Canonical UTF-8 JSON used for hashes and persisted descriptors."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _validation(
            "scan-assessment metadata cannot be serialized",
            code="invalid_scan_assessment_manifest",
            field_name="manifest",
        ) from exc


__all__ = [
    "MAX_SCAN_ASSESSMENT_BYTES",
    "MAX_SCAN_ASSESSMENT_MANIFEST_BYTES",
    "MAX_SCAN_ASSESSMENT_PROVENANCE_CHARACTERS",
    "SCAN_ASSESSMENT_ARTIFACT_ID",
    "SCAN_ASSESSMENT_CONTRACT_VERSION",
    "SCAN_ASSESSMENT_MEDIA_TYPE",
    "SCAN_ASSESSMENT_SCHEMA",
    "ScanAssessmentDraft",
    "ScanAssessmentIntegrityError",
    "ScanAssessmentKey",
    "ScanAssessmentManifest",
    "ScanAssessmentProvenance",
    "ScanAssessmentRepositoryPort",
    "ScanAssessmentService",
    "ScanAssessmentView",
    "canonical_scan_assessment_json",
    "scan_assessment_locator_digest",
    "scan_assessment_not_found",
    "validate_scan_assessment_operation_id",
    "validate_scan_assessment_revision",
]
