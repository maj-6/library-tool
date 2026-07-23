"""Portable, revisioned raster-artifact read contracts.

The contracts in this module describe public engine state, not filesystem
records.  In particular, :class:`RasterResourceRef` is an opaque identifier
that a transport may exchange for bytes; paths, URLs, and storage locators do
not belong in these views.  Adapters are responsible for resolving legacy
stores and for deriving freshness before constructing a view.

This slice is deliberately read-only.  Correction commands and persistence
are separate application-service boundaries.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from .capabilities import CapabilityRef
from .errors import ValidationError


JsonMapping: TypeAlias = Mapping[str, Any]

CORRECTIONS_WORKBENCH_ID = "corrections"
RASTER_ARTIFACTS_READ_CAPABILITY = CapabilityRef(
    "library.raster-artifacts.read",
    1,
)

IMAGE_CATEGORIES = frozenset(
    {"title_page", "cover", "spine", "content_specimen", "other"}
)
MAX_EXTENSION_DEPTH = 12
MAX_EXTENSION_NODES = 512
MAX_EXTENSION_ENCODED_BYTES = 32 * 1024
MAX_LINEAGE_REFS = 64
MAX_ASSERTIONS = 32
MAX_PORTABLE_INTEGER = (1 << 53) - 1

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_MEDIA_TYPE_RE = re.compile(
    r"^image/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$",
    re.IGNORECASE,
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_PRIVATE_EXTENSION_KEYS = frozenset(
    {
        "absolute_path",
        "asset_ref",
        "file",
        "file_name",
        "filename",
        "filepath",
        "local_path",
        "locator",
        "path",
        "resource_ref",
        "storage_key",
        "storage_locator",
        "storage_path",
        "uri",
        "url",
    }
)
_PRIVATE_EXTENSION_SUFFIXES = frozenset(
    {"file", "filename", "filepath", "locator", "path", "uri", "url"}
)
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_EXTENSION_KEY_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")


def _validation(message: str, *, code: str, field_name: str) -> ValidationError:
    return ValidationError(message, code=code, details={"field": field_name})


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _validation(
            f"{field_name} must be a portable opaque identifier",
            code="invalid_artifact_identity",
            field_name=field_name,
        )
    return value


def _resource_identifier(value: Any, field_name: str) -> str:
    result = _identifier(value, field_name)
    lowered = result.casefold()
    if (
        lowered.startswith(("file:", "http:", "https:"))
        or _WINDOWS_DRIVE_PREFIX_RE.match(result)
    ):
        raise _validation(
            f"{field_name} must not be a path or URL",
            code="unsafe_artifact_resource_ref",
            field_name=field_name,
        )
    return result


def _revision(value: Any, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise _validation(
            f"{field_name} must be a revision token",
            code="invalid_artifact_revision",
            field_name=field_name,
        )
    return value


def _optional_revision(value: Any, field_name: str) -> str:
    if value == "":
        return ""
    return _revision(value, field_name)


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
            code="invalid_artifact_contract",
            field_name=field_name,
        )
    if not allow_empty and not value.strip():
        raise _validation(
            f"{field_name} must not be empty",
            code="invalid_artifact_contract",
            field_name=field_name,
        )
    if any(
        ord(character) == 127
        or (ord(character) < 32 and character not in "\n\r\t")
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise _validation(
            f"{field_name} contains an unsafe character",
            code="invalid_artifact_contract",
            field_name=field_name,
        )
    return value


def _positive_integer(value: Any, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_PORTABLE_INTEGER
    ):
        raise _validation(
            f"{field_name} must be a positive portable integer",
            code="invalid_raster_dimensions",
            field_name=field_name,
        )
    return value


def _confidence(value: Any, field_name: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _validation(
            f"{field_name} must be between zero and one",
            code="invalid_artifact_assignment",
            field_name=field_name,
        )
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 1:
        raise _validation(
            f"{field_name} must be between zero and one",
            code="invalid_artifact_assignment",
            field_name=field_name,
        )
    if number.is_integer():
        return int(number)
    return number


def _private_extension_key(key: str) -> bool:
    separated = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", key)
    separated = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", separated)
    normalized = _EXTENSION_KEY_SEPARATOR_RE.sub("_", separated).strip("_").casefold()
    if normalized in _PRIVATE_EXTENSION_KEYS:
        return True
    return normalized.rsplit("_", 1)[-1] in _PRIVATE_EXTENSION_SUFFIXES


def _freeze_json(
    value: Any,
    *,
    path: str,
    depth: int = 0,
    active: set[int] | None = None,
    budget: list[int] | None = None,
) -> Any:
    if depth > MAX_EXTENSION_DEPTH:
        raise _validation(
            "extension metadata is nested too deeply",
            code="invalid_artifact_extensions",
            field_name=path,
        )
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > MAX_EXTENSION_NODES:
        raise _validation(
            "extension metadata has too many values",
            code="invalid_artifact_extensions",
            field_name=path,
        )

    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                _safe_text(value, path, maximum=8192)
            except ValidationError as exc:
                raise _validation(
                    "extension metadata contains an invalid string",
                    code="invalid_artifact_extensions",
                    field_name=path,
                ) from exc
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_PORTABLE_INTEGER:
            raise _validation(
                "extension metadata contains a non-portable integer",
                code="invalid_artifact_extensions",
                field_name=path,
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation(
                "extension metadata contains a non-finite number",
                code="invalid_artifact_extensions",
                field_name=path,
            )
        return value

    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise _validation(
                "extension metadata contains a reference cycle",
                code="invalid_artifact_extensions",
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
                        code="invalid_artifact_extensions",
                        field_name=path,
                    )
                _safe_text(key, f"{path} key", maximum=128, allow_empty=False)
                if _private_extension_key(key):
                    raise _validation(
                        "extension metadata contains a private resource field",
                        code="private_artifact_extension",
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
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        identity = id(value)
        if identity in active:
            raise _validation(
                "extension metadata contains a reference cycle",
                code="invalid_artifact_extensions",
                field_name=path,
            )
        active.add(identity)
        try:
            if len(value) > MAX_EXTENSION_NODES:
                raise _validation(
                    "extension metadata has too many values",
                    code="invalid_artifact_extensions",
                    field_name=path,
                )
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
        code="invalid_artifact_extensions",
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
            code="invalid_artifact_extensions",
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
            code="invalid_artifact_extensions",
            field_name=field_name,
        ) from exc
    if len(encoded) > MAX_EXTENSION_ENCODED_BYTES:
        raise _validation(
            f"{field_name} exceeds its encoded size budget",
            code="invalid_artifact_extensions",
            field_name=field_name,
        )
    return frozen


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise _validation(
            f"{field_name} is invalid",
            code="invalid_artifact_contract",
            field_name=field_name,
        ) from exc


def _typed_values(
    value: Any,
    item_type: type,
    field_name: str,
    *,
    maximum: int,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _validation(
            f"{field_name} must be an array",
            code="invalid_artifact_contract",
            field_name=field_name,
        )
    values = tuple(value)
    if len(values) > maximum or any(
        not isinstance(item, item_type) for item in values
    ):
        raise _validation(
            f"{field_name} contains invalid values",
            code="invalid_artifact_contract",
            field_name=field_name,
        )
    return values


class ArtifactFreshness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNTRACKED = "untracked"


class ResourceState(str, Enum):
    AVAILABLE = "available"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class AssignmentOrigin(str, Enum):
    MANUAL = "manual"
    INHERITED = "inherited"
    SUGGESTED = "suggested"


class CaptionOrigin(str, Enum):
    MANUAL = "manual"
    MACHINE = "machine"
    INHERITED = "inherited"
    IMPORTED = "imported"


@dataclass(frozen=True, slots=True)
class RasterArtifactKey:
    item_id: str
    artifact_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _identifier(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "artifact_id",
            _identifier(self.artifact_id, "artifact_id"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "artifact_id": self.artifact_id}


@dataclass(frozen=True, slots=True)
class RasterResourceRef:
    """Opaque read reference resolved by a trusted transport adapter."""

    resource_id: str
    revision: str
    variant: str = "display"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "resource_id",
            _resource_identifier(self.resource_id, "resource_id"),
        )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        object.__setattr__(self, "variant", _identifier(self.variant, "variant"))

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.resource_id,
            "revision": self.revision,
            "variant": self.variant,
        }


@dataclass(frozen=True, slots=True)
class RasterDimensions:
    width: int
    height: int
    orientation: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "width", _positive_integer(self.width, "width"))
        object.__setattr__(
            self,
            "height",
            _positive_integer(self.height, "height"),
        )
        if (
            isinstance(self.orientation, bool)
            or not isinstance(self.orientation, int)
            or self.orientation not in range(1, 9)
        ):
            raise _validation(
                "orientation must be an EXIF orientation from 1 through 8",
                code="invalid_raster_dimensions",
                field_name="orientation",
            )

    def as_dict(self) -> dict[str, int]:
        return {
            "width": self.width,
            "height": self.height,
            "orientation": self.orientation,
        }


@dataclass(frozen=True, slots=True)
class RasterSourceRef:
    representation_id: str
    representation_revision: str
    canvas_id: str = ""
    canvas_revision: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "representation_id",
            _identifier(self.representation_id, "representation_id"),
        )
        object.__setattr__(
            self,
            "representation_revision",
            _revision(self.representation_revision, "representation_revision"),
        )
        if bool(self.canvas_id) != bool(self.canvas_revision):
            raise _validation(
                "canvas identity and revision must be supplied together",
                code="invalid_artifact_source",
                field_name="canvas",
            )
        if self.canvas_id:
            object.__setattr__(
                self,
                "canvas_id",
                _identifier(self.canvas_id, "canvas_id"),
            )
            object.__setattr__(
                self,
                "canvas_revision",
                _revision(self.canvas_revision, "canvas_revision"),
            )

    def as_dict(self) -> dict[str, str]:
        value = {
            "representation_id": self.representation_id,
            "representation_revision": self.representation_revision,
        }
        if self.canvas_id:
            value.update(
                {
                    "canvas_id": self.canvas_id,
                    "canvas_revision": self.canvas_revision,
                }
            )
        return value


@dataclass(frozen=True, slots=True)
class RasterLineageRef:
    artifact_id: str
    artifact_revision: str
    relation: str = "derived_from"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_id",
            _identifier(self.artifact_id, "lineage.artifact_id"),
        )
        object.__setattr__(
            self,
            "artifact_revision",
            _revision(self.artifact_revision, "lineage.artifact_revision"),
        )
        object.__setattr__(
            self,
            "relation",
            _identifier(self.relation, "lineage.relation"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_revision": self.artifact_revision,
            "relation": self.relation,
        }


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    origin: str = "unknown"
    provider_id: str = ""
    model: str = ""
    recipe_revision: str = ""
    operation_id: str = ""
    generated_at: str = ""
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", _identifier(self.origin, "origin"))
        if self.provider_id:
            object.__setattr__(
                self,
                "provider_id",
                _identifier(self.provider_id, "provider_id"),
            )
        object.__setattr__(self, "model", _safe_text(self.model, "model", maximum=256))
        object.__setattr__(
            self,
            "recipe_revision",
            _optional_revision(self.recipe_revision, "recipe_revision"),
        )
        if self.operation_id:
            object.__setattr__(
                self,
                "operation_id",
                _identifier(self.operation_id, "operation_id"),
            )
        object.__setattr__(
            self,
            "generated_at",
            _safe_text(self.generated_at, "generated_at", maximum=128),
        )
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
class CategoryAssignment:
    category: str
    origin: AssignmentOrigin | str
    revision: str
    inherited_from_artifact_id: str = ""
    confidence: int | float | None = None
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if self.category not in IMAGE_CATEGORIES:
            raise _validation(
                "category is not in the canonical image vocabulary",
                code="invalid_artifact_assignment",
                field_name="category",
            )
        origin = _enum(self.origin, AssignmentOrigin, "origin")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        inherited_from = self.inherited_from_artifact_id
        if origin is AssignmentOrigin.INHERITED:
            inherited_from = _identifier(
                inherited_from,
                "inherited_from_artifact_id",
            )
        elif inherited_from:
            raise _validation(
                "only inherited assignments may name an inherited source",
                code="invalid_artifact_assignment",
                field_name="inherited_from_artifact_id",
            )
        object.__setattr__(self, "inherited_from_artifact_id", inherited_from)
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "confidence"),
        )
        if not isinstance(self.provenance, ArtifactProvenance):
            raise _validation(
                "provenance must be ArtifactProvenance",
                code="invalid_artifact_assignment",
                field_name="provenance",
            )
        object.__setattr__(
            self,
            "extensions",
            _extensions(self.extensions, "category.extensions"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "origin": self.origin.value,
            "revision": self.revision,
            "inherited_from_artifact_id": self.inherited_from_artifact_id,
            "confidence": self.confidence,
            "provenance": self.provenance.as_dict(),
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class CaptionAssertion:
    text: str
    origin: CaptionOrigin | str
    revision: str
    language: str = ""
    source_annotation_id: str = ""
    confidence: int | float | None = None
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "text",
            _safe_text(self.text, "caption.text", maximum=16_384, allow_empty=False),
        )
        object.__setattr__(self, "origin", _enum(self.origin, CaptionOrigin, "origin"))
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        if self.language and not _LANGUAGE_RE.fullmatch(self.language):
            raise _validation(
                "language must be an empty string or a language tag",
                code="invalid_caption_assertion",
                field_name="language",
            )
        if self.source_annotation_id:
            object.__setattr__(
                self,
                "source_annotation_id",
                _identifier(self.source_annotation_id, "source_annotation_id"),
            )
        object.__setattr__(
            self,
            "confidence",
            _confidence(self.confidence, "confidence"),
        )
        if not isinstance(self.provenance, ArtifactProvenance):
            raise _validation(
                "provenance must be ArtifactProvenance",
                code="invalid_caption_assertion",
                field_name="provenance",
            )
        object.__setattr__(
            self,
            "extensions",
            _extensions(self.extensions, "caption.extensions"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "origin": self.origin.value,
            "revision": self.revision,
            "language": self.language,
            "source_annotation_id": self.source_annotation_id,
            "confidence": self.confidence,
            "provenance": self.provenance.as_dict(),
            "extensions": _thaw(self.extensions),
        }


@dataclass(frozen=True, slots=True)
class RasterArtifactView:
    key: RasterArtifactKey
    revision: str
    kind: str
    media_type: str
    content_sha256: str
    dimensions: RasterDimensions
    source: RasterSourceRef
    resource_state: ResourceState | str
    resource: RasterResourceRef | None = None
    label: str = ""
    freshness: ArtifactFreshness | str = ArtifactFreshness.UNTRACKED
    lineage: tuple[RasterLineageRef, ...] = ()
    category_assignments: tuple[CategoryAssignment, ...] = ()
    caption_assertions: tuple[CaptionAssertion, ...] = ()
    provenance: ArtifactProvenance = field(default_factory=ArtifactProvenance)
    extensions: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if not isinstance(self.key, RasterArtifactKey):
            raise _validation(
                "key must be RasterArtifactKey",
                code="invalid_artifact_identity",
                field_name="key",
            )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        object.__setattr__(self, "kind", _identifier(self.kind, "kind"))
        if not isinstance(self.media_type, str) or not _MEDIA_TYPE_RE.fullmatch(
            self.media_type
        ) or self.media_type.casefold() == "image/svg+xml":
            raise _validation(
                "media_type must identify a raster image",
                code="invalid_raster_media_type",
                field_name="media_type",
            )
        object.__setattr__(self, "media_type", self.media_type.casefold())
        if not isinstance(self.content_sha256, str) or not _SHA256_RE.fullmatch(
            self.content_sha256
        ):
            raise _validation(
                "content_sha256 must be a SHA-256 digest",
                code="invalid_artifact_checksum",
                field_name="content_sha256",
            )
        object.__setattr__(self, "content_sha256", self.content_sha256.casefold())
        if not isinstance(self.dimensions, RasterDimensions):
            raise _validation(
                "dimensions must be RasterDimensions",
                code="invalid_raster_dimensions",
                field_name="dimensions",
            )
        if not isinstance(self.source, RasterSourceRef):
            raise _validation(
                "source must be RasterSourceRef",
                code="invalid_artifact_source",
                field_name="source",
            )
        resource_state = _enum(self.resource_state, ResourceState, "resource_state")
        freshness = _enum(self.freshness, ArtifactFreshness, "freshness")
        object.__setattr__(self, "resource_state", resource_state)
        object.__setattr__(self, "freshness", freshness)
        if resource_state is ResourceState.AVAILABLE:
            if not isinstance(self.resource, RasterResourceRef):
                raise _validation(
                    "available artifacts require an opaque resource reference",
                    code="invalid_artifact_resource_state",
                    field_name="resource",
                )
        elif self.resource is not None:
            raise _validation(
                "missing or unavailable artifacts cannot expose a resource",
                code="invalid_artifact_resource_state",
                field_name="resource",
            )
        object.__setattr__(self, "label", _safe_text(self.label, "label", maximum=512))

        lineage = _typed_values(
            self.lineage,
            RasterLineageRef,
            "lineage",
            maximum=MAX_LINEAGE_REFS,
        )
        lineage_keys = [(value.relation, value.artifact_id) for value in lineage]
        if len(lineage_keys) != len(set(lineage_keys)) or any(
            value.artifact_id == self.key.artifact_id for value in lineage
        ):
            raise _validation(
                "lineage must contain unique external artifact references",
                code="invalid_artifact_lineage",
                field_name="lineage",
            )
        object.__setattr__(self, "lineage", lineage)

        categories = _typed_values(
            self.category_assignments,
            CategoryAssignment,
            "category_assignments",
            maximum=len(AssignmentOrigin),
        )
        category_origins = [assignment.origin for assignment in categories]
        if len(category_origins) != len(set(category_origins)):
            raise _validation(
                "category assignments must have unique origins",
                code="invalid_artifact_assignment",
                field_name="category_assignments",
            )
        object.__setattr__(self, "category_assignments", categories)

        captions = _typed_values(
            self.caption_assertions,
            CaptionAssertion,
            "caption_assertions",
            maximum=MAX_ASSERTIONS,
        )
        caption_origins = [caption.origin for caption in captions]
        if len(caption_origins) != len(set(caption_origins)):
            raise _validation(
                "caption assertions must have unique origins",
                code="invalid_caption_assertion",
                field_name="caption_assertions",
            )
        object.__setattr__(self, "caption_assertions", captions)
        if not isinstance(self.provenance, ArtifactProvenance):
            raise _validation(
                "provenance must be ArtifactProvenance",
                code="invalid_artifact_contract",
                field_name="provenance",
            )
        object.__setattr__(self, "extensions", _extensions(self.extensions))

    @property
    def effective_category(self) -> str:
        by_origin = {value.origin: value for value in self.category_assignments}
        for origin in (
            AssignmentOrigin.MANUAL,
            AssignmentOrigin.INHERITED,
            AssignmentOrigin.SUGGESTED,
        ):
            if origin in by_origin:
                return by_origin[origin].category
        return "other"

    @property
    def effective_caption(self) -> CaptionAssertion | None:
        by_origin = {value.origin: value for value in self.caption_assertions}
        for origin in (
            CaptionOrigin.MANUAL,
            CaptionOrigin.IMPORTED,
            CaptionOrigin.INHERITED,
            CaptionOrigin.MACHINE,
        ):
            if origin in by_origin:
                return by_origin[origin]
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "revision": self.revision,
            "kind": self.kind,
            "label": self.label,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "dimensions": self.dimensions.as_dict(),
            "source": self.source.as_dict(),
            "resource_state": self.resource_state.value,
            "resource": self.resource.as_dict() if self.resource else None,
            "freshness": self.freshness.value,
            "lineage": [value.as_dict() for value in self.lineage],
            "category_assignments": [
                value.as_dict() for value in self.category_assignments
            ],
            "effective_category": self.effective_category,
            "caption_assertions": [
                value.as_dict() for value in self.caption_assertions
            ],
            "effective_caption": (
                self.effective_caption.as_dict() if self.effective_caption else None
            ),
            "provenance": self.provenance.as_dict(),
            "extensions": _thaw(self.extensions),
        }


@runtime_checkable
class RasterArtifactProjectorPort(Protocol):
    """Read/project raster state without exposing its persistence model."""

    def list_raster_artifacts(self, item_id: str) -> Sequence[RasterArtifactView]: ...

    def get_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None: ...


__all__ = [
    "ArtifactFreshness",
    "ArtifactProvenance",
    "AssignmentOrigin",
    "CORRECTIONS_WORKBENCH_ID",
    "CaptionAssertion",
    "CaptionOrigin",
    "CategoryAssignment",
    "IMAGE_CATEGORIES",
    "RASTER_ARTIFACTS_READ_CAPABILITY",
    "RasterArtifactKey",
    "RasterArtifactProjectorPort",
    "RasterArtifactView",
    "RasterDimensions",
    "RasterLineageRef",
    "RasterResourceRef",
    "RasterSourceRef",
    "ResourceState",
]
