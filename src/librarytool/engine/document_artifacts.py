"""Versioned, framework-neutral contracts for non-raster artifacts.

Document artifacts are bounded public projections.  They describe generated
metadata, OCR text, transform manifests, and other non-raster resources
without exposing the filesystem or a provider's private response.  Resource
identifiers are opaque grants resolved by a trusted adapter.

The contracts are read-only and storage-neutral.  A resource page pins both
the artifact revision and the exact resource revision so a caller can never
silently combine bytes from different snapshots.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .capabilities import CapabilityRef
from .errors import ValidationError


JsonMapping: TypeAlias = Mapping[str, Any]

DOCUMENT_ARTIFACT_CONTRACT_VERSION = 1
DOCUMENT_ARTIFACT_SCHEMA = (
    f"librarytool.document-artifact/{DOCUMENT_ARTIFACT_CONTRACT_VERSION}"
)
DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA = (
    f"librarytool.document-resource-page-request/{DOCUMENT_ARTIFACT_CONTRACT_VERSION}"
)
DOCUMENT_RESOURCE_PAGE_SCHEMA = (
    f"librarytool.document-resource-page/{DOCUMENT_ARTIFACT_CONTRACT_VERSION}"
)
DOCUMENT_ARTIFACTS_READ_CAPABILITY = CapabilityRef(
    "library.document-artifacts.read",
    DOCUMENT_ARTIFACT_CONTRACT_VERSION,
)

# These are first-party kinds, not a closed vocabulary.  Providers may add a
# portable lower-case kind without hiding it in extension metadata.
DOCUMENT_ARTIFACT_KINDS = frozenset(
    {
        "document",
        "generated-metadata",
        "ocr-text",
        "transform-manifest",
        "unknown-document",
    }
)

MAX_DOCUMENT_EXTENSION_DEPTH = 8
MAX_DOCUMENT_EXTENSION_NODES = 256
MAX_DOCUMENT_EXTENSION_ENCODED_BYTES = 16 * 1024
MAX_DOCUMENT_EXTENSION_STRING_CHARACTERS = 4 * 1024
MAX_DOCUMENT_LINEAGE_REFS = 64
# 48 KiB of raw bytes has a base64 representation no larger than 64 KiB.
MAX_DOCUMENT_RESOURCE_PAGE_BYTES = 48 * 1024
MAX_PORTABLE_INTEGER = (1 << 53) - 1

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KIND_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_REVISION_RE = re.compile(r"^[\x21-\x7e]{1,512}$")
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
)
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_UNC_PATH_RE = re.compile(r"^(?:\\\\|//)[^\\/]+[\\/]")
_PRIVATE_POSIX_PATH_RE = re.compile(
    r"^/(?:etc|home|mnt|private|root|run|srv|tmp|Users|usr|var|Volumes)(?:/|$)",
    re.IGNORECASE,
)
_PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_JWT_RE = re.compile(r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$")
_CREDENTIAL_PREFIX_RE = re.compile(
    r"^(?:Bearer\s+\S+|Basic\s+[A-Za-z0-9+/=]+|"
    r"github_pat_\S+|gh[pousr]_\S+|sk-[A-Za-z0-9_-]{16,}|"
    r"xox[a-z]-\S+)$",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_PRIVATE_KEY_NAMES = frozenset(
    {
        "absolute_path",
        "access_key",
        "api_key",
        "asset_ref",
        "auth",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "directory",
        "download_url",
        "file",
        "file_name",
        "filename",
        "filepath",
        "folder",
        "local_path",
        "locator",
        "password",
        "private_key",
        "resource_ref",
        "secret",
        "session",
        "session_id",
        "storage_key",
        "storage_locator",
        "storage_path",
        "token",
        "uri",
        "url",
    }
)
_PRIVATE_KEY_SUFFIXES = frozenset(
    {
        "cookie",
        "credential",
        "credentials",
        "file",
        "filename",
        "filepath",
        "locator",
        "password",
        "path",
        "secret",
        "token",
        "uri",
        "url",
    }
)


def _validation(message: str, *, code: str, field_name: str) -> ValidationError:
    return ValidationError(message, code=code, details={"field": field_name})


def _safe_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise _validation(
            f"{field_name} must be a bounded string",
            code="invalid_document_artifact",
            field_name=field_name,
        )
    if not allow_empty and not value.strip():
        raise _validation(
            f"{field_name} must not be empty",
            code="invalid_document_artifact",
            field_name=field_name,
        )
    if any(
        ord(character) == 127
        or (ord(character) < 32 and character not in "\n\r\t\f")
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise _validation(
            f"{field_name} contains an unsafe character",
            code="invalid_document_artifact",
            field_name=field_name,
        )
    return value


def _looks_private(value: str) -> bool:
    candidate = value.strip()
    return bool(
        candidate.casefold().startswith("file:")
        or candidate.startswith(("/", "./", "../", ".\\", "..\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(candidate)
        or _UNC_PATH_RE.match(candidate)
        or _PRIVATE_POSIX_PATH_RE.match(candidate)
        or _PEM_PRIVATE_KEY_RE.search(candidate)
        or _AWS_ACCESS_KEY_RE.search(candidate)
        or _JWT_RE.fullmatch(candidate)
        or _CREDENTIAL_PREFIX_RE.fullmatch(candidate)
        or _URL_USERINFO_RE.match(candidate)
    )


def _public_text(
    value: Any,
    field_name: str,
    *,
    maximum: int,
    allow_empty: bool = True,
) -> str:
    result = _safe_text(
        value,
        field_name,
        maximum=maximum,
        allow_empty=allow_empty,
    )
    if result and _looks_private(result):
        raise _validation(
            f"{field_name} must not expose a private locator or credential",
            code="private_document_artifact_value",
            field_name=field_name,
        )
    return result


def _opaque_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a portable opaque identifier",
            code="invalid_document_artifact_identity",
            field_name=field_name,
        )
    lowered = value.casefold()
    if (
        _WINDOWS_DRIVE_PREFIX_RE.match(value)
        or lowered.startswith(("file:", "http:", "https:", "ftp:"))
        or _looks_private(value)
    ):
        raise _validation(
            f"{field_name} must not be a path or URL",
            code="private_document_artifact_value",
            field_name=field_name,
        )
    return value


def _kind(value: Any, field_name: str = "kind") -> str:
    if not isinstance(value, str) or len(value) > 128 or not _KIND_RE.fullmatch(value):
        raise _validation(
            "kind must be a canonical lower-case portable identifier",
            code="invalid_document_artifact_kind",
            field_name=field_name,
        )
    return value


def _revision(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not _REVISION_RE.fullmatch(value)
        or any(character in {'"', "'", "\\", "/"} for character in value)
        or _WINDOWS_DRIVE_PREFIX_RE.match(value)
        or value.casefold().startswith(("http:", "https:", "ftp:"))
        or _looks_private(value)
    ):
        raise _validation(
            f"{field_name} must be a transport-safe ASCII revision token",
            code="invalid_document_artifact_revision",
            field_name=field_name,
        )
    return value


def _optional_revision(value: Any, field_name: str) -> str:
    if value == "":
        return ""
    return _revision(value, field_name)


def _nonnegative_integer(value: Any, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_PORTABLE_INTEGER
    ):
        raise _validation(
            f"{field_name} must be a non-negative portable integer",
            code="invalid_document_resource_size",
            field_name=field_name,
        )
    return value


def _positive_page_size(value: Any, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_DOCUMENT_RESOURCE_PAGE_BYTES
    ):
        raise _validation(
            f"{field_name} is outside the document page budget",
            code="invalid_document_resource_page",
            field_name=field_name,
        )
    return value


def _media_type(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or not _MEDIA_TYPE_RE.fullmatch(value)
        or value.casefold().startswith("image/")
    ):
        raise _validation(
            "media_type must identify a non-raster resource without parameters",
            code="invalid_document_media_type",
            field_name="media_type",
        )
    return value.casefold()


def _is_textual_media_type(media_type: str) -> bool:
    return (
        media_type.startswith("text/")
        or media_type
        in {
            "application/json",
            "application/javascript",
            "application/xml",
            "application/x-ndjson",
            "application/x-yaml",
        }
        or media_type.endswith(("+json", "+xml", "+yaml"))
    )


def _sha256(value: Any, field_name: str = "content_sha256") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a SHA-256 digest",
            code="invalid_document_artifact_checksum",
            field_name=field_name,
        )
    return value.casefold()


def _private_extension_key(key: str) -> bool:
    separated = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", key)
    separated = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", separated)
    normalized = _KEY_SEPARATOR_RE.sub("_", separated).strip("_").casefold()
    if normalized in _PRIVATE_KEY_NAMES:
        return True
    return normalized.rsplit("_", 1)[-1] in _PRIVATE_KEY_SUFFIXES


def _freeze_json(
    value: Any,
    *,
    path: str,
    depth: int = 0,
    active: set[int] | None = None,
    budget: list[int] | None = None,
) -> Any:
    if depth > MAX_DOCUMENT_EXTENSION_DEPTH:
        raise _validation(
            "extension metadata is nested too deeply",
            code="invalid_document_extensions",
            field_name=path,
        )
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > MAX_DOCUMENT_EXTENSION_NODES:
        raise _validation(
            "extension metadata has too many values",
            code="invalid_document_extensions",
            field_name=path,
        )

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            result = _public_text(
                value,
                path,
                maximum=MAX_DOCUMENT_EXTENSION_STRING_CHARACTERS,
            )
        except ValidationError as exc:
            raise _validation(
                "extension metadata contains unsafe private or unbounded text",
                code=(
                    "private_document_artifact_value"
                    if exc.code == "private_document_artifact_value"
                    else "invalid_document_extensions"
                ),
                field_name=path,
            ) from exc
        return result
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_PORTABLE_INTEGER:
            raise _validation(
                "extension metadata contains a non-portable integer",
                code="invalid_document_extensions",
                field_name=path,
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation(
                "extension metadata contains a non-finite number",
                code="invalid_document_extensions",
                field_name=path,
            )
        return value

    if active is None:
        active = set()
    if isinstance(value, Mapping):
        if len(value) > MAX_DOCUMENT_EXTENSION_NODES:
            raise _validation(
                "extension metadata has too many values",
                code="invalid_document_extensions",
                field_name=path,
            )
        identity = id(value)
        if identity in active:
            raise _validation(
                "extension metadata contains a reference cycle",
                code="invalid_document_extensions",
                field_name=path,
            )
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or key != key.strip()
                    or len(key) > 128
                ):
                    raise _validation(
                        "extension keys must be bounded, trimmed strings",
                        code="invalid_document_extensions",
                        field_name=path,
                    )
                _safe_text(
                    key,
                    f"{path} key",
                    maximum=128,
                    allow_empty=False,
                )
                if _private_extension_key(key):
                    raise _validation(
                        "extension metadata contains a private field",
                        code="private_document_artifact_extension",
                        field_name=f"{path}.{key}",
                    )
                frozen[key] = _freeze_json(
                    item,
                    path=f"{path}.{key}",
                    depth=depth + 1,
                    active=active,
                    budget=budget,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)

    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray, memoryview),
    ):
        if len(value) > MAX_DOCUMENT_EXTENSION_NODES:
            raise _validation(
                "extension metadata has too many values",
                code="invalid_document_extensions",
                field_name=path,
            )
        identity = id(value)
        if identity in active:
            raise _validation(
                "extension metadata contains a reference cycle",
                code="invalid_document_extensions",
                field_name=path,
            )
        active.add(identity)
        try:
            return tuple(
                _freeze_json(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    active=active,
                    budget=budget,
                )
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)

    raise _validation(
        "extension metadata contains non-JSON data",
        code="invalid_document_extensions",
        field_name=path,
    )


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _extensions(value: Any, field_name: str = "extensions") -> JsonMapping:
    if not isinstance(value, Mapping):
        raise _validation(
            f"{field_name} must be an object",
            code="invalid_document_extensions",
            field_name=field_name,
        )
    frozen = _freeze_json(value, path=f"$.{field_name}")
    assert isinstance(frozen, Mapping)
    try:
        encoded = json.dumps(
            _thaw(frozen),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _validation(
            f"{field_name} is not portable JSON",
            code="invalid_document_extensions",
            field_name=field_name,
        ) from exc
    if len(encoded) > MAX_DOCUMENT_EXTENSION_ENCODED_BYTES:
        raise _validation(
            f"{field_name} exceeds its encoded-size budget",
            code="invalid_document_extensions",
            field_name=field_name,
        )
    return frozen


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise _validation(
            f"{field_name} is invalid",
            code="invalid_document_artifact",
            field_name=field_name,
        ) from exc


class DocumentArtifactFreshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNTRACKED = "untracked"


class DocumentResourceState(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class DocumentPageMode(str, Enum):
    BYTES = "bytes"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class DocumentArtifactKey:
    item_id: str
    artifact_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "item_id",
            _opaque_identifier(self.item_id, "item_id"),
        )
        object.__setattr__(
            self,
            "artifact_id",
            _opaque_identifier(self.artifact_id, "artifact_id"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "artifact_id": self.artifact_id}


@dataclass(frozen=True, slots=True)
class DocumentResourceRef:
    """Opaque resource grant resolved by a trusted transport adapter."""

    resource_id: str
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _opaque_identifier(self.resource_id, "resource_id"),
        )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))

    def as_dict(self) -> dict[str, str]:
        return {"id": self.resource_id, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class DocumentSourceRef:
    """Exact public source used to derive a document artifact."""

    source_kind: str
    source_id: str
    source_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_kind",
            _kind(self.source_kind, "source_kind"),
        )
        object.__setattr__(
            self,
            "source_id",
            _opaque_identifier(self.source_id, "source_id"),
        )
        object.__setattr__(
            self,
            "source_revision",
            _revision(self.source_revision, "source_revision"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.source_kind,
            "id": self.source_id,
            "revision": self.source_revision,
        }


@dataclass(frozen=True, slots=True)
class DocumentLineageRef:
    artifact_id: str
    artifact_revision: str
    relation: str = "derived_from"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_id",
            _opaque_identifier(self.artifact_id, "lineage.artifact_id"),
        )
        object.__setattr__(
            self,
            "artifact_revision",
            _revision(self.artifact_revision, "lineage.artifact_revision"),
        )
        object.__setattr__(
            self,
            "relation",
            _kind(self.relation, "lineage.relation"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_revision": self.artifact_revision,
            "relation": self.relation,
        }


@dataclass(frozen=True, slots=True)
class DocumentArtifactProvenance:
    origin: str = "unknown"
    provider_id: str = ""
    model: str = ""
    recipe_revision: str = ""
    operation_id: str = ""
    generated_at: str = ""
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "origin",
            _kind(self.origin, "provenance.origin"),
        )
        if self.provider_id:
            object.__setattr__(
                self,
                "provider_id",
                _opaque_identifier(self.provider_id, "provider_id"),
            )
        object.__setattr__(
            self,
            "model",
            _public_text(self.model, "model", maximum=256),
        )
        object.__setattr__(
            self,
            "recipe_revision",
            _optional_revision(self.recipe_revision, "recipe_revision"),
        )
        if self.operation_id:
            object.__setattr__(
                self,
                "operation_id",
                _opaque_identifier(self.operation_id, "operation_id"),
            )
        generated_at = _safe_text(
            self.generated_at,
            "generated_at",
            maximum=64,
        )
        if generated_at:
            try:
                if not _RFC3339_RE.fullmatch(generated_at):
                    raise ValueError("timestamp shape")
                datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise _validation(
                    "generated_at must be an RFC 3339 timestamp",
                    code="invalid_document_artifact_provenance",
                    field_name="generated_at",
                ) from exc
        object.__setattr__(self, "generated_at", generated_at)
        object.__setattr__(
            self,
            "extensions",
            _extensions(self.extensions, "provenance.extensions"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "provider_id": self.provider_id,
            "model": self.model,
            "recipe_revision": self.recipe_revision,
            "operation_id": self.operation_id,
            "generated_at": self.generated_at,
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class DocumentResourceSummary:
    """Safe availability and integrity metadata for one exact resource."""

    state: DocumentResourceState | str
    media_type: str
    content_sha256: str | None = None
    byte_size: int | None = None
    resource: DocumentResourceRef | None = None
    text_encoding: str = ""

    def __post_init__(self) -> None:
        state = _enum(self.state, DocumentResourceState, "resource.state")
        media_type = _media_type(self.media_type)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "media_type", media_type)

        if (self.content_sha256 is None) != (self.byte_size is None):
            raise _validation(
                "resource checksum and size must be supplied together",
                code="invalid_document_resource_summary",
                field_name="resource.integrity",
            )
        if self.content_sha256 is not None:
            object.__setattr__(
                self,
                "content_sha256",
                _sha256(self.content_sha256),
            )
            object.__setattr__(
                self,
                "byte_size",
                _nonnegative_integer(self.byte_size, "byte_size"),
            )

        if state is DocumentResourceState.AVAILABLE:
            if not isinstance(self.resource, DocumentResourceRef):
                raise _validation(
                    "available resources require an opaque resource grant",
                    code="invalid_document_resource_state",
                    field_name="resource",
                )
            if self.content_sha256 is None:
                raise _validation(
                    "available resources require checksum and size",
                    code="invalid_document_resource_summary",
                    field_name="resource.integrity",
                )
        elif self.resource is not None:
            raise _validation(
                "nonavailable resources cannot expose a resource grant",
                code="invalid_document_resource_state",
                field_name="resource",
            )

        if self.text_encoding not in {"", "utf-8"}:
            raise _validation(
                "text_encoding must be empty or utf-8",
                code="invalid_document_text_encoding",
                field_name="text_encoding",
            )
        if self.text_encoding and not _is_textual_media_type(media_type):
            raise _validation(
                "text encoding requires a textual media type",
                code="invalid_document_text_encoding",
                field_name="text_encoding",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "resource": self.resource.as_dict() if self.resource else None,
            "text_encoding": self.text_encoding,
        }


@dataclass(frozen=True, slots=True)
class DocumentArtifactView:
    key: DocumentArtifactKey
    revision: str
    kind: str
    resource: DocumentResourceSummary
    source: DocumentSourceRef
    label: str = ""
    language: str = ""
    freshness: DocumentArtifactFreshness | str = DocumentArtifactFreshness.UNTRACKED
    lineage: tuple[DocumentLineageRef, ...] = ()
    provenance: DocumentArtifactProvenance = field(
        default_factory=DocumentArtifactProvenance
    )
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if not isinstance(self.key, DocumentArtifactKey):
            raise _validation(
                "key must be DocumentArtifactKey",
                code="invalid_document_artifact_identity",
                field_name="key",
            )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        object.__setattr__(self, "kind", _kind(self.kind))
        if not isinstance(self.resource, DocumentResourceSummary):
            raise _validation(
                "resource must be DocumentResourceSummary",
                code="invalid_document_resource_summary",
                field_name="resource",
            )
        if not isinstance(self.source, DocumentSourceRef):
            raise _validation(
                "source must be DocumentSourceRef",
                code="invalid_document_artifact_source",
                field_name="source",
            )
        object.__setattr__(
            self,
            "label",
            _public_text(self.label, "label", maximum=512),
        )
        if (
            not isinstance(self.language, str)
            or len(self.language) > 64
            or self.language
            and not _LANGUAGE_RE.fullmatch(self.language)
        ):
            raise _validation(
                "language must be empty or a bounded language tag",
                code="invalid_document_language",
                field_name="language",
            )
        freshness = _enum(
            self.freshness,
            DocumentArtifactFreshness,
            "freshness",
        )
        object.__setattr__(self, "freshness", freshness)

        if isinstance(self.lineage, (str, bytes)) or not isinstance(
            self.lineage,
            Sequence,
        ):
            raise _validation(
                "lineage must be an array",
                code="invalid_document_artifact_lineage",
                field_name="lineage",
            )
        lineage = tuple(self.lineage)
        if len(lineage) > MAX_DOCUMENT_LINEAGE_REFS or any(
            not isinstance(value, DocumentLineageRef) for value in lineage
        ):
            raise _validation(
                "lineage contains invalid or excessive references",
                code="invalid_document_artifact_lineage",
                field_name="lineage",
            )
        identities = [(value.relation, value.artifact_id) for value in lineage]
        if len(identities) != len(set(identities)) or any(
            value.artifact_id == self.key.artifact_id for value in lineage
        ):
            raise _validation(
                "lineage must contain unique external artifact references",
                code="invalid_document_artifact_lineage",
                field_name="lineage",
            )
        object.__setattr__(self, "lineage", lineage)

        if not isinstance(self.provenance, DocumentArtifactProvenance):
            raise _validation(
                "provenance must be DocumentArtifactProvenance",
                code="invalid_document_artifact_provenance",
                field_name="provenance",
            )
        object.__setattr__(self, "extensions", _extensions(self.extensions))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DOCUMENT_ARTIFACT_SCHEMA,
            "key": self.key.as_dict(),
            "revision": self.revision,
            "kind": self.kind,
            "label": self.label,
            "language": self.language,
            "resource": self.resource.as_dict(),
            "source": self.source.as_dict(),
            "freshness": self.freshness.value,
            "lineage": [value.as_dict() for value in self.lineage],
            "provenance": self.provenance.as_dict(),
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class DocumentResourcePageRequest:
    """A bounded read pinned to one artifact and resource revision."""

    key: DocumentArtifactKey
    artifact_revision: str
    resource: DocumentResourceRef
    mode: DocumentPageMode | str = DocumentPageMode.BYTES
    offset: int = 0
    max_bytes: int = MAX_DOCUMENT_RESOURCE_PAGE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.key, DocumentArtifactKey):
            raise _validation(
                "key must be DocumentArtifactKey",
                code="invalid_document_artifact_identity",
                field_name="key",
            )
        object.__setattr__(
            self,
            "artifact_revision",
            _revision(self.artifact_revision, "artifact_revision"),
        )
        if not isinstance(self.resource, DocumentResourceRef):
            raise _validation(
                "resource must be DocumentResourceRef",
                code="invalid_document_resource_page",
                field_name="resource",
            )
        object.__setattr__(
            self,
            "mode",
            _enum(self.mode, DocumentPageMode, "mode"),
        )
        object.__setattr__(
            self,
            "offset",
            _nonnegative_integer(self.offset, "offset"),
        )
        object.__setattr__(
            self,
            "max_bytes",
            _positive_page_size(self.max_bytes, "max_bytes"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA,
            "key": self.key.as_dict(),
            "artifact_revision": self.artifact_revision,
            "resource": self.resource.as_dict(),
            "mode": self.mode.value,
            "offset": self.offset,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True, slots=True)
class DocumentResourcePageView:
    """One exact page; text pages are strict UTF-8 byte slices."""

    key: DocumentArtifactKey
    artifact_revision: str
    resource: DocumentResourceRef
    mode: DocumentPageMode | str
    media_type: str
    content_sha256: str
    total_byte_size: int
    offset: int
    max_bytes: int
    content: bytes
    next_offset: int | None
    text_encoding: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.key, DocumentArtifactKey):
            raise _validation(
                "key must be DocumentArtifactKey",
                code="invalid_document_artifact_identity",
                field_name="key",
            )
        object.__setattr__(
            self,
            "artifact_revision",
            _revision(self.artifact_revision, "artifact_revision"),
        )
        if not isinstance(self.resource, DocumentResourceRef):
            raise _validation(
                "resource must be DocumentResourceRef",
                code="invalid_document_resource_page",
                field_name="resource",
            )
        mode = _enum(self.mode, DocumentPageMode, "mode")
        media_type = _media_type(self.media_type)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "media_type", media_type)
        object.__setattr__(
            self,
            "content_sha256",
            _sha256(self.content_sha256),
        )
        total_byte_size = _nonnegative_integer(
            self.total_byte_size,
            "total_byte_size",
        )
        offset = _nonnegative_integer(self.offset, "offset")
        max_bytes = _positive_page_size(self.max_bytes, "max_bytes")
        object.__setattr__(self, "total_byte_size", total_byte_size)
        object.__setattr__(self, "offset", offset)
        object.__setattr__(self, "max_bytes", max_bytes)

        # Requiring exact bytes avoids copying an arbitrarily large bytearray
        # before the page budget is checked.
        if type(self.content) is not bytes:
            raise _validation(
                "content must be immutable bytes",
                code="invalid_document_resource_page",
                field_name="content",
            )
        byte_count = len(self.content)
        if byte_count > max_bytes:
            raise _validation(
                "content exceeds the requested document page budget",
                code="invalid_document_resource_page",
                field_name="content",
            )
        if offset > total_byte_size or offset + byte_count > total_byte_size:
            raise _validation(
                "content lies outside the declared resource",
                code="invalid_document_resource_page",
                field_name="offset",
            )
        if offset < total_byte_size and byte_count == 0:
            raise _validation(
                "a nonterminal resource page must make progress",
                code="invalid_document_resource_page",
                field_name="content",
            )

        if self.next_offset is not None:
            object.__setattr__(
                self,
                "next_offset",
                _nonnegative_integer(self.next_offset, "next_offset"),
            )
        expected_next = (
            offset + byte_count if offset + byte_count < total_byte_size else None
        )
        if self.next_offset != expected_next:
            raise _validation(
                "next_offset does not match the returned byte range",
                code="invalid_document_resource_page",
                field_name="next_offset",
            )

        if self.text_encoding not in {"", "utf-8"}:
            raise _validation(
                "text_encoding must be empty or utf-8",
                code="invalid_document_text_encoding",
                field_name="text_encoding",
            )
        if mode is DocumentPageMode.TEXT:
            if self.text_encoding != "utf-8" or not _is_textual_media_type(media_type):
                raise _validation(
                    "text pages require a UTF-8 textual resource",
                    code="invalid_document_text_encoding",
                    field_name="text_encoding",
                )
            try:
                text = self.content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise _validation(
                    "text page boundaries must align to UTF-8 code points",
                    code="invalid_document_text_page_boundary",
                    field_name="content",
                ) from exc
            # This also rejects lone surrogates or unsafe controls if a custom
            # bytes subclass ever bypasses the exact-bytes check.
            _safe_text(
                text,
                "content",
                maximum=MAX_DOCUMENT_RESOURCE_PAGE_BYTES,
            )
            if len(text.encode("utf-8")) != byte_count:
                raise _validation(
                    "text page byte_count is not canonical UTF-8",
                    code="invalid_document_text_page_boundary",
                    field_name="content",
                )
        elif self.text_encoding and not _is_textual_media_type(media_type):
            raise _validation(
                "text encoding requires a textual media type",
                code="invalid_document_text_encoding",
                field_name="text_encoding",
            )

    @classmethod
    def build(
        cls,
        *,
        request: DocumentResourcePageRequest,
        media_type: str,
        content_sha256: str,
        total_byte_size: int,
        content: bytes,
        text_encoding: str = "",
    ) -> "DocumentResourcePageView":
        if not isinstance(request, DocumentResourcePageRequest):
            raise _validation(
                "request must be DocumentResourcePageRequest",
                code="invalid_document_resource_page",
                field_name="request",
            )
        if type(content) is not bytes:
            raise _validation(
                "content must be immutable bytes",
                code="invalid_document_resource_page",
                field_name="content",
            )
        total = _nonnegative_integer(total_byte_size, "total_byte_size")
        if len(content) > request.max_bytes:
            raise _validation(
                "content exceeds the requested document page budget",
                code="invalid_document_resource_page",
                field_name="content",
            )
        next_offset = (
            request.offset + len(content)
            if request.offset + len(content) < total
            else None
        )
        return cls(
            key=request.key,
            artifact_revision=request.artifact_revision,
            resource=request.resource,
            mode=request.mode,
            media_type=media_type,
            content_sha256=content_sha256,
            total_byte_size=total,
            offset=request.offset,
            max_bytes=request.max_bytes,
            content=content,
            next_offset=next_offset,
            text_encoding=text_encoding,
        )

    @property
    def byte_count(self) -> int:
        return len(self.content)

    @property
    def page_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def text(self) -> str | None:
        if self.mode is DocumentPageMode.TEXT:
            return self.content.decode("utf-8")
        return None

    def as_dict(self) -> dict[str, Any]:
        if self.mode is DocumentPageMode.TEXT:
            encoding = "utf-8"
            data = self.content.decode("utf-8")
        else:
            encoding = "base64"
            data = base64.b64encode(self.content).decode("ascii")
        return {
            "schema": DOCUMENT_RESOURCE_PAGE_SCHEMA,
            "key": self.key.as_dict(),
            "artifact_revision": self.artifact_revision,
            "resource": self.resource.as_dict(),
            "mode": self.mode.value,
            "media_type": self.media_type,
            "text_encoding": self.text_encoding,
            "content_sha256": self.content_sha256,
            "total_byte_size": self.total_byte_size,
            "offset": self.offset,
            "max_bytes": self.max_bytes,
            "byte_count": self.byte_count,
            "next_offset": self.next_offset,
            "page_sha256": self.page_sha256,
            "encoding": encoding,
            "data": data,
        }


@runtime_checkable
class DocumentArtifactProjectorPort(Protocol):
    """Read document summaries without exposing their persistence model."""

    def list_document_artifacts(
        self,
        item_id: str,
    ) -> Sequence[DocumentArtifactView]: ...

    def get_document_artifact(
        self,
        key: DocumentArtifactKey,
    ) -> DocumentArtifactView | None: ...


@runtime_checkable
class DocumentResourcePageReaderPort(Protocol):
    """Resolve one revision-pinned, bounded document resource page."""

    def read_document_resource_page(
        self,
        request: DocumentResourcePageRequest,
    ) -> DocumentResourcePageView: ...


__all__ = [
    "DOCUMENT_ARTIFACT_CONTRACT_VERSION",
    "DOCUMENT_ARTIFACT_KINDS",
    "DOCUMENT_ARTIFACT_SCHEMA",
    "DOCUMENT_ARTIFACTS_READ_CAPABILITY",
    "DOCUMENT_RESOURCE_PAGE_REQUEST_SCHEMA",
    "DOCUMENT_RESOURCE_PAGE_SCHEMA",
    "MAX_DOCUMENT_EXTENSION_DEPTH",
    "MAX_DOCUMENT_EXTENSION_ENCODED_BYTES",
    "MAX_DOCUMENT_EXTENSION_NODES",
    "MAX_DOCUMENT_EXTENSION_STRING_CHARACTERS",
    "MAX_DOCUMENT_LINEAGE_REFS",
    "MAX_DOCUMENT_RESOURCE_PAGE_BYTES",
    "DocumentArtifactFreshness",
    "DocumentArtifactKey",
    "DocumentArtifactProjectorPort",
    "DocumentArtifactProvenance",
    "DocumentArtifactView",
    "DocumentLineageRef",
    "DocumentPageMode",
    "DocumentResourcePageReaderPort",
    "DocumentResourcePageRequest",
    "DocumentResourcePageView",
    "DocumentResourceRef",
    "DocumentResourceState",
    "DocumentResourceSummary",
    "DocumentSourceRef",
]
