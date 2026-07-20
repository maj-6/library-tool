"""Revisioned, framework-neutral text-layer aggregates.

This module deliberately does not replace :mod:`librarytool.engine.text_layers`.
That older service is a compatibility helper used by the current Replica
pipeline.  The contracts below are the durable boundary that future browser,
Qt, Godot, command-line, OCR, and research clients can share.

The engine owns semantic validation, optimistic concurrency, and idempotency.
Repositories own identity allocation, source lookup, serialization, locking,
and atomic publication.  In particular, a command unit of work must expose a
durable receipt *before* it consults live item, source, or document state.  An
exact retry can therefore replay after those live resources have moved or
disappeared without repeating the mutation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import unicodedata
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import (
    Any,
    ContextManager,
    Literal,
    Protocol,
    TypeAlias,
    TypeVar,
)

from .errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)


JsonMapping: TypeAlias = Mapping[str, Any]
TextLayerMutationAction: TypeAlias = Literal[
    "create", "replace-unit", "replace-batch"
]
TextLayerOrigin: TypeAlias = Literal[
    "unknown", "machine", "human", "import", "derived"
]
TextLayerReviewState: TypeAlias = Literal[
    "unreviewed", "reviewed", "approved", "rejected"
]
TextLayerSourceStatus: TypeAlias = Literal[
    "current", "stale", "unavailable"
]

_T = TypeVar("_T")

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ORIGINS = frozenset({"unknown", "machine", "human", "import", "derived"})
_REVIEW_STATES = frozenset(
    {"unreviewed", "reviewed", "approved", "rejected"}
)
_ACTIONS = frozenset({"create", "replace-unit", "replace-batch"})

# These are contract-level denial-of-service limits, not storage-format limits.
# An adapter may impose a lower bound appropriate to its medium.
MAX_TEXT_UNIT_CHARACTERS = 32 * 1024 * 1024
MAX_TEXT_LAYER_CHARACTERS = 256 * 1024 * 1024
MAX_TEXT_LAYER_UNITS = 100_000
MAX_TEXT_LAYER_BATCH_REPLACEMENTS = 4_096
MAX_TEXT_LAYER_BATCH_CHARACTERS = 32 * 1024 * 1024
MAX_TEXT_LAYER_RECEIPT_UNITS = MAX_TEXT_LAYER_BATCH_REPLACEMENTS
# Bound picker/list projections and their per-layer source lookups even when a
# broken adapter reports a lazy or dishonest Sequence.
MAX_TEXT_LAYERS_PER_ITEM = 10_000
MAX_TEXT_LAYER_METADATA_DEPTH = 16
MAX_TEXT_LAYER_METADATA_NODES = 4_096
MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS = 65_536
MAX_TEXT_LAYER_METADATA_ENCODED_BYTES = 256 * 1024
# Whole-command provenance is much smaller than the document text budget.
# This prevents valid per-unit metadata from multiplying into GiB-scale create
# or batch hashes while still allowing extensive per-unit research evidence.
MAX_TEXT_LAYER_PROVENANCE_ENCODED_BYTES = 16 * 1024 * 1024
MAX_PORTABLE_JSON_INTEGER = (1 << 53) - 1

TEXT_LAYER_RECEIPT_STORAGE_SCHEMA = "librarytool.text-layer-mutation-receipt"
TEXT_LAYER_RECEIPT_STORAGE_VERSION = 1


def _safe_string(
    value: Any,
    field_name: str,
    *,
    maximum: int | None = None,
    text: bool = False,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{field_name} contains an unpaired surrogate")
    if any(
        ord(character) == 127
        or (ord(character) < 32 and character not in "\n\r\t")
        for character in value
    ):
        raise ValueError(f"{field_name} contains a control character")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field_name} is too long")
    # ``text`` is intentionally not normalized or stripped.  The named flag
    # documents the call site's intent and guards against a future helper
    # refactor accidentally applying descriptor normalization to content.
    if text:
        return value
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


def _revision(value: Any, field_name: str, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if optional and not value:
        return value
    _safe_string(value, field_name, maximum=512)
    if (
        not value
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise ValueError(f"{field_name} is not a valid revision token")
    return value


def _language(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("language must be a string")
    if value and not _LANGUAGE_RE.fullmatch(value):
        raise ValueError("language must be an empty string or a language tag")
    return value


def _freeze_json(
    value: Any,
    *,
    path: str = "$",
    active: set[int] | None = None,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    """Detach strict JSON into mappings and tuples callers cannot mutate."""

    if depth > MAX_TEXT_LAYER_METADATA_DEPTH:
        raise ValueError(f"{path} exceeds the metadata depth budget")
    if budget is None:
        # Mutable counters avoid allocating a state object at every node:
        # [value nodes, raw UTF-8 bytes across keys and string values].
        budget = [0, 0]
    budget[0] += 1
    if budget[0] > MAX_TEXT_LAYER_METADATA_NODES:
        raise ValueError(f"{path} exceeds the metadata node budget")

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_PORTABLE_JSON_INTEGER <= value <= MAX_PORTABLE_JSON_INTEGER:
            raise ValueError(f"{path} contains a non-portable integer")
        return value
    if isinstance(value, str):
        result = _safe_string(
            value,
            path,
            maximum=MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS,
        )
        budget[1] += len(result.encode("utf-8"))
        if budget[1] > MAX_TEXT_LAYER_METADATA_ENCODED_BYTES:
            raise ValueError(f"{path} exceeds the metadata size budget")
        return result
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        if not value.is_integer():
            raise ValueError(f"{path} contains a non-integral JSON number")
        portable = int(value)
        if not -MAX_PORTABLE_JSON_INTEGER <= portable <= MAX_PORTABLE_JSON_INTEGER:
            raise ValueError(f"{path} contains a non-portable integer")
        # JSON clients disagree about 1 vs 1.0 and negative zero.  Normalize
        # integral floats to the one portable integer representation so value
        # equality and content-addressed identity cannot diverge.
        return portable
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} contains a non-string object key")
                _safe_string(key, f"{path} object key", maximum=256)
                budget[1] += len(key.encode("utf-8"))
                if budget[1] > MAX_TEXT_LAYER_METADATA_ENCODED_BYTES:
                    raise ValueError(f"{path} exceeds the metadata size budget")
                frozen[key] = _freeze_json(
                    item,
                    path=f"{path}.{key}",
                    active=active,
                    depth=depth + 1,
                    budget=budget,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            return tuple(
                _freeze_json(
                    item,
                    path=f"{path}[{index}]",
                    active=active,
                    depth=depth + 1,
                    budget=budget,
                )
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise TypeError(f"{path} contains non-JSON value {type(value).__name__}")


def _freeze_mapping(value: Any, *, path: str) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be an object")
    frozen = _freeze_json(value, path=path)
    assert isinstance(frozen, Mapping)
    if len(_canonical(frozen)) > MAX_TEXT_LAYER_METADATA_ENCODED_BYTES:
        raise ValueError(f"{path} exceeds the metadata encoded-size budget")
    return frozen


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


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
        raise ValueError("value is not canonical JSON") from exc


def _digest(prefix: str, value: Any) -> str:
    _identifier(prefix, "revision prefix")
    return f"{prefix}-" + hashlib.sha256(_canonical(value)).hexdigest()


def _typed_tuple(
    value: Any,
    item_type: type,
    field_name: str,
    *,
    maximum: int | None = None,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{field_name} must be a sequence")
    reported_length = len(value)
    if maximum is not None and reported_length > maximum:
        raise ValueError(f"{field_name} has too many values")
    if maximum is None:
        result = tuple(value)
    else:
        # Cap traversal as well as the reported length.  A hostile or broken
        # Sequence cannot bypass the budget by lying in __len__.
        result = tuple(islice(iter(value), maximum + 1))
        if len(result) > maximum:
            raise ValueError(f"{field_name} has too many values")
        if len(result) != reported_length:
            raise ValueError(f"{field_name} has inconsistent length")
    if any(not isinstance(item, item_type) for item in result):
        raise TypeError(f"{field_name} contains an invalid value")
    return result


@dataclass(frozen=True, slots=True, eq=False)
class TextLayerProvenance:
    """Portable authorship and review evidence for one exact text unit."""

    origin: TextLayerOrigin = "unknown"
    review_state: TextLayerReviewState = "unreviewed"
    provider_id: str = ""
    model: str = ""
    recipe_revision: str = ""
    updated_at: str = ""
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if self.origin not in _ORIGINS:
            raise ValueError("origin is invalid")
        if self.review_state not in _REVIEW_STATES:
            raise ValueError("review_state is invalid")
        if self.provider_id:
            _identifier(self.provider_id, "provider_id")
        _safe_string(self.model, "model", maximum=256)
        _revision(self.recipe_revision, "recipe_revision", optional=True)
        _safe_string(self.updated_at, "updated_at", maximum=128)
        object.__setattr__(
            self,
            "metadata",
            _freeze_mapping(self.metadata, path="$.provenance.metadata"),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "TextLayerProvenance":
        if not isinstance(value, Mapping):
            raise TypeError("provenance must be an object")
        fields = {
            "origin",
            "review_state",
            "provider_id",
            "model",
            "recipe_revision",
            "updated_at",
            "metadata",
        }
        if set(value) != fields:
            raise ValueError("provenance fields do not match the schema")
        return cls(
            origin=value["origin"],
            review_state=value["review_state"],
            provider_id=value["provider_id"],
            model=value["model"],
            recipe_revision=value["recipe_revision"],
            updated_at=value["updated_at"],
            metadata=value["metadata"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "review_state": self.review_state,
            "provider_id": self.provider_id,
            "model": self.model,
            "recipe_revision": self.recipe_revision,
            "updated_at": self.updated_at,
            "metadata": _thaw(self.metadata),
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TextLayerProvenance):
            return NotImplemented
        # Mapping equality in Python equates True with 1.  Canonical JSON does
        # not, so provenance equality must use the same type-strict identity
        # representation as revision hashing.
        return _canonical(self.as_dict()) == _canonical(other.as_dict())

    def __hash__(self) -> int:
        return hash(_canonical(self.as_dict()))


def _require_provenance_budget(
    provenances: Sequence[TextLayerProvenance],
    *,
    field_name: str,
) -> None:
    """Bound aggregate serialized provenance before command materialization."""

    total = 0
    size_by_identity: dict[int, int] = {}
    for provenance in provenances:
        identity = id(provenance)
        encoded_size = size_by_identity.get(identity)
        if encoded_size is None:
            encoded_size = len(_canonical(provenance.as_dict()))
            size_by_identity[identity] = encoded_size
        # Add the size for every occurrence.  Reusing one immutable provenance
        # object still serializes it once per unit/replacement in the command.
        total += encoded_size
        if total > MAX_TEXT_LAYER_PROVENANCE_ENCODED_BYTES:
            raise ValueError(f"{field_name} provenance is too large")


@dataclass(frozen=True, slots=True)
class TextLayerSourcePin:
    """The exact representation revision against which a layer was made."""

    representation_id: str
    revision: str

    def __post_init__(self) -> None:
        _identifier(self.representation_id, "representation_id")
        _revision(self.revision, "source revision")

    @classmethod
    def from_dict(cls, value: Any) -> "TextLayerSourcePin":
        if not isinstance(value, Mapping) or set(value) != {
            "representation_id",
            "revision",
        }:
            raise ValueError("source pin fields do not match the schema")
        return cls(
            representation_id=value["representation_id"],
            revision=value["revision"],
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "representation_id": self.representation_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class TextLayerSourceSnapshot:
    """Repository projection of one currently attached source revision."""

    item_id: str
    representation_id: str
    revision: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _identifier(self.representation_id, "representation_id")
        _revision(self.revision, "source revision")

    def as_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class TextLayerSourceView:
    """Pinned and live source state; freshness is never caller supplied."""

    representation_id: str
    pinned_revision: str
    current_revision: str = ""
    available: bool = False

    def __post_init__(self) -> None:
        _identifier(self.representation_id, "representation_id")
        _revision(self.pinned_revision, "pinned_revision")
        _revision(self.current_revision, "current_revision", optional=True)
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")
        if self.available != bool(self.current_revision):
            raise ValueError("available must agree with current_revision")

    @property
    def status(self) -> TextLayerSourceStatus:
        if not self.available:
            return "unavailable"
        if self.current_revision != self.pinned_revision:
            return "stale"
        return "current"

    def as_dict(self) -> dict[str, Any]:
        return {
            "representation_id": self.representation_id,
            "pinned_revision": self.pinned_revision,
            "current_revision": self.current_revision,
            "available": self.available,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class TextLayerUnitDraft:
    """One opaque, ordered text unit without repository-assigned revisions."""

    selector: str
    order: int
    text: str
    label: str = ""
    provenance: TextLayerProvenance = field(default_factory=TextLayerProvenance)

    def __post_init__(self) -> None:
        _identifier(self.selector, "selector")
        if (
            not isinstance(self.order, int)
            or isinstance(self.order, bool)
            or self.order < 0
            or self.order > MAX_PORTABLE_JSON_INTEGER
        ):
            raise ValueError("order must be a portable non-negative integer")
        _safe_string(
            self.text,
            "text",
            maximum=MAX_TEXT_UNIT_CHARACTERS,
            text=True,
        )
        _safe_string(self.label, "label", maximum=512)
        if not isinstance(self.provenance, TextLayerProvenance):
            raise TypeError("provenance must be TextLayerProvenance")

    @classmethod
    def from_dict(cls, value: Any) -> "TextLayerUnitDraft":
        if not isinstance(value, Mapping):
            raise TypeError("text unit must be an object")
        if set(value) != {"selector", "order", "label", "text", "provenance"}:
            raise ValueError("text unit fields do not match the schema")
        return cls(
            selector=value["selector"],
            order=value["order"],
            label=value["label"],
            text=value["text"],
            provenance=TextLayerProvenance.from_dict(value["provenance"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "order": self.order,
            "label": self.label,
            "text": self.text,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class TextLayerDraft:
    """Complete canonical text-layer content accepted at creation/replacement."""

    source: TextLayerSourcePin
    units: tuple[TextLayerUnitDraft, ...] = ()
    label: str = ""
    kind: str = "transcription"
    language: str = ""
    preamble: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, TextLayerSourcePin):
            raise TypeError("source must be TextLayerSourcePin")
        _safe_string(self.label, "label", maximum=512)
        _identifier(self.kind, "text layer kind")
        _language(self.language)
        _safe_string(
            self.preamble,
            "preamble",
            maximum=MAX_TEXT_UNIT_CHARACTERS,
            text=True,
        )
        units = _typed_tuple(
            self.units,
            TextLayerUnitDraft,
            "units",
            maximum=MAX_TEXT_LAYER_UNITS,
        )
        selectors = [value.selector for value in units]
        orders = [value.order for value in units]
        if len(selectors) != len(set(selectors)):
            raise ValueError("text layer selectors must be unique")
        if len(orders) != len(set(orders)):
            raise ValueError("text layer orders must be unique")
        if len(self.preamble) + sum(len(value.text) for value in units) > (
            MAX_TEXT_LAYER_CHARACTERS
        ):
            raise ValueError("the text layer is too large")
        _require_provenance_budget(
            tuple(value.provenance for value in units),
            field_name="text layer",
        )
        object.__setattr__(
            self,
            "units",
            tuple(sorted(units, key=lambda value: (value.order, value.selector))),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "kind": self.kind,
            "language": self.language,
            "source": self.source.as_dict(),
            "preamble": self.preamble,
            "units": [value.as_dict() for value in self.units],
        }


def _unit_content_revision(text: str) -> str:
    return _digest("tuc", {"text": text})


def _unit_revision(draft: TextLayerUnitDraft) -> str:
    return _digest("tur", draft.as_dict())


@dataclass(frozen=True, slots=True)
class TextLayerUnitSnapshot:
    """One immutable text unit with independent content and record CAS."""

    selector: str
    order: int
    text: str
    content_revision: str
    unit_revision: str
    label: str = ""
    provenance: TextLayerProvenance = field(default_factory=TextLayerProvenance)

    def __post_init__(self) -> None:
        draft = TextLayerUnitDraft(
            selector=self.selector,
            order=self.order,
            text=self.text,
            label=self.label,
            provenance=self.provenance,
        )
        _revision(self.content_revision, "unit content revision")
        _revision(self.unit_revision, "unit revision")
        if self.content_revision != _unit_content_revision(draft.text):
            raise ValueError("unit content revision does not match its text")
        if self.unit_revision != _unit_revision(draft):
            raise ValueError("unit revision does not match its record")

    @classmethod
    def build(cls, draft: TextLayerUnitDraft) -> "TextLayerUnitSnapshot":
        if not isinstance(draft, TextLayerUnitDraft):
            raise TypeError("draft must be TextLayerUnitDraft")
        return cls(
            selector=draft.selector,
            order=draft.order,
            text=draft.text,
            label=draft.label,
            provenance=draft.provenance,
            content_revision=_unit_content_revision(draft.text),
            unit_revision=_unit_revision(draft),
        )

    def as_draft(self) -> TextLayerUnitDraft:
        return TextLayerUnitDraft(
            selector=self.selector,
            order=self.order,
            text=self.text,
            label=self.label,
            provenance=self.provenance,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.as_draft().as_dict(),
            "content_revision": self.content_revision,
            "unit_revision": self.unit_revision,
        }


def _document_content_payload(draft: TextLayerDraft) -> dict[str, Any]:
    return {
        "preamble": draft.preamble,
        "units": [
            {
                "selector": value.selector,
                "order": value.order,
                "text": value.text,
            }
            for value in draft.units
        ],
    }


def _document_content_revision(draft: TextLayerDraft) -> str:
    return _digest("tlc", _document_content_payload(draft))


def _document_revision(item_id: str, layer_id: str, draft: TextLayerDraft) -> str:
    return _digest(
        "tld",
        {
            "item_id": item_id,
            "layer_id": layer_id,
            "document": draft.as_dict(),
        },
    )


@dataclass(frozen=True, slots=True)
class TextLayerDocumentSnapshot:
    """Canonical repository form of one revisioned text-layer document."""

    item_id: str
    layer_id: str
    source: TextLayerSourcePin
    units: tuple[TextLayerUnitSnapshot, ...]
    document_revision: str
    content_revision: str
    label: str = ""
    kind: str = "transcription"
    language: str = ""
    preamble: str = ""

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _identifier(self.layer_id, "layer_id")
        units = _typed_tuple(
            self.units,
            TextLayerUnitSnapshot,
            "units",
            maximum=MAX_TEXT_LAYER_UNITS,
        )
        draft = TextLayerDraft(
            source=self.source,
            units=tuple(value.as_draft() for value in units),
            label=self.label,
            kind=self.kind,
            language=self.language,
            preamble=self.preamble,
        )
        normalized = tuple(
            TextLayerUnitSnapshot.build(value) for value in draft.units
        )
        if units != normalized:
            raise ValueError("text layer units are not in canonical order")
        _revision(self.document_revision, "document_revision")
        _revision(self.content_revision, "content_revision")
        if self.content_revision != _document_content_revision(draft):
            raise ValueError("content_revision does not match the document")
        if self.document_revision != _document_revision(
            self.item_id, self.layer_id, draft
        ):
            raise ValueError("document_revision does not match the document")
        object.__setattr__(self, "units", units)

    @classmethod
    def build(
        cls,
        item_id: str,
        layer_id: str,
        draft: TextLayerDraft,
    ) -> "TextLayerDocumentSnapshot":
        if not isinstance(draft, TextLayerDraft):
            raise TypeError("draft must be TextLayerDraft")
        return cls(
            item_id=item_id,
            layer_id=layer_id,
            source=draft.source,
            units=tuple(TextLayerUnitSnapshot.build(value) for value in draft.units),
            document_revision=_document_revision(item_id, layer_id, draft),
            content_revision=_document_content_revision(draft),
            label=draft.label,
            kind=draft.kind,
            language=draft.language,
            preamble=draft.preamble,
        )

    def as_draft(self) -> TextLayerDraft:
        return TextLayerDraft(
            source=self.source,
            units=tuple(value.as_draft() for value in self.units),
            label=self.label,
            kind=self.kind,
            language=self.language,
            preamble=self.preamble,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "layer_id": self.layer_id,
            "label": self.label,
            "kind": self.kind,
            "language": self.language,
            "source": self.source.as_dict(),
            "preamble": self.preamble,
            "units": [value.as_dict() for value in self.units],
            "document_revision": self.document_revision,
            "content_revision": self.content_revision,
        }


def _view_revision(
    document: TextLayerDocumentSnapshot,
    source: TextLayerSourceView,
) -> str:
    return _digest(
        "tlv",
        {"document": document.as_dict(), "source": source.as_dict()},
    )


@dataclass(frozen=True, slots=True)
class TextLayerSummaryView:
    """Small query projection suitable for document pickers."""

    item_id: str
    layer_id: str
    label: str
    kind: str
    language: str
    document_revision: str
    content_revision: str
    view_revision: str
    source: TextLayerSourceView
    unit_count: int

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _identifier(self.layer_id, "layer_id")
        _safe_string(self.label, "label", maximum=512)
        _identifier(self.kind, "text layer kind")
        _language(self.language)
        for name in ("document_revision", "content_revision", "view_revision"):
            _revision(getattr(self, name), name)
        if not isinstance(self.source, TextLayerSourceView):
            raise TypeError("source must be TextLayerSourceView")
        if (
            not isinstance(self.unit_count, int)
            or isinstance(self.unit_count, bool)
            or self.unit_count < 0
            or self.unit_count > MAX_TEXT_LAYER_UNITS
        ):
            raise ValueError("unit_count is outside the text-layer unit budget")

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "layer_id": self.layer_id,
            "label": self.label,
            "kind": self.kind,
            "language": self.language,
            "document_revision": self.document_revision,
            "content_revision": self.content_revision,
            "view_revision": self.view_revision,
            "source": self.source.as_dict(),
            "unit_count": self.unit_count,
        }


@dataclass(frozen=True, slots=True)
class TextLayerDocumentView:
    """Complete query projection with live source freshness."""

    document: TextLayerDocumentSnapshot
    source: TextLayerSourceView
    view_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.document, TextLayerDocumentSnapshot):
            raise TypeError("document must be TextLayerDocumentSnapshot")
        if not isinstance(self.source, TextLayerSourceView):
            raise TypeError("source must be TextLayerSourceView")
        if (
            self.source.representation_id
            != self.document.source.representation_id
            or self.source.pinned_revision != self.document.source.revision
        ):
            raise ValueError("source view does not match the document pin")
        _revision(self.view_revision, "view_revision")
        if self.view_revision != _view_revision(self.document, self.source):
            raise ValueError("view_revision does not match the view")

    @classmethod
    def build(
        cls,
        document: TextLayerDocumentSnapshot,
        source: TextLayerSourceView,
    ) -> "TextLayerDocumentView":
        return cls(
            document=document,
            source=source,
            view_revision=_view_revision(document, source),
        )

    def summary(self) -> TextLayerSummaryView:
        return TextLayerSummaryView(
            item_id=self.document.item_id,
            layer_id=self.document.layer_id,
            label=self.document.label,
            kind=self.document.kind,
            language=self.document.language,
            document_revision=self.document.document_revision,
            content_revision=self.document.content_revision,
            view_revision=self.view_revision,
            source=self.source,
            unit_count=len(self.document.units),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "document": self.document.as_dict(),
            "source": self.source.as_dict(),
            "view_revision": self.view_revision,
        }


@dataclass(frozen=True, slots=True)
class TextLayerUnitReplacement:
    """Complete replacement of one unit's mutable record fields."""

    selector: str
    text: str
    provenance: TextLayerProvenance = field(default_factory=TextLayerProvenance)

    def __post_init__(self) -> None:
        _identifier(self.selector, "selector")
        _safe_string(
            self.text,
            "text",
            maximum=MAX_TEXT_UNIT_CHARACTERS,
            text=True,
        )
        if not isinstance(self.provenance, TextLayerProvenance):
            raise TypeError("provenance must be TextLayerProvenance")

    def as_dict(self) -> dict[str, Any]:
        return {
            "selector": self.selector,
            "text": self.text,
            "provenance": self.provenance.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CreateTextLayerCommand:
    item_id: str
    draft: TextLayerDraft
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str):
            raise TypeError("item_id must be a string")
        if not isinstance(self.draft, TextLayerDraft):
            raise TypeError("draft must be TextLayerDraft")
        if not isinstance(self.operation_id, str):
            raise TypeError("operation_id must be a string")


@dataclass(frozen=True, slots=True)
class ReplaceTextLayerUnitCommand:
    item_id: str
    layer_id: str
    replacement: TextLayerUnitReplacement
    expected_unit_revision: str
    expected_source_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for name in (
            "item_id",
            "layer_id",
            "expected_unit_revision",
            "expected_source_revision",
            "operation_id",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        if not isinstance(self.replacement, TextLayerUnitReplacement):
            raise TypeError("replacement must be TextLayerUnitReplacement")


@dataclass(frozen=True, slots=True)
class ReplaceTextLayerUnitsCommand:
    item_id: str
    layer_id: str
    replacements: tuple[TextLayerUnitReplacement, ...]
    expected_document_revision: str
    expected_source_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for name in (
            "item_id",
            "layer_id",
            "expected_document_revision",
            "expected_source_revision",
            "operation_id",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be a string")
        raw = self.replacements
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise TypeError("replacements must be a sequence")
        # Bound work before tuple materialization, duplicate detection,
        # sorting, and eventual command hashing.  This keeps an untrusted
        # client from turning validation into an unbounded CPU/memory task.
        if len(raw) > MAX_TEXT_LAYER_BATCH_REPLACEMENTS:
            raise ValueError("the text layer batch has too many replacements")
        replacements = _typed_tuple(
            raw,
            TextLayerUnitReplacement,
            "replacements",
            maximum=MAX_TEXT_LAYER_BATCH_REPLACEMENTS,
        )
        total_characters = 0
        for replacement in replacements:
            total_characters += len(replacement.text)
            if total_characters > MAX_TEXT_LAYER_BATCH_CHARACTERS:
                raise ValueError("the text layer batch is too large")
        _require_provenance_budget(
            tuple(value.provenance for value in replacements),
            field_name="text layer batch",
        )
        selectors = [value.selector for value in replacements]
        if len(selectors) != len(set(selectors)):
            raise ValueError("replacements contain duplicate selectors")
        object.__setattr__(
            self,
            "replacements",
            tuple(sorted(replacements, key=lambda value: value.selector)),
        )


@dataclass(frozen=True, slots=True)
class TextLayerUnitMutationReceipt:
    selector: str
    before_unit_revision: str
    after_unit_revision: str
    before_content_revision: str
    after_content_revision: str

    def __post_init__(self) -> None:
        _identifier(self.selector, "selector")
        _revision(
            self.before_unit_revision,
            "before_unit_revision",
            optional=True,
        )
        _revision(self.after_unit_revision, "after_unit_revision")
        _revision(
            self.before_content_revision,
            "before_content_revision",
            optional=True,
        )
        _revision(self.after_content_revision, "after_content_revision")
        if self.before_unit_revision == self.after_unit_revision:
            raise ValueError("a unit mutation must advance its unit revision")
        if bool(self.before_unit_revision) != bool(self.before_content_revision):
            raise ValueError("before unit and content revisions must agree")

    @classmethod
    def from_dict(cls, value: Any) -> "TextLayerUnitMutationReceipt":
        if not isinstance(value, Mapping) or set(value) != {
            "selector",
            "before_unit_revision",
            "after_unit_revision",
            "before_content_revision",
            "after_content_revision",
        }:
            raise ValueError("unit receipt fields do not match the schema")
        return cls(
            selector=value["selector"],
            before_unit_revision=value["before_unit_revision"],
            after_unit_revision=value["after_unit_revision"],
            before_content_revision=value["before_content_revision"],
            after_content_revision=value["after_content_revision"],
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "selector": self.selector,
            "before_unit_revision": self.before_unit_revision,
            "after_unit_revision": self.after_unit_revision,
            "before_content_revision": self.before_content_revision,
            "after_content_revision": self.after_content_revision,
        }


@dataclass(frozen=True, slots=True)
class TextLayerMutationReceipt:
    """Small, path-free public outcome with no replay fingerprint."""

    action: TextLayerMutationAction
    operation_id: str
    item_id: str
    layer_id: str
    source_revision: str
    before_document_revision: str
    after_document_revision: str
    before_content_revision: str
    after_content_revision: str
    units: tuple[TextLayerUnitMutationReceipt, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        _identifier(self.operation_id, "operation_id")
        _identifier(self.item_id, "item_id")
        _identifier(self.layer_id, "layer_id")
        _revision(self.source_revision, "source_revision")
        for name in (
            "before_document_revision",
            "before_content_revision",
        ):
            _revision(getattr(self, name), name, optional=True)
        for name in ("after_document_revision", "after_content_revision"):
            _revision(getattr(self, name), name)
        raw_units = self.units
        if isinstance(raw_units, (str, bytes)) or not isinstance(
            raw_units, Sequence
        ):
            raise TypeError("receipt units must be a sequence")
        if len(raw_units) > MAX_TEXT_LAYER_RECEIPT_UNITS:
            raise ValueError("receipt contains too many unit mutations")
        units = _typed_tuple(
            raw_units,
            TextLayerUnitMutationReceipt,
            "receipt units",
            maximum=MAX_TEXT_LAYER_RECEIPT_UNITS,
        )
        selectors = [value.selector for value in units]
        if len(selectors) != len(set(selectors)):
            raise ValueError("receipt contains duplicate selectors")
        units = tuple(sorted(units, key=lambda value: value.selector))
        if self.action == "create":
            if self.before_document_revision or self.before_content_revision:
                raise ValueError("create receipt cannot have a prior revision")
            if units:
                raise ValueError("create receipt must use aggregate-only outcome")
        else:
            if (
                not self.before_document_revision
                or not self.before_content_revision
                or self.before_document_revision == self.after_document_revision
                or not units
                or any(not value.before_unit_revision for value in units)
            ):
                raise ValueError("replace receipt state is inconsistent")
            document_content_changed = (
                self.before_content_revision != self.after_content_revision
            )
            unit_content_changed = any(
                value.before_content_revision != value.after_content_revision
                for value in units
            )
            if document_content_changed != unit_content_changed:
                raise ValueError(
                    "replace receipt content revisions are contradictory"
                )
        object.__setattr__(self, "units", units)

    @classmethod
    def from_public_dict(cls, value: Any) -> "TextLayerMutationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("text layer receipt must be an object")
        fields = {
            "action",
            "operation_id",
            "item_id",
            "layer_id",
            "source_revision",
            "before_document_revision",
            "after_document_revision",
            "before_content_revision",
            "after_content_revision",
            "units",
        }
        if set(value) != fields:
            raise ValueError("text layer receipt fields do not match the schema")
        raw_units = value["units"]
        if isinstance(raw_units, (str, bytes)) or not isinstance(
            raw_units, Sequence
        ):
            raise TypeError("receipt units must be an array")
        if len(raw_units) > MAX_TEXT_LAYER_RECEIPT_UNITS:
            raise ValueError("receipt contains too many unit mutations")
        materialized_units = tuple(
            islice(iter(raw_units), MAX_TEXT_LAYER_RECEIPT_UNITS + 1)
        )
        if len(materialized_units) > MAX_TEXT_LAYER_RECEIPT_UNITS:
            raise ValueError("receipt contains too many unit mutations")
        if len(materialized_units) != len(raw_units):
            raise ValueError("receipt units have inconsistent length")
        return cls(
            action=value["action"],
            operation_id=value["operation_id"],
            item_id=value["item_id"],
            layer_id=value["layer_id"],
            source_revision=value["source_revision"],
            before_document_revision=value["before_document_revision"],
            after_document_revision=value["after_document_revision"],
            before_content_revision=value["before_content_revision"],
            after_content_revision=value["after_content_revision"],
            units=tuple(
                TextLayerUnitMutationReceipt.from_dict(v)
                for v in materialized_units
            ),
        )

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "operation_id": self.operation_id,
            "item_id": self.item_id,
            "layer_id": self.layer_id,
            "source_revision": self.source_revision,
            "before_document_revision": self.before_document_revision,
            "after_document_revision": self.after_document_revision,
            "before_content_revision": self.before_content_revision,
            "after_content_revision": self.after_content_revision,
            "units": [value.as_dict() for value in self.units],
        }

    def as_dict(self) -> dict[str, Any]:
        """Return the transport-safe public projection."""

        return self.as_public_dict()


class TextLayerStoredMutationReceipt:
    """Storage-only replay envelope around one public mutation receipt.

    This is intentionally not a dataclass: ``dataclasses.asdict`` cannot walk
    into the private command fingerprint through a public command result.
    Repository adapters persist this envelope through its versioned codec and
    return it only through the aggregate unit-of-work port.
    """

    __slots__ = ("_receipt", "__command_sha256")
    _WRITE_ONCE_FIELDS = frozenset(
        {
            "_receipt",
            "_TextLayerStoredMutationReceipt__command_sha256",
        }
    )

    def __init__(
        self,
        receipt: TextLayerMutationReceipt,
        *,
        command_sha256: str,
    ) -> None:
        if not isinstance(receipt, TextLayerMutationReceipt):
            raise TypeError("receipt must be TextLayerMutationReceipt")
        if not isinstance(command_sha256, str) or not _SHA256_RE.fullmatch(
            command_sha256
        ):
            raise ValueError("command_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "_receipt", receipt)
        object.__setattr__(
            self,
            "_TextLayerStoredMutationReceipt__command_sha256",
            command_sha256,
        )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._WRITE_ONCE_FIELDS and hasattr(self, name):
            raise AttributeError("stored text layer receipt fields are write-once")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("stored text layer receipt fields cannot be deleted")

    @property
    def receipt(self) -> TextLayerMutationReceipt:
        return self._receipt

    def matches_command_sha256(self, value: str) -> bool:
        return self.__command_sha256 == value

    @classmethod
    def from_storage_dict(cls, value: Any) -> "TextLayerStoredMutationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("stored text layer receipt must be an object")
        public_fields = {
            "action",
            "operation_id",
            "item_id",
            "layer_id",
            "source_revision",
            "before_document_revision",
            "after_document_revision",
            "before_content_revision",
            "after_content_revision",
            "units",
        }
        fields = {
            "schema",
            "version",
            "command_sha256",
            *public_fields,
        }
        if set(value) != fields:
            raise ValueError(
                "stored text layer receipt fields do not match the schema"
            )
        if value["schema"] != TEXT_LAYER_RECEIPT_STORAGE_SCHEMA:
            raise ValueError("stored text layer receipt schema is unsupported")
        if (
            type(value["version"]) is not int
            or value["version"] != TEXT_LAYER_RECEIPT_STORAGE_VERSION
        ):
            raise ValueError("stored text layer receipt version is unsupported")
        receipt = TextLayerMutationReceipt.from_public_dict(
            {name: value[name] for name in public_fields}
        )
        return cls(receipt, command_sha256=value["command_sha256"])

    def as_storage_dict(self) -> dict[str, Any]:
        return {
            "schema": TEXT_LAYER_RECEIPT_STORAGE_SCHEMA,
            "version": TEXT_LAYER_RECEIPT_STORAGE_VERSION,
            **self._receipt.as_public_dict(),
            "command_sha256": self.__command_sha256,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TextLayerStoredMutationReceipt):
            return NotImplemented
        return (
            self._receipt == other._receipt
            and self.__command_sha256 == other.__command_sha256
        )

    def __hash__(self) -> int:
        return hash((self._receipt, self.__command_sha256))

    def __repr__(self) -> str:
        return f"TextLayerStoredMutationReceipt(receipt={self._receipt!r})"


@dataclass(frozen=True, slots=True)
class TextLayerCommandResult:
    receipt: TextLayerMutationReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, TextLayerMutationReceipt):
            raise TypeError("receipt must be TextLayerMutationReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_public_dict(),
        }


class TextLayerReadSessionPort(Protocol):
    """One coherent query snapshot over layers and their live sources."""

    def item_exists(self, item_id: str) -> bool: ...

    def list(self, item_id: str) -> Sequence[TextLayerDocumentSnapshot]: ...

    def get(
        self, item_id: str, layer_id: str
    ) -> TextLayerDocumentSnapshot | None: ...

    def source(
        self, item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot | None: ...


class TextLayerUnitOfWorkPort(TextLayerReadSessionPort, Protocol):
    """Operation-scoped, explicitly committed text-layer transaction.

    ``receipt`` must consult only the durable replay namespace.  It is the
    first method the application service calls and must not require a live
    item, representation, layer document, or media read.  Stage methods do
    not publish.  ``commit`` atomically publishes staged state and the receipt;
    leaving the context without a successful commit discards staged state.
    """

    def receipt(
        self, operation_id: str
    ) -> TextLayerStoredMutationReceipt | None: ...

    def allocate_layer_id(self, item_id: str) -> str: ...

    def stage_create(
        self,
        item_id: str,
        layer_id: str,
        draft: TextLayerDraft,
    ) -> TextLayerDocumentSnapshot: ...

    def stage_replace(
        self,
        current: TextLayerDocumentSnapshot,
        draft: TextLayerDraft,
    ) -> TextLayerDocumentSnapshot: ...

    def commit(self, receipt: TextLayerStoredMutationReceipt) -> None: ...


class TextLayerAggregateRepositoryPort(Protocol):
    """Open coherent read sessions and durable command units of work."""

    def snapshot(
        self, item_id: str
    ) -> ContextManager[TextLayerReadSessionPort]: ...

    def unit_of_work(
        self, *, operation_id: str
    ) -> ContextManager[TextLayerUnitOfWorkPort]: ...


class TextLayerAggregateService:
    """Query and conditionally mutate canonical text-layer documents."""

    def __init__(self, repository: TextLayerAggregateRepositoryPort) -> None:
        self._repository = repository

    def list(self, item_id: str) -> tuple[TextLayerSummaryView, ...]:
        identifier = self._item_id(item_id)
        with self._snapshot(identifier) as session:
            self._require_item(session, identifier)
            raw = self._repository_method(session, "list", identifier)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise RepositoryError(
                    "the text layer repository returned an invalid collection",
                    code="invalid_text_layer_collection",
                    details={"item_id": identifier},
                )
            reported_count = self._repository_call(len, raw)
            if reported_count > MAX_TEXT_LAYERS_PER_ITEM:
                raise RepositoryError(
                    "the item has too many text layers",
                    code="text_layer_collection_too_large",
                    details={
                        "item_id": identifier,
                        "maximum": MAX_TEXT_LAYERS_PER_ITEM,
                    },
                )
            raw_documents = self._repository_call(
                lambda: tuple(
                    islice(iter(raw), MAX_TEXT_LAYERS_PER_ITEM + 1)
                )
            )
            if len(raw_documents) > MAX_TEXT_LAYERS_PER_ITEM:
                raise RepositoryError(
                    "the item has too many text layers",
                    code="text_layer_collection_too_large",
                    details={
                        "item_id": identifier,
                        "maximum": MAX_TEXT_LAYERS_PER_ITEM,
                    },
                )
            if len(raw_documents) != reported_count:
                raise RepositoryError(
                    "the text layer repository returned an inconsistent collection",
                    code="invalid_text_layer_collection",
                    details={"item_id": identifier},
                )
            documents = tuple(
                self._document(value, item_id=identifier)
                for value in raw_documents
            )
            layer_ids = [value.layer_id for value in documents]
            if len(layer_ids) != len(set(layer_ids)):
                raise RepositoryError(
                    "the text layer repository returned duplicate identities",
                    code="duplicate_text_layer_identity",
                    details={"item_id": identifier},
                )
            summaries = tuple(
                self._view(session, value).summary() for value in documents
            )
        return tuple(
            sorted(
                summaries,
                key=lambda value: (
                    value.label.casefold(),
                    value.layer_id,
                ),
            )
        )

    def get(self, item_id: str, layer_id: str) -> TextLayerDocumentView:
        identifier = self._item_id(item_id)
        layer = self._layer_id(layer_id)
        with self._snapshot(identifier) as session:
            self._require_item(session, identifier)
            raw = self._repository_method(session, "get", identifier, layer)
            if raw is None:
                raise NotFoundError(
                    "the text layer does not exist",
                    code="text_layer_not_found",
                    details={"item_id": identifier, "layer_id": layer},
                )
            document = self._document(
                raw,
                item_id=identifier,
                layer_id=layer,
            )
            return self._view(session, document)

    def create(self, command: CreateTextLayerCommand) -> TextLayerCommandResult:
        if not isinstance(command, CreateTextLayerCommand):
            raise ValidationError(
                "create requires a CreateTextLayerCommand",
                code="invalid_text_layer_command",
            )
        item_id = self._item_id(command.item_id)
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "create",
                "item_id": item_id,
                "draft": command.draft.as_dict(),
            }
        )
        with self._unit_of_work(operation_id) as unit:
            replay = self._replay(
                unit,
                operation_id=operation_id,
                command_sha256=command_sha256,
                action="create",
                item_id=item_id,
                source_revision=command.draft.source.revision,
            )
            if replay is not None:
                return replay
            self._require_item(unit, item_id)
            self._require_source(unit, item_id, command.draft.source)
            layer_id = self._allocated_layer_id(
                self._repository_method(unit, "allocate_layer_id", item_id)
            )
            collision = self._repository_method(
                unit,
                "get",
                item_id,
                layer_id,
            )
            if collision is not None:
                raise RepositoryError(
                    "the text layer repository allocated an existing identity",
                    code="allocated_text_layer_id_collision",
                    details={"item_id": item_id, "layer_id": layer_id},
                    retryable=True,
                )
            staged = self._document(
                self._repository_method(
                    unit,
                    "stage_create",
                    item_id,
                    layer_id,
                    command.draft,
                ),
                item_id=item_id,
                layer_id=layer_id,
            )
            self._match_staged(staged, command.draft)
            receipt = TextLayerMutationReceipt(
                action="create",
                operation_id=operation_id,
                item_id=item_id,
                layer_id=layer_id,
                source_revision=command.draft.source.revision,
                before_document_revision="",
                after_document_revision=staged.document_revision,
                before_content_revision="",
                after_content_revision=staged.content_revision,
                # A create receipt represents one aggregate publication.
                # Per-unit entries would scale to MAX_TEXT_LAYER_UNITS and
                # are unnecessary for replay or reconciliation.
                units=(),
            )
            stored_receipt = TextLayerStoredMutationReceipt(
                receipt,
                command_sha256=command_sha256,
            )
            self._repository_method(unit, "commit", stored_receipt)
            return TextLayerCommandResult(receipt)

    def replace_unit(
        self,
        command: ReplaceTextLayerUnitCommand,
    ) -> TextLayerCommandResult:
        if not isinstance(command, ReplaceTextLayerUnitCommand):
            raise ValidationError(
                "replace_unit requires a ReplaceTextLayerUnitCommand",
                code="invalid_text_layer_command",
            )
        item_id = self._item_id(command.item_id)
        layer_id = self._layer_id(command.layer_id)
        expected_unit = self._expected_revision(
            command.expected_unit_revision,
            code="text_layer_unit_revision_required",
            invalid_code="invalid_text_layer_unit_revision",
            details={
                "item_id": item_id,
                "layer_id": layer_id,
                "selector": command.replacement.selector,
            },
        )
        expected_source = self._expected_source_revision(
            command.expected_source_revision,
            item_id=item_id,
            layer_id=layer_id,
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "replace-unit",
                "item_id": item_id,
                "layer_id": layer_id,
                "expected_unit_revision": expected_unit,
                "expected_source_revision": expected_source,
                "replacement": command.replacement.as_dict(),
            }
        )
        return self._replace(
            item_id=item_id,
            layer_id=layer_id,
            replacements=(command.replacement,),
            expected_source_revision=expected_source,
            operation_id=operation_id,
            command_sha256=command_sha256,
            action="replace-unit",
            expected_unit_revision=expected_unit,
        )

    def replace_units(
        self,
        command: ReplaceTextLayerUnitsCommand,
    ) -> TextLayerCommandResult:
        if not isinstance(command, ReplaceTextLayerUnitsCommand):
            raise ValidationError(
                "replace_units requires a ReplaceTextLayerUnitsCommand",
                code="invalid_text_layer_command",
            )
        item_id = self._item_id(command.item_id)
        layer_id = self._layer_id(command.layer_id)
        if not command.replacements:
            raise ValidationError(
                "a batch replacement must contain at least one unit",
                code="empty_text_layer_batch",
                details={"item_id": item_id, "layer_id": layer_id},
            )
        expected_document = self._expected_revision(
            command.expected_document_revision,
            code="text_layer_document_revision_required",
            invalid_code="invalid_text_layer_document_revision",
            details={"item_id": item_id, "layer_id": layer_id},
        )
        expected_source = self._expected_source_revision(
            command.expected_source_revision,
            item_id=item_id,
            layer_id=layer_id,
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "replace-batch",
                "item_id": item_id,
                "layer_id": layer_id,
                "expected_document_revision": expected_document,
                "expected_source_revision": expected_source,
                "replacements": [
                    value.as_dict() for value in command.replacements
                ],
            }
        )
        return self._replace(
            item_id=item_id,
            layer_id=layer_id,
            replacements=command.replacements,
            expected_source_revision=expected_source,
            operation_id=operation_id,
            command_sha256=command_sha256,
            action="replace-batch",
            expected_document_revision=expected_document,
        )

    def _replace(
        self,
        *,
        item_id: str,
        layer_id: str,
        replacements: tuple[TextLayerUnitReplacement, ...],
        expected_source_revision: str,
        operation_id: str,
        command_sha256: str,
        action: Literal["replace-unit", "replace-batch"],
        expected_unit_revision: str = "",
        expected_document_revision: str = "",
    ) -> TextLayerCommandResult:
        selectors = tuple(value.selector for value in replacements)
        try:
            with self._unit_of_work(operation_id) as unit:
                replay = self._replay(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    action=action,
                    item_id=item_id,
                    layer_id=layer_id,
                    source_revision=expected_source_revision,
                    selectors=set(selectors),
                )
                if replay is not None:
                    return replay
                self._require_item(unit, item_id)
                current_raw = self._repository_method(
                    unit,
                    "get",
                    item_id,
                    layer_id,
                )
                if current_raw is None:
                    raise NotFoundError(
                        "the text layer does not exist",
                        code="text_layer_not_found",
                        details={"item_id": item_id, "layer_id": layer_id},
                    )
                current = self._document(
                    current_raw,
                    item_id=item_id,
                    layer_id=layer_id,
                )
                if expected_document_revision and (
                    current.document_revision != expected_document_revision
                ):
                    raise ConflictError(
                        "the text layer changed elsewhere",
                        code="text_layer_document_revision_conflict",
                        details={
                            "item_id": item_id,
                            "layer_id": layer_id,
                            "expected_revision": expected_document_revision,
                            "current_revision": current.document_revision,
                        },
                        retryable=True,
                    )
                by_selector = {value.selector: value for value in current.units}
                missing = sorted(set(selectors) - set(by_selector))
                if missing:
                    raise NotFoundError(
                        "one or more text units do not exist",
                        code="text_layer_unit_not_found",
                        details={
                            "item_id": item_id,
                            "layer_id": layer_id,
                            "selectors": missing,
                        },
                    )
                if expected_unit_revision:
                    selected = by_selector[selectors[0]]
                    if selected.unit_revision != expected_unit_revision:
                        raise ConflictError(
                            "the text unit changed elsewhere",
                            code="text_layer_unit_revision_conflict",
                            details={
                                "item_id": item_id,
                                "layer_id": layer_id,
                                "selector": selected.selector,
                                "expected_revision": expected_unit_revision,
                                "current_revision": selected.unit_revision,
                            },
                            retryable=True,
                        )
                if current.source.revision != expected_source_revision:
                    raise ConflictError(
                        "the text layer is pinned to another source revision",
                        code="text_layer_source_revision_conflict",
                        details={
                            "item_id": item_id,
                            "layer_id": layer_id,
                            "expected_revision": expected_source_revision,
                            "pinned_revision": current.source.revision,
                        },
                    )
                self._require_source(unit, item_id, current.source)
                candidate = self._apply(current, replacements)
                if self._same_draft(candidate, current.as_draft()):
                    raise ValidationError(
                        "the replacement does not change the text layer",
                        code="unchanged_text_layer",
                        details={"item_id": item_id, "layer_id": layer_id},
                    )
                staged = self._document(
                    self._repository_method(
                        unit,
                        "stage_replace",
                        current,
                        candidate,
                    ),
                    item_id=item_id,
                    layer_id=layer_id,
                )
                self._match_staged(staged, candidate)
                if staged.document_revision == current.document_revision:
                    raise RepositoryError(
                        "the text layer repository did not advance the revision",
                        code="text_layer_revision_not_advanced",
                        details={"item_id": item_id, "layer_id": layer_id},
                    )
                changes = self._changes(current, staged, set(selectors))
                if not changes:
                    raise RepositoryError(
                        "the text layer repository did not replace a unit",
                        code="text_layer_repository_content_mismatch",
                        details={"item_id": item_id, "layer_id": layer_id},
                    )
                receipt = TextLayerMutationReceipt(
                    action=action,
                    operation_id=operation_id,
                    item_id=item_id,
                    layer_id=layer_id,
                    source_revision=expected_source_revision,
                    before_document_revision=current.document_revision,
                    after_document_revision=staged.document_revision,
                    before_content_revision=current.content_revision,
                    after_content_revision=staged.content_revision,
                    units=changes,
                )
                stored_receipt = TextLayerStoredMutationReceipt(
                    receipt,
                    command_sha256=command_sha256,
                )
                self._repository_method(unit, "commit", stored_receipt)
                return TextLayerCommandResult(receipt)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from None

    @staticmethod
    def _apply(
        current: TextLayerDocumentSnapshot,
        replacements: Sequence[TextLayerUnitReplacement],
    ) -> TextLayerDraft:
        replacements_by_selector = {
            value.selector: value for value in replacements
        }
        units = []
        for value in current.units:
            replacement = replacements_by_selector.get(value.selector)
            units.append(
                value.as_draft()
                if replacement is None
                else TextLayerUnitDraft(
                    selector=value.selector,
                    order=value.order,
                    label=value.label,
                    text=replacement.text,
                    provenance=replacement.provenance,
                )
            )
        return TextLayerDraft(
            source=current.source,
            units=tuple(units),
            label=current.label,
            kind=current.kind,
            language=current.language,
            preamble=current.preamble,
        )

    @staticmethod
    def _changes(
        before: TextLayerDocumentSnapshot,
        after: TextLayerDocumentSnapshot,
        requested: set[str],
    ) -> tuple[TextLayerUnitMutationReceipt, ...]:
        before_by_id = {value.selector: value for value in before.units}
        after_by_id = {value.selector: value for value in after.units}
        changes = []
        for selector in sorted(requested):
            old = before_by_id[selector]
            new = after_by_id[selector]
            if old.unit_revision == new.unit_revision:
                continue
            changes.append(
                TextLayerUnitMutationReceipt(
                    selector=selector,
                    before_unit_revision=old.unit_revision,
                    after_unit_revision=new.unit_revision,
                    before_content_revision=old.content_revision,
                    after_content_revision=new.content_revision,
                )
            )
        return tuple(changes)

    @staticmethod
    def _match_staged(
        staged: TextLayerDocumentSnapshot,
        draft: TextLayerDraft,
    ) -> None:
        if not TextLayerAggregateService._same_draft(
            staged.as_draft(),
            draft,
        ):
            raise RepositoryError(
                "the text layer repository changed canonical content",
                code="text_layer_repository_content_mismatch",
                details={
                    "item_id": staged.item_id,
                    "layer_id": staged.layer_id,
                },
            )

    @staticmethod
    def _same_draft(left: TextLayerDraft, right: TextLayerDraft) -> bool:
        """Compare through type-strict canonical JSON, never Python mappings."""

        return _canonical(left.as_dict()) == _canonical(right.as_dict())

    @staticmethod
    def _document(
        value: Any,
        *,
        item_id: str,
        layer_id: str = "",
    ) -> TextLayerDocumentSnapshot:
        if not isinstance(value, TextLayerDocumentSnapshot):
            raise RepositoryError(
                "the text layer repository returned an invalid document",
                code="invalid_text_layer_document",
                details={"item_id": item_id},
            )
        if value.item_id != item_id or (layer_id and value.layer_id != layer_id):
            raise RepositoryError(
                "the text layer repository returned another document",
                code="text_layer_repository_scope_mismatch",
                details={
                    "requested_item_id": item_id,
                    "requested_layer_id": layer_id,
                    "returned_item_id": value.item_id,
                    "returned_layer_id": value.layer_id,
                },
            )
        return value

    def _view(
        self,
        session: TextLayerReadSessionPort,
        document: TextLayerDocumentSnapshot,
    ) -> TextLayerDocumentView:
        raw = self._repository_method(
            session,
            "source",
            document.item_id,
            document.source.representation_id,
        )
        if raw is None:
            source = TextLayerSourceView(
                representation_id=document.source.representation_id,
                pinned_revision=document.source.revision,
            )
        else:
            current = self._source(
                raw,
                item_id=document.item_id,
                representation_id=document.source.representation_id,
            )
            source = TextLayerSourceView(
                representation_id=current.representation_id,
                pinned_revision=document.source.revision,
                current_revision=current.revision,
                available=True,
            )
        return TextLayerDocumentView.build(document, source)

    @staticmethod
    def _source(
        value: Any,
        *,
        item_id: str,
        representation_id: str,
    ) -> TextLayerSourceSnapshot:
        if not isinstance(value, TextLayerSourceSnapshot):
            raise RepositoryError(
                "the text layer repository returned an invalid source",
                code="invalid_text_layer_source",
                details={"item_id": item_id},
            )
        if (
            value.item_id != item_id
            or value.representation_id != representation_id
        ):
            raise RepositoryError(
                "the text layer repository returned another source",
                code="text_layer_repository_scope_mismatch",
                details={
                    "requested_item_id": item_id,
                    "requested_representation_id": representation_id,
                    "returned_item_id": value.item_id,
                    "returned_representation_id": value.representation_id,
                },
            )
        return value

    def _require_source(
        self,
        unit: TextLayerReadSessionPort,
        item_id: str,
        pin: TextLayerSourcePin,
    ) -> TextLayerSourceSnapshot:
        raw = self._repository_method(
            unit,
            "source",
            item_id,
            pin.representation_id,
        )
        if raw is None:
            raise NotFoundError(
                "the text layer source is unavailable",
                code="text_layer_source_not_found",
                details={
                    "item_id": item_id,
                    "representation_id": pin.representation_id,
                },
            )
        source = self._source(
            raw,
            item_id=item_id,
            representation_id=pin.representation_id,
        )
        if source.revision != pin.revision:
            raise ConflictError(
                "the text layer source changed elsewhere",
                code="text_layer_source_revision_conflict",
                details={
                    "item_id": item_id,
                    "representation_id": pin.representation_id,
                    "expected_revision": pin.revision,
                    "current_revision": source.revision,
                },
                retryable=True,
            )
        return source

    @classmethod
    def _require_item(
        cls,
        session: TextLayerReadSessionPort,
        item_id: str,
    ) -> None:
        exists = cls._repository_method(session, "item_exists", item_id)
        if not isinstance(exists, bool):
            raise RepositoryError(
                "the text layer repository returned invalid item membership",
                code="invalid_text_layer_item_membership",
                details={"item_id": item_id},
            )
        if not exists:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": item_id},
            )

    @classmethod
    def _replay(
        cls,
        unit: TextLayerUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        action: TextLayerMutationAction,
        item_id: str,
        source_revision: str,
        layer_id: str = "",
        selectors: set[str] | None = None,
    ) -> TextLayerCommandResult | None:
        prior = cls._repository_method(unit, "receipt", operation_id)
        if prior is None:
            return None
        if not isinstance(prior, TextLayerStoredMutationReceipt):
            raise RepositoryError(
                "the text layer repository returned an invalid receipt",
                code="invalid_text_layer_receipt",
            )
        receipt = prior.receipt
        if receipt.operation_id != operation_id:
            raise RepositoryError(
                "the text layer repository returned another operation receipt",
                code="text_layer_receipt_scope_mismatch",
            )
        if not prior.matches_command_sha256(
            command_sha256
        ) or receipt.action != action:
            raise ConflictError(
                "operation id was already used for another text layer command",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        if (
            receipt.item_id != item_id
            or (layer_id and receipt.layer_id != layer_id)
            or receipt.source_revision != source_revision
        ):
            raise RepositoryError(
                "the stored text layer receipt has inconsistent scope",
                code="invalid_text_layer_receipt",
                details={"operation_id": operation_id},
            )
        if selectors is not None:
            changed = {value.selector for value in receipt.units}
            if not changed or not changed.issubset(selectors):
                raise RepositoryError(
                    "the stored text layer receipt has inconsistent units",
                    code="invalid_text_layer_receipt",
                    details={"operation_id": operation_id},
                )
        return TextLayerCommandResult(receipt, replayed=True)

    @staticmethod
    def _command_hash(value: JsonMapping) -> str:
        return hashlib.sha256(_canonical(value)).hexdigest()

    @staticmethod
    def _item_id(value: str) -> str:
        if not value:
            raise ValidationError("item id is required", code="item_id_required")
        try:
            return _identifier(value, "item_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "item id must be a portable identifier",
                code="invalid_item_id",
            ) from exc

    @staticmethod
    def _layer_id(value: str) -> str:
        if not value:
            raise ValidationError(
                "text layer id is required",
                code="text_layer_id_required",
            )
        try:
            return _identifier(value, "layer_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "text layer id must be a portable identifier",
                code="invalid_text_layer_id",
            ) from exc

    @staticmethod
    def _allocated_layer_id(value: Any) -> str:
        try:
            return _identifier(value, "allocated layer_id")
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "the text layer repository allocated an invalid identity",
                code="invalid_allocated_text_layer_id",
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
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "operation id must be a portable token",
                code="invalid_operation_id",
            ) from exc

    @staticmethod
    def _expected_revision(
        value: str,
        *,
        code: str,
        invalid_code: str,
        details: JsonMapping,
    ) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an expected revision is required",
                code=code,
                details=details,
            )
        try:
            return _revision(value, "expected revision")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "the expected revision is invalid",
                code=invalid_code,
                details=details,
            ) from exc

    @classmethod
    def _expected_source_revision(
        cls,
        value: str,
        *,
        item_id: str,
        layer_id: str,
    ) -> str:
        return cls._expected_revision(
            value,
            code="text_layer_source_revision_required",
            invalid_code="invalid_text_layer_source_revision",
            details={"item_id": item_id, "layer_id": layer_id},
        )

    @staticmethod
    def _repository_call(
        operation: Callable[..., _T],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        """Call one adapter method without trusting adapter error payloads."""

        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            # This deliberately includes EngineError.  An adapter must not be
            # able to smuggle paths, provider payloads, or arbitrary public
            # error codes through the trusted application boundary.
            raise TextLayerAggregateService._repository_failure(exc) from None

    @classmethod
    def _repository_method(
        cls,
        target: Any,
        name: str,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Resolve and call an adapter method inside the sanitized boundary."""

        return cls._repository_call(
            lambda: getattr(target, name)(*args, **kwargs)
        )

    @contextmanager
    def _snapshot(
        self,
        item_id: str,
    ) -> Generator[TextLayerReadSessionPort, None, None]:
        """Sanitize read-context entry/exit and forbid error suppression."""

        try:
            manager = self._repository.snapshot(item_id)
            session = manager.__enter__()
        except Exception as exc:
            raise self._repository_failure(exc) from None

        try:
            yield session
        except BaseException:
            error_info = sys.exc_info()
            try:
                # Ignore the return value: repository managers cannot suppress
                # an engine validation, not-found, or conflict outcome.
                manager.__exit__(*error_info)
            except Exception as exc:
                raise self._repository_failure(exc) from None
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception as exc:
                raise self._repository_failure(exc) from None

    @contextmanager
    def _unit_of_work(
        self,
        operation_id: str,
    ) -> Generator[TextLayerUnitOfWorkPort, None, None]:
        """Sanitize command-context entry/exit and forbid error suppression."""

        try:
            manager = self._repository.unit_of_work(
                operation_id=operation_id
            )
            unit = manager.__enter__()
        except Exception as exc:
            raise self._repository_failure(exc) from None

        try:
            yield unit
        except BaseException:
            error_info = sys.exc_info()
            try:
                manager.__exit__(*error_info)
            except Exception as exc:
                raise self._repository_failure(exc) from None
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception as exc:
                raise self._repository_failure(exc) from None

    @staticmethod
    def _repository_failure(exc: Exception) -> RepositoryError:
        return RepositoryError(
            "the text layer repository failed",
            code="text_layer_repository_unavailable",
            # Backend exception text may contain paths, credentials, SQL, or
            # provider payloads.  Only the diagnostic type crosses the public
            # engine boundary.
            details={"cause_type": type(exc).__name__},
            retryable=True,
        )


__all__ = [
    "CreateTextLayerCommand",
    "MAX_PORTABLE_JSON_INTEGER",
    "MAX_TEXT_LAYER_BATCH_CHARACTERS",
    "MAX_TEXT_LAYER_BATCH_REPLACEMENTS",
    "MAX_TEXT_LAYER_CHARACTERS",
    "MAX_TEXT_LAYERS_PER_ITEM",
    "MAX_TEXT_LAYER_METADATA_DEPTH",
    "MAX_TEXT_LAYER_METADATA_ENCODED_BYTES",
    "MAX_TEXT_LAYER_METADATA_NODES",
    "MAX_TEXT_LAYER_METADATA_STRING_CHARACTERS",
    "MAX_TEXT_LAYER_PROVENANCE_ENCODED_BYTES",
    "MAX_TEXT_LAYER_RECEIPT_UNITS",
    "MAX_TEXT_LAYER_UNITS",
    "MAX_TEXT_UNIT_CHARACTERS",
    "ReplaceTextLayerUnitCommand",
    "ReplaceTextLayerUnitsCommand",
    "TextLayerAggregateService",
    "TextLayerAggregateRepositoryPort",
    "TextLayerCommandResult",
    "TextLayerDocumentSnapshot",
    "TextLayerDocumentView",
    "TextLayerDraft",
    "TextLayerMutationAction",
    "TextLayerMutationReceipt",
    "TextLayerOrigin",
    "TextLayerProvenance",
    "TextLayerReadSessionPort",
    "TextLayerReviewState",
    "TextLayerSourcePin",
    "TextLayerSourceSnapshot",
    "TextLayerSourceStatus",
    "TextLayerSourceView",
    "TextLayerSummaryView",
    "TextLayerStoredMutationReceipt",
    "TextLayerUnitDraft",
    "TextLayerUnitMutationReceipt",
    "TextLayerUnitOfWorkPort",
    "TextLayerUnitReplacement",
    "TextLayerUnitSnapshot",
    "TEXT_LAYER_RECEIPT_STORAGE_SCHEMA",
    "TEXT_LAYER_RECEIPT_STORAGE_VERSION",
]
