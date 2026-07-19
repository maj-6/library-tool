"""Immutable contracts for the provider-neutral translation aggregate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Sequence


_TRANSLATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_TRANSLATION_PAGE_STATES = frozenset(
    {"current", "stale", "untracked", "missing", "orphaned"}
)
_TRANSLATION_ORIGINS = frozenset(
    {"unknown", "legacy", "machine", "human", "import"}
)
_TRANSLATION_REVIEW_STATES = frozenset(
    {"unreviewed", "reviewed", "approved", "rejected"}
)

TranslationPageState = Literal[
    "current", "stale", "untracked", "missing", "orphaned"
]
TranslationPageOrigin = Literal[
    "unknown", "legacy", "machine", "human", "import"
]
TranslationReviewState = Literal[
    "unreviewed", "reviewed", "approved", "rejected"
]


def _translation_identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _TRANSLATION_ID_RE.fullmatch(value):
        raise ValueError(f"{name} must be a portable identifier")
    return value


def _translation_language(value: str) -> str:
    if not isinstance(value, str) or not _LANGUAGE_TAG_RE.fullmatch(value):
        raise ValueError("target_language must be a valid language tag")
    return value


def _translation_revision(value: str, name: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return value
    return _translation_identifier(value, name)


def _translation_string(
    value: str, name: str, *, max_length: int | None = None
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ValueError(f"{name} contains a control character")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValueError(f"{name} contains an unpaired surrogate")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{name} is too long")
    return value


@dataclass(frozen=True, slots=True)
class TranslationSourceCanvas:
    """One authoritative, ordered source-text canvas.

    ``selector`` is the stable identity used by translation records. ``label``
    is presentation-only (for example, a printed folio or current PDF page).
    """

    selector: str
    order: int
    text: str
    label: str = ""

    def __post_init__(self) -> None:
        _translation_identifier(self.selector, "selector")
        if (
            not isinstance(self.order, int)
            or isinstance(self.order, bool)
            or self.order < 0
        ):
            raise ValueError("order must be a non-negative integer")
        _translation_string(self.text, "text")
        _translation_string(self.label, "label", max_length=256)

    def as_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "order": self.order,
            "label": self.label,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class TranslationSourceSnapshot:
    """Authoritative text-layer input returned by the source repository."""

    item_id: str
    layer_id: str
    representation_id: str
    canvases: tuple[TranslationSourceCanvas, ...] = ()

    def __post_init__(self) -> None:
        _translation_identifier(self.item_id, "item_id")
        _translation_identifier(self.layer_id, "layer_id")
        _translation_identifier(self.representation_id, "representation_id")
        canvases = tuple(self.canvases)
        if any(not isinstance(value, TranslationSourceCanvas) for value in canvases):
            raise TypeError("canvases must contain TranslationSourceCanvas values")
        selectors = [value.selector for value in canvases]
        if len(selectors) != len(set(selectors)):
            raise ValueError("source canvases contain duplicate selectors")
        object.__setattr__(
            self,
            "canvases",
            tuple(sorted(canvases, key=lambda value: (value.order, value.selector))),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "layer_id": self.layer_id,
            "representation_id": self.representation_id,
            "canvases": [value.as_dict() for value in self.canvases],
        }


@dataclass(frozen=True, slots=True)
class TranslationSourceRef:
    layer_id: str
    representation_id: str
    revision: str
    available: bool = True

    def __post_init__(self) -> None:
        _translation_identifier(self.layer_id, "layer_id")
        if self.available:
            _translation_identifier(self.representation_id, "representation_id")
        elif self.representation_id:
            _translation_identifier(self.representation_id, "representation_id")
        _translation_revision(self.revision, "source revision")
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")

    def as_dict(self) -> dict[str, object]:
        return {
            "layer_id": self.layer_id,
            "representation_id": self.representation_id,
            "revision": self.revision,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class TranslationPageRecord:
    """Repository form of one translated selector."""

    selector: str
    text: str
    source_revision: str = ""
    source_layer_id: str = ""
    origin: TranslationPageOrigin = "unknown"
    review_state: TranslationReviewState = "unreviewed"
    provider_id: str = ""
    model: str = ""
    recipe_revision: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        _translation_identifier(self.selector, "selector")
        _translation_string(self.text, "text")
        _translation_revision(self.source_revision, "source_revision", optional=True)
        if self.source_layer_id:
            _translation_identifier(self.source_layer_id, "source_layer_id")
        if self.origin not in _TRANSLATION_ORIGINS:
            raise ValueError("origin is invalid")
        if self.review_state not in _TRANSLATION_REVIEW_STATES:
            raise ValueError("review_state is invalid")
        if self.provider_id:
            _translation_identifier(self.provider_id, "provider_id")
        _translation_string(self.model, "model", max_length=256)
        _translation_revision(self.recipe_revision, "recipe_revision", optional=True)
        _translation_string(self.updated_at, "updated_at", max_length=128)

    def as_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "text": self.text,
            "source_revision": self.source_revision,
            "source_layer_id": self.source_layer_id,
            "origin": self.origin,
            "review_state": self.review_state,
            "provider_id": self.provider_id,
            "model": self.model,
            "recipe_revision": self.recipe_revision,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TranslationAggregate:
    """Storage-neutral translation aggregate used by repository ports."""

    translation_id: str
    item_id: str
    target_language: str
    source_layer_id: str
    pages: tuple[TranslationPageRecord, ...] = ()

    def __post_init__(self) -> None:
        _translation_identifier(self.translation_id, "translation_id")
        _translation_identifier(self.item_id, "item_id")
        _translation_language(self.target_language)
        _translation_identifier(self.source_layer_id, "source_layer_id")
        pages = tuple(self.pages)
        if any(not isinstance(value, TranslationPageRecord) for value in pages):
            raise TypeError("pages must contain TranslationPageRecord values")
        selectors = [value.selector for value in pages]
        if len(selectors) != len(set(selectors)):
            raise ValueError("translation pages contain duplicate selectors")
        object.__setattr__(
            self, "pages", tuple(sorted(pages, key=lambda value: value.selector))
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.translation_id,
            "item_id": self.item_id,
            "target_language": self.target_language,
            "source_layer_id": self.source_layer_id,
            "pages": [value.as_dict() for value in self.pages],
        }


def _translation_selectors(values: Sequence[str], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of selectors")
    selectors = tuple(values)
    for value in selectors:
        _translation_identifier(value, f"{name} selector")
    if len(selectors) != len(set(selectors)):
        raise ValueError(f"{name} contains duplicate selectors")
    return selectors


@dataclass(frozen=True, slots=True)
class TranslationStatus:
    """Disjoint selector sets describing one translation against its source."""

    current: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    untracked: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    orphaned: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        groups = {}
        for name in ("current", "stale", "untracked", "missing", "orphaned"):
            values = _translation_selectors(getattr(self, name), name)
            groups[name] = values
            object.__setattr__(self, name, values)
        seen: set[str] = set()
        for name, values in groups.items():
            overlap = seen.intersection(values)
            if overlap:
                raise ValueError(
                    "translation status selector appears in multiple states: "
                    f"{sorted(overlap)[0]} ({name})"
                )
            seen.update(values)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "current": list(self.current),
            "stale": list(self.stale),
            "untracked": list(self.untracked),
            "missing": list(self.missing),
            "orphaned": list(self.orphaned),
        }


@dataclass(frozen=True, slots=True)
class TranslationPageView:
    selector: str
    order: int | None
    label: str
    source_text: str
    text: str
    state: TranslationPageState
    source_revision: str = ""
    source_layer_id: str = ""
    origin: TranslationPageOrigin = "unknown"
    review_state: TranslationReviewState = "unreviewed"
    provider_id: str = ""
    model: str = ""
    recipe_revision: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        _translation_identifier(self.selector, "selector")
        if self.order is not None and (
            not isinstance(self.order, int)
            or isinstance(self.order, bool)
            or self.order < 0
        ):
            raise ValueError("order must be a non-negative integer or None")
        _translation_string(self.label, "label", max_length=256)
        _translation_string(self.source_text, "source_text")
        _translation_string(self.text, "text")
        if self.state not in _TRANSLATION_PAGE_STATES:
            raise ValueError("state is invalid")
        TranslationPageRecord(
            selector=self.selector,
            text=self.text,
            source_revision=self.source_revision,
            source_layer_id=self.source_layer_id,
            origin=self.origin,
            review_state=self.review_state,
            provider_id=self.provider_id,
            model=self.model,
            recipe_revision=self.recipe_revision,
            updated_at=self.updated_at,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "order": self.order,
            "label": self.label,
            "source_text": self.source_text,
            "text": self.text,
            "state": self.state,
            "source_revision": self.source_revision,
            "source_layer_id": self.source_layer_id,
            "origin": self.origin,
            "review_state": self.review_state,
            "provider_id": self.provider_id,
            "model": self.model,
            "recipe_revision": self.recipe_revision,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TranslationSummaryView:
    translation_id: str
    item_id: str
    target_language: str
    document_revision: str
    view_revision: str
    source: TranslationSourceRef
    page_count: int
    status: TranslationStatus

    def __post_init__(self) -> None:
        _translation_identifier(self.translation_id, "translation_id")
        _translation_identifier(self.item_id, "item_id")
        _translation_language(self.target_language)
        _translation_revision(self.document_revision, "document_revision")
        _translation_revision(self.view_revision, "view_revision")
        if not isinstance(self.source, TranslationSourceRef):
            raise TypeError("source must be a TranslationSourceRef")
        if (
            not isinstance(self.page_count, int)
            or isinstance(self.page_count, bool)
            or self.page_count < 0
        ):
            raise ValueError("page_count must be a non-negative integer")
        if not isinstance(self.status, TranslationStatus):
            raise TypeError("status must be a TranslationStatus")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.translation_id,
            "item_id": self.item_id,
            "target_language": self.target_language,
            "document_revision": self.document_revision,
            "view_revision": self.view_revision,
            "source": self.source.as_dict(),
            "page_count": self.page_count,
            "status": self.status.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class TranslationDocumentView:
    translation_id: str
    item_id: str
    target_language: str
    document_revision: str
    view_revision: str
    source: TranslationSourceRef
    pages: tuple[TranslationPageView, ...]
    status: TranslationStatus

    def __post_init__(self) -> None:
        pages = tuple(self.pages)
        if any(not isinstance(value, TranslationPageView) for value in pages):
            raise TypeError("pages must contain TranslationPageView values")
        selectors = [value.selector for value in pages]
        if len(selectors) != len(set(selectors)):
            raise ValueError("translation page views contain duplicate selectors")
        TranslationSummaryView(
            translation_id=self.translation_id,
            item_id=self.item_id,
            target_language=self.target_language,
            document_revision=self.document_revision,
            view_revision=self.view_revision,
            source=self.source,
            page_count=sum(bool(value.text.strip()) for value in pages),
            status=self.status,
        )
        states = {
            selector: state
            for state in ("current", "stale", "untracked", "missing", "orphaned")
            for selector in getattr(self.status, state)
        }
        if set(selectors) != set(states):
            raise ValueError("translation page and status selectors must match")
        if any(value.state != states[value.selector] for value in pages):
            raise ValueError("translation page state contradicts status")
        object.__setattr__(self, "pages", pages)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.translation_id,
            "item_id": self.item_id,
            "target_language": self.target_language,
            "document_revision": self.document_revision,
            "view_revision": self.view_revision,
            "source": self.source.as_dict(),
            "pages": [value.as_dict() for value in self.pages],
            "status": self.status.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReplaceTranslationPageCommand:
    """Human page edit with independent document and source preconditions."""

    item_id: str
    translation_id: str
    selector: str
    text: str
    expected_document_revision: str
    expected_source_revision: str

    def __post_init__(self) -> None:
        _translation_identifier(self.item_id, "item_id")
        _translation_identifier(self.translation_id, "translation_id")
        _translation_identifier(self.selector, "selector")
        _translation_string(self.text, "text")
        _translation_revision(
            self.expected_document_revision, "expected_document_revision"
        )
        _translation_revision(
            self.expected_source_revision, "expected_source_revision"
        )


__all__ = [
    "ReplaceTranslationPageCommand",
    "TranslationAggregate",
    "TranslationDocumentView",
    "TranslationPageOrigin",
    "TranslationPageRecord",
    "TranslationPageState",
    "TranslationPageView",
    "TranslationReviewState",
    "TranslationSourceCanvas",
    "TranslationSourceRef",
    "TranslationSourceSnapshot",
    "TranslationStatus",
    "TranslationSummaryView",
]
