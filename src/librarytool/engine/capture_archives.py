"""Replay-safe association of capture snapshots with sealed ``.lib/3`` files.

The application service in this module deliberately knows neither filesystem
paths nor the legacy capture store.  A capture adapter supplies one immutable
lib/3 seed, a materializer seals it, and a repository publishes the archive,
association, and operation receipt as one transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .errors import ConflictError, EngineError, RepositoryError, ValidationError


CAPTURE_LIB_FORMAT_VERSION = "3.0"
CAPTURE_LIB_ASSOCIATION_SCHEMA = "org.whl.capture-lib-association"
CAPTURE_LIB_ASSOCIATION_VERSION = 1
CAPTURE_LIB_RECEIPT_SCHEMA = "org.whl.capture-lib-receipt"
CAPTURE_LIB_RECEIPT_VERSION = 1

_CAPTURE_ARCHIVE_COMMAND_SCHEMA = "org.whl.capture-lib-command"
_CAPTURE_ARCHIVE_COMMAND_VERSION = 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOOK_ID_RE = re.compile(r"^b-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_MANIFEST_FIELDS = frozenset(
    {
        "created_at",
        "source",
        "meta",
        "instructions",
        "representations",
        "artifacts",
        "review_policy",
        "ext",
    }
)
_REQUIRED_MANIFEST_FIELDS = frozenset({"representations", "artifacts"})
_MAX_MANIFEST_BYTES = 10 * 1024 * 1024
_MAX_JSON_NESTING = 128
_MAX_REPRESENTATIONS = 5_000
_MAX_ARTIFACTS = 10_000
_MAX_RESOURCE_BYTES = 100 * 1024 * 1024
_MAX_TOTAL_RESOURCE_BYTES = 300 * 1024 * 1024
_BOOK_NAMESPACE = uuid.UUID("786b8352-2398-55f4-a04b-91d79fc52cf1")
_REVIEW_POLICY_MODES = frozenset({"all-durable", "active-only", "none"})
_REPRESENTATION_FIELDS = frozenset(
    {
        "id",
        "revision",
        "role",
        "media_type",
        "member",
        "content_sha256",
        "dimensions",
        "lineage",
        "ext",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "id",
        "revision",
        "kind",
        "media_type",
        "member",
        "content_sha256",
        "source",
        "dimensions",
        "provenance",
        "category_assignments",
        "caption_assertions",
        "role_assignments",
        "selector",
        "relationships",
        "ext",
    }
)


def _validation(
    message: str,
    *,
    code: str,
    field_name: str,
    details: Mapping[str, Any] | None = None,
) -> ValidationError:
    return ValidationError(
        message,
        code=code,
        details={"field": field_name, **dict(details or {})},
    )


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a portable opaque identifier",
            code="invalid_capture_archive_command",
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
            code="invalid_capture_archive_command",
            field_name=field_name,
        )
    return value


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a lowercase SHA-256 digest",
            code="invalid_capture_archive_document",
            field_name=field_name,
        )
    return value


def _timestamp(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 80
        or value != value.strip()
    ):
        raise _validation(
            f"{field_name} must be a bounded timestamp",
            code="invalid_capture_archive_document",
            field_name=field_name,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _validation(
            f"{field_name} must be an ISO-8601 timestamp",
            code="invalid_capture_archive_document",
            field_name=field_name,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _validation(
            f"{field_name} must include a UTC offset",
            code="invalid_capture_archive_document",
            field_name=field_name,
        )
    return value


def _canonical_json(value: Any, *, field_name: str) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise _validation(
            f"{field_name} must contain portable JSON",
            code="invalid_capture_archive_command",
            field_name=field_name,
        ) from exc
    if len(payload) > _MAX_MANIFEST_BYTES:
        raise _validation(
            f"{field_name} is too large",
            code="invalid_capture_archive_command",
            field_name=field_name,
            details={"maximum_bytes": _MAX_MANIFEST_BYTES},
        )
    return payload


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _validate_json_nesting(
    value: Any,
    *,
    field_name: str,
    maximum: int = _MAX_JSON_NESTING,
) -> None:
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if isinstance(current, dict):
            nested_depth = depth + 1
            if nested_depth > maximum:
                raise _validation(
                    f"{field_name} nesting is too deep",
                    code="invalid_capture_archive_command",
                    field_name=field_name,
                    details={"maximum_nesting": maximum},
                )
            pending.extend(
                (nested, nested_depth) for nested in current.values()
            )
        elif isinstance(current, list):
            nested_depth = depth + 1
            if nested_depth > maximum:
                raise _validation(
                    f"{field_name} nesting is too deep",
                    code="invalid_capture_archive_command",
                    field_name=field_name,
                    details={"maximum_nesting": maximum},
                )
            pending.extend((nested, nested_depth) for nested in current)


def _safe_resource_member(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _validation(
            "resource member must be a portable archive path",
            code="invalid_capture_archive_command",
            field_name="resources",
        )
    parts = value.split("/")
    if parts[0] not in {"representations", "artifacts"} or any(
        part in {"", ".", ".."} for part in parts
    ):
        raise _validation(
            "resources must live below representations/ or artifacts/",
            code="invalid_capture_archive_command",
            field_name="resources",
            details={"member": value},
        )
    return value


@dataclass(frozen=True, slots=True)
class CaptureArchiveSource:
    """One immutable, revision-pinned seed for a capture-only lib/3 archive."""

    capture_id: str
    source_revision: str
    manifest: Mapping[str, Any]
    resources: Mapping[str, bytes]
    _manifest_payload: bytes = field(init=False, repr=False, compare=False)
    _fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capture_id", _identifier(self.capture_id, "capture_id")
        )
        object.__setattr__(
            self,
            "source_revision",
            _revision(self.source_revision, "source_revision"),
        )
        if not isinstance(self.manifest, Mapping):
            raise _validation(
                "manifest must be an object",
                code="invalid_capture_archive_command",
                field_name="manifest",
            )
        raw_manifest = dict(self.manifest)
        unknown = frozenset(raw_manifest) - _ALLOWED_MANIFEST_FIELDS
        missing = _REQUIRED_MANIFEST_FIELDS - frozenset(raw_manifest)
        if unknown or missing:
            raise _validation(
                "capture manifest fields must match the lib/3 seed contract",
                code="invalid_capture_archive_command",
                field_name="manifest",
                details={
                    "unknown": sorted(str(value) for value in unknown),
                    "missing": sorted(missing),
                },
            )
        if (
            not isinstance(raw_manifest["representations"], list)
            or not raw_manifest["representations"]
        ):
            raise _validation(
                "a capture archive needs at least one representation",
                code="invalid_capture_archive_command",
                field_name="manifest.representations",
            )
        if not isinstance(raw_manifest["artifacts"], list):
            raise _validation(
                "manifest.artifacts must be an array",
                code="invalid_capture_archive_command",
                field_name="manifest.artifacts",
            )
        if len(raw_manifest["representations"]) > _MAX_REPRESENTATIONS:
            raise _validation(
                "manifest has too many representations",
                code="invalid_capture_archive_command",
                field_name="manifest.representations",
                details={"maximum_items": _MAX_REPRESENTATIONS},
            )
        if len(raw_manifest["artifacts"]) > _MAX_ARTIFACTS:
            raise _validation(
                "manifest has too many artifacts",
                code="invalid_capture_archive_command",
                field_name="manifest.artifacts",
                details={"maximum_items": _MAX_ARTIFACTS},
            )
        manifest_payload = _canonical_json(
            raw_manifest,
            field_name="manifest",
        )
        detached_manifest = json.loads(manifest_payload)
        _validate_json_nesting(
            detached_manifest,
            field_name="manifest",
        )
        if "created_at" in detached_manifest:
            created_at = detached_manifest["created_at"]
            if created_at != "":
                _timestamp(
                    created_at,
                    "manifest.created_at",
                )
        if "source" in detached_manifest:
            _identifier(
                detached_manifest["source"],
                "manifest.source",
            )
        for field_name in ("meta", "ext"):
            if field_name in detached_manifest and not isinstance(
                detached_manifest[field_name],
                dict,
            ):
                raise _validation(
                    f"manifest.{field_name} must be an object",
                    code="invalid_capture_archive_command",
                    field_name=f"manifest.{field_name}",
                )
        if "instructions" in detached_manifest:
            instructions = detached_manifest["instructions"]
            if (
                not isinstance(instructions, dict)
                or not set(instructions).issubset({"book"})
                or (
                    "book" in instructions
                    and not isinstance(instructions["book"], str)
                )
            ):
                raise _validation(
                    "manifest.instructions must contain only a book string",
                    code="invalid_capture_archive_command",
                    field_name="manifest.instructions",
                )
            detached_manifest["instructions"] = {
                "book": instructions.get("book", "")
            }
        if "review_policy" in detached_manifest:
            policy = detached_manifest["review_policy"]
            if (
                not isinstance(policy, dict)
                or set(policy) != {"mode"}
                or policy.get("mode") not in _REVIEW_POLICY_MODES
            ):
                raise _validation(
                    "manifest.review_policy must contain one supported mode",
                    code="invalid_capture_archive_command",
                    field_name="manifest.review_policy",
                )
        detached_manifest.setdefault("created_at", "")
        detached_manifest.setdefault("source", "primary")
        detached_manifest.setdefault("meta", {})
        detached_manifest.setdefault("instructions", {"book": ""})
        detached_manifest.setdefault(
            "review_policy",
            {"mode": "all-durable"},
        )
        detached_manifest.setdefault("ext", {})
        for collection in ("representations", "artifacts"):
            records = detached_manifest[collection]
            identities: dict[str, str] = {}
            allowed_fields = (
                _REPRESENTATION_FIELDS
                if collection == "representations"
                else _ARTIFACT_FIELDS
            )
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    raise _validation(
                        f"manifest.{collection} must contain objects",
                        code="invalid_capture_archive_command",
                        field_name=f"manifest.{collection}[{index}]",
                    )
                unknown_record_fields = set(record) - allowed_fields
                if unknown_record_fields:
                    raise _validation(
                        f"manifest.{collection} record fields are invalid",
                        code="invalid_capture_archive_command",
                        field_name=f"manifest.{collection}[{index}]",
                        details={
                            "unknown": sorted(unknown_record_fields),
                        },
                    )
                identity = _identifier(
                    record.get("id"),
                    f"manifest.{collection}[{index}].id",
                )
                previous = identities.get(identity.casefold())
                if previous is not None:
                    raise _validation(
                        f"manifest.{collection} identities must be unique",
                        code="invalid_capture_archive_command",
                        field_name=f"manifest.{collection}[{index}].id",
                        details={"identity": identity, "previous": previous},
                    )
                identities[identity.casefold()] = identity
            detached_manifest[collection] = sorted(
                records,
                key=lambda record: (
                    str(record["id"]).casefold(),
                    str(record["id"]),
                ),
            )
        representations = detached_manifest["representations"]
        representation_by_id = {str(record["id"]): record for record in representations}
        originals: set[str] = set()
        renditions: list[dict[str, Any]] = []
        for index, record in enumerate(representations):
            _revision(
                record.get("revision"),
                f"manifest.representations[{index}].revision",
            )
            role = _identifier(
                record.get("role"),
                f"manifest.representations[{index}].role",
            )
            lineage = record.get("lineage")
            if not isinstance(lineage, list):
                raise _validation(
                    "representation lineage must be an array",
                    code="invalid_capture_archive_command",
                    field_name=f"manifest.representations[{index}].lineage",
                )
            if role == "capture-original":
                originals.add(str(record["id"]))
            if role in {"capture-display", "corrected-rendition"}:
                renditions.append(record)
        if not originals or not renditions:
            raise _validation(
                "a capture archive needs original and display/corrected representations",
                code="invalid_capture_archive_command",
                field_name="manifest.representations",
            )

        children_by_parent: dict[str, set[str]] = {}
        for identity, record in representation_by_id.items():
            lineage = record.get("lineage")
            if not isinstance(lineage, list):
                continue
            for parent in lineage:
                if not isinstance(parent, dict):
                    continue
                parent_id = parent.get("representation_id")
                if isinstance(parent_id, str):
                    children_by_parent.setdefault(parent_id, set()).add(
                        identity
                    )
        reaches_original = set(originals)
        pending = list(originals)
        while pending:
            parent_id = pending.pop()
            for child_id in children_by_parent.get(parent_id, ()):
                if child_id not in reaches_original:
                    reaches_original.add(child_id)
                    pending.append(child_id)

        for rendition in renditions:
            if str(rendition["id"]) not in reaches_original:
                raise _validation(
                    "every display/corrected representation must descend from an original",
                    code="invalid_capture_archive_command",
                    field_name=(f"manifest.representations.{rendition['id']}.lineage"),
                )
        manifest_payload = _canonical_json(
            detached_manifest,
            field_name="manifest",
        )
        object.__setattr__(self, "manifest", _freeze_json(detached_manifest))
        object.__setattr__(self, "_manifest_payload", manifest_payload)

        if not isinstance(self.resources, Mapping) or not self.resources:
            raise _validation(
                "resources must be a non-empty object",
                code="invalid_capture_archive_command",
                field_name="resources",
            )
        resources: dict[str, bytes] = {}
        aliases: dict[str, str] = {}
        total = 0
        for raw_member, raw_content in self.resources.items():
            member = _safe_resource_member(raw_member)
            alias = member.casefold()
            previous = aliases.get(alias)
            if previous is not None and previous != member:
                raise _validation(
                    "resource members may not alias by case",
                    code="invalid_capture_archive_command",
                    field_name="resources",
                    details={"members": [previous, member]},
                )
            aliases[alias] = member
            if not isinstance(raw_content, (bytes, bytearray, memoryview)):
                raise _validation(
                    "resource content must be bytes",
                    code="invalid_capture_archive_command",
                    field_name=f"resources.{member}",
                )
            content = bytes(raw_content)
            if len(content) > _MAX_RESOURCE_BYTES:
                raise _validation(
                    "one capture resource is too large",
                    code="invalid_capture_archive_command",
                    field_name=f"resources.{member}",
                    details={"maximum_bytes": _MAX_RESOURCE_BYTES},
                )
            total += len(content)
            resources[member] = content
        if total > _MAX_TOTAL_RESOURCE_BYTES:
            raise _validation(
                "capture resources are too large",
                code="invalid_capture_archive_command",
                field_name="resources",
                details={"maximum_bytes": _MAX_TOTAL_RESOURCE_BYTES},
            )
        ordered = dict(sorted(resources.items()))
        object.__setattr__(self, "resources", MappingProxyType(ordered))

        descriptor = {
            "capture_id": self.capture_id,
            "source_revision": self.source_revision,
            "manifest": detached_manifest,
            "resources": [
                {
                    "member": member,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for member, content in ordered.items()
            ],
        }
        fingerprint = hashlib.sha256(
            b"org.whl.capture-lib-source/1\0"
            + _canonical_json(descriptor, field_name="source")
        ).hexdigest()
        object.__setattr__(self, "_fingerprint", fingerprint)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def manifest_copy(self) -> dict[str, Any]:
        return json.loads(self._manifest_payload)

    def descriptor(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "source_revision": self.source_revision,
            "manifest": self.manifest_copy(),
            "resources": [
                {
                    "member": member,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for member, content in self.resources.items()
            ],
        }


@dataclass(frozen=True, slots=True)
class AssociateCaptureArchiveCommand:
    source: CaptureArchiveSource
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, CaptureArchiveSource):
            raise TypeError("source must be a CaptureArchiveSource")
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )

    @property
    def fingerprint(self) -> str:
        command = {
            "schema": _CAPTURE_ARCHIVE_COMMAND_SCHEMA,
            "version": _CAPTURE_ARCHIVE_COMMAND_VERSION,
            "source": self.source.descriptor(),
        }
        return hashlib.sha256(
            _canonical_json(command, field_name="command")
        ).hexdigest()


def capture_book_id(capture_id: str) -> str:
    """Return the canonical portable book identity for one capture identity."""

    normalized = _identifier(capture_id, "capture_id")
    return "b-" + uuid.uuid5(_BOOK_NAMESPACE, normalized).hex


class CaptureArchiveState(str, Enum):
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class CaptureArchiveAssociation:
    capture_id: str
    book_id: str
    archive_sha256: str
    archive_bytes: int
    format_version: str
    state: CaptureArchiveState | str
    generated_at: str
    source_revision: str
    source_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capture_id", _identifier(self.capture_id, "capture_id")
        )
        if not isinstance(self.book_id, str) or not _BOOK_ID_RE.fullmatch(self.book_id):
            raise _validation(
                "book_id must be a stable lib identity",
                code="invalid_capture_archive_document",
                field_name="book_id",
            )
        object.__setattr__(
            self,
            "archive_sha256",
            _sha256(self.archive_sha256, "archive_sha256"),
        )
        if (
            not isinstance(self.archive_bytes, int)
            or isinstance(self.archive_bytes, bool)
            or self.archive_bytes <= 0
        ):
            raise _validation(
                "archive_bytes must be a positive integer",
                code="invalid_capture_archive_document",
                field_name="archive_bytes",
            )
        if self.format_version != CAPTURE_LIB_FORMAT_VERSION:
            raise _validation(
                "association format_version must be lib/3.0",
                code="invalid_capture_archive_document",
                field_name="format_version",
            )
        try:
            state = CaptureArchiveState(self.state)
        except (TypeError, ValueError) as exc:
            raise _validation(
                "association state must be current or stale",
                code="invalid_capture_archive_document",
                field_name="state",
            ) from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "generated_at",
            _timestamp(self.generated_at, "generated_at"),
        )
        object.__setattr__(
            self,
            "source_revision",
            _revision(self.source_revision, "source_revision"),
        )
        object.__setattr__(
            self,
            "source_fingerprint",
            _sha256(self.source_fingerprint, "source_fingerprint"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPTURE_LIB_ASSOCIATION_SCHEMA,
            "version": CAPTURE_LIB_ASSOCIATION_VERSION,
            "capture_id": self.capture_id,
            "book_id": self.book_id,
            "archive_sha256": self.archive_sha256,
            "archive_bytes": self.archive_bytes,
            "format_version": self.format_version,
            "state": self.state.value,
            "generated_at": self.generated_at,
            "source_revision": self.source_revision,
            "source_fingerprint": self.source_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CaptureArchiveAssociation":
        if not isinstance(value, Mapping):
            raise ValueError("capture archive association must be an object")
        expected = {
            "schema",
            "version",
            "capture_id",
            "book_id",
            "archive_sha256",
            "archive_bytes",
            "format_version",
            "state",
            "generated_at",
            "source_revision",
            "source_fingerprint",
        }
        if set(value) != expected:
            raise ValueError("capture archive association fields are invalid")
        if (
            value["schema"] != CAPTURE_LIB_ASSOCIATION_SCHEMA
            or type(value["version"]) is not int
            or value["version"] != CAPTURE_LIB_ASSOCIATION_VERSION
        ):
            raise ValueError("capture archive association schema is invalid")
        return cls(
            capture_id=value["capture_id"],
            book_id=value["book_id"],
            archive_sha256=value["archive_sha256"],
            archive_bytes=value["archive_bytes"],
            format_version=value["format_version"],
            state=value["state"],
            generated_at=value["generated_at"],
            source_revision=value["source_revision"],
            source_fingerprint=value["source_fingerprint"],
        )


class CaptureArchiveDisposition(str, Enum):
    CREATED = "created"
    EXISTING = "existing"


@dataclass(frozen=True, slots=True)
class CaptureArchiveReceipt:
    operation_id: str
    command_sha256: str
    disposition: CaptureArchiveDisposition | str
    association: CaptureArchiveAssociation

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )
        object.__setattr__(
            self,
            "command_sha256",
            _sha256(self.command_sha256, "command_sha256"),
        )
        try:
            disposition = CaptureArchiveDisposition(self.disposition)
        except (TypeError, ValueError) as exc:
            raise _validation(
                "disposition must be created or existing",
                code="invalid_capture_archive_document",
                field_name="disposition",
            ) from exc
        object.__setattr__(self, "disposition", disposition)
        if not isinstance(self.association, CaptureArchiveAssociation):
            raise TypeError("association must be a CaptureArchiveAssociation")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": CAPTURE_LIB_RECEIPT_SCHEMA,
            "version": CAPTURE_LIB_RECEIPT_VERSION,
            "operation_id": self.operation_id,
            "command_sha256": self.command_sha256,
            "disposition": self.disposition.value,
            "association": self.association.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CaptureArchiveReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("capture archive receipt must be an object")
        expected = {
            "schema",
            "version",
            "operation_id",
            "command_sha256",
            "disposition",
            "association",
        }
        if set(value) != expected:
            raise ValueError("capture archive receipt fields are invalid")
        if (
            value["schema"] != CAPTURE_LIB_RECEIPT_SCHEMA
            or type(value["version"]) is not int
            or value["version"] != CAPTURE_LIB_RECEIPT_VERSION
        ):
            raise ValueError("capture archive receipt schema is invalid")
        return cls(
            operation_id=value["operation_id"],
            command_sha256=value["command_sha256"],
            disposition=value["disposition"],
            association=CaptureArchiveAssociation.from_dict(value["association"]),
        )


@dataclass(frozen=True, slots=True)
class CaptureArchiveResult:
    receipt: CaptureArchiveReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CaptureArchiveReceipt):
            raise TypeError("receipt must be a CaptureArchiveReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CaptureArchivePublication:
    operation_id: str
    command_sha256: str
    capture_id: str
    book_id: str
    source_revision: str
    source_fingerprint: str
    generated_at: str
    archive: bytes

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, "operation_id"),
        )
        object.__setattr__(
            self,
            "command_sha256",
            _sha256(self.command_sha256, "command_sha256"),
        )
        object.__setattr__(
            self, "capture_id", _identifier(self.capture_id, "capture_id")
        )
        if not isinstance(self.book_id, str) or not _BOOK_ID_RE.fullmatch(self.book_id):
            raise _validation(
                "book_id must be a stable lib identity",
                code="invalid_capture_archive_document",
                field_name="book_id",
            )
        object.__setattr__(
            self,
            "source_revision",
            _revision(self.source_revision, "source_revision"),
        )
        object.__setattr__(
            self,
            "source_fingerprint",
            _sha256(self.source_fingerprint, "source_fingerprint"),
        )
        object.__setattr__(
            self,
            "generated_at",
            _timestamp(self.generated_at, "generated_at"),
        )
        if not isinstance(self.archive, bytes) or not self.archive:
            raise _validation(
                "archive must be non-empty immutable bytes",
                code="invalid_capture_archive_document",
                field_name="archive",
            )

    @property
    def association(self) -> CaptureArchiveAssociation:
        return CaptureArchiveAssociation(
            capture_id=self.capture_id,
            book_id=self.book_id,
            archive_sha256=hashlib.sha256(self.archive).hexdigest(),
            archive_bytes=len(self.archive),
            format_version=CAPTURE_LIB_FORMAT_VERSION,
            state=CaptureArchiveState.CURRENT,
            generated_at=self.generated_at,
            source_revision=self.source_revision,
            source_fingerprint=self.source_fingerprint,
        )


@runtime_checkable
class CaptureArchiveMaterializerPort(Protocol):
    def materialize(
        self,
        source: CaptureArchiveSource,
        *,
        book_id: str,
    ) -> bytes: ...


@runtime_checkable
class CaptureArchiveRepositoryPort(Protocol):
    def replay(
        self,
        command: AssociateCaptureArchiveCommand,
    ) -> CaptureArchiveResult | None: ...

    def bind_existing(
        self,
        command: AssociateCaptureArchiveCommand,
    ) -> CaptureArchiveResult | None: ...

    def publish(
        self,
        publication: CaptureArchivePublication,
    ) -> CaptureArchiveResult: ...

    def get(self, capture_id: str) -> CaptureArchiveAssociation | None: ...


class CaptureArchiveService:
    """Seal and atomically associate the first canonical archive for a capture."""

    def __init__(
        self,
        repository: CaptureArchiveRepositoryPort,
        materializer: CaptureArchiveMaterializerPort,
        *,
        timestamp: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(repository, CaptureArchiveRepositoryPort):
            raise TypeError("repository must implement CaptureArchiveRepositoryPort")
        if not isinstance(materializer, CaptureArchiveMaterializerPort):
            raise TypeError(
                "materializer must implement CaptureArchiveMaterializerPort"
            )
        if timestamp is not None and not callable(timestamp):
            raise TypeError("timestamp must be callable")
        self._repository = repository
        self._materializer = materializer
        self._timestamp = timestamp or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
        )

    def associate(
        self,
        command: AssociateCaptureArchiveCommand,
    ) -> CaptureArchiveResult:
        if not isinstance(command, AssociateCaptureArchiveCommand):
            raise TypeError("command must be an AssociateCaptureArchiveCommand")
        try:
            replay = self._repository.replay(command)
            if replay is not None:
                self._validate_result(command, replay)
                return replay

            existing = self._repository.bind_existing(command)
            if existing is not None:
                self._validate_result(command, existing)
                return existing

            book_id = capture_book_id(command.source.capture_id)
            archive = self._materializer.materialize(
                command.source,
                book_id=book_id,
            )
            if not isinstance(archive, bytes) or not archive:
                raise RepositoryError(
                    "the capture archive materializer returned invalid bytes",
                    code="invalid_capture_archive_materialization",
                )
            publication = CaptureArchivePublication(
                operation_id=command.operation_id,
                command_sha256=command.fingerprint,
                capture_id=command.source.capture_id,
                book_id=book_id,
                source_revision=command.source.source_revision,
                source_fingerprint=command.source.fingerprint,
                generated_at=_timestamp(self._timestamp(), "generated_at"),
                archive=archive,
            )
            result = self._repository.publish(publication)
            self._validate_result(
                command,
                result,
                expected_archive=publication.association,
            )
            return result
        except (ConflictError, RepositoryError, ValidationError):
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the capture archive association failed",
                code="capture_archive_association_failed",
                details={"cause": type(exc).__name__},
                retryable=True,
            ) from exc

    def get(self, capture_id: str) -> CaptureArchiveAssociation | None:
        normalized = _identifier(capture_id, "capture_id")
        try:
            association = self._repository.get(normalized)
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the capture archive association could not be loaded",
                code="capture_archive_association_unavailable",
                details={"cause": type(exc).__name__},
                retryable=True,
            ) from exc
        if association is not None and (
            association.capture_id != normalized
            or association.book_id != capture_book_id(normalized)
        ):
            raise RepositoryError(
                "the capture archive repository returned another identity",
                code="invalid_capture_archive_storage",
            )
        return association

    @staticmethod
    def _validate_result(
        command: AssociateCaptureArchiveCommand,
        result: CaptureArchiveResult,
        *,
        expected_archive: CaptureArchiveAssociation | None = None,
    ) -> None:
        if not isinstance(result, CaptureArchiveResult):
            raise RepositoryError(
                "the capture archive repository returned an invalid result",
                code="invalid_capture_archive_storage",
            )
        receipt = result.receipt
        association = receipt.association
        if (
            receipt.operation_id != command.operation_id
            or receipt.command_sha256 != command.fingerprint
            or association.capture_id != command.source.capture_id
            or association.book_id != capture_book_id(command.source.capture_id)
            or association.source_revision != command.source.source_revision
            or association.source_fingerprint != command.source.fingerprint
        ):
            raise RepositoryError(
                "the capture archive result is outside the command scope",
                code="invalid_capture_archive_storage",
            )
        if (
            expected_archive is not None
            and not result.replayed
            and receipt.disposition is CaptureArchiveDisposition.CREATED
            and (
            association.archive_sha256 != expected_archive.archive_sha256
            or association.archive_bytes != expected_archive.archive_bytes
            )
        ):
            raise ConflictError(
                "the same capture source produced different archive bytes",
                code="capture_archive_materialization_conflict",
                details={"capture_id": association.capture_id},
            )


__all__ = [
    "CAPTURE_LIB_ASSOCIATION_SCHEMA",
    "CAPTURE_LIB_ASSOCIATION_VERSION",
    "CAPTURE_LIB_FORMAT_VERSION",
    "CAPTURE_LIB_RECEIPT_SCHEMA",
    "CAPTURE_LIB_RECEIPT_VERSION",
    "AssociateCaptureArchiveCommand",
    "CaptureArchiveAssociation",
    "CaptureArchiveDisposition",
    "CaptureArchiveMaterializerPort",
    "CaptureArchivePublication",
    "CaptureArchiveReceipt",
    "CaptureArchiveRepositoryPort",
    "CaptureArchiveResult",
    "CaptureArchiveService",
    "CaptureArchiveSource",
    "CaptureArchiveState",
    "capture_book_id",
]
