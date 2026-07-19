"""Command and result contracts shared by engine clients and adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


JsonMap = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ItemDescriptor:
    item_id: str
    sources: tuple[str, ...] = ("primary",)
    metadata: JsonMap = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PageKey:
    item_id: str
    source_id: str = "primary"
    page: int = 1


@dataclass(frozen=True, slots=True)
class ReplaceRegionPageCommand:
    key: PageKey
    expected_revision: str
    items: Sequence[JsonMap]
    doc: str = "compiled.txt"
    dims: JsonMap = field(default_factory=dict)
    state: str = ""
    # Existing extension data is retained by default.  Set preserve_ext=False
    # to replace it, including replacing it with an empty mapping.
    preserve_ext: bool = True
    ext: JsonMap = field(default_factory=dict)


class ProposalAction(str, Enum):
    APPLY = "apply"
    DISMISS = "dismiss"


@dataclass(frozen=True, slots=True)
class ReviewRegionProposalCommand:
    key: PageKey
    action: ProposalAction | str
    expected_region_revision: str
    expected_proposal_revision: str


@dataclass(frozen=True, slots=True)
class LayoutFamilyQuery:
    item_id: str
    source_id: str = "primary"
    options: JsonMap = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RecompileRegionPagesCommand:
    item_id: str
    source_id: str = "primary"
    layer: str = "text"
    page: int | None = None
    target: str = ""


@dataclass(frozen=True, slots=True)
class RecompileRegionPagesResult:
    pages: int
    documents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegionPageView:
    key: PageKey
    found: bool
    revision: str
    doc: str = ""
    dims: JsonMap = field(default_factory=dict)
    state: str = ""
    stale: JsonMap = field(default_factory=dict)
    ext: JsonMap = field(default_factory=dict)
    items: tuple[JsonMap, ...] = ()
    proposal: JsonMap | None = None
    compile_pending: JsonMap | None = None


@dataclass(frozen=True, slots=True)
class FailureDetail:
    code: str
    message: str
    details: JsonMap = field(default_factory=dict)
    retryable: bool = True

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        code: str,
        details: JsonMap | None = None,
        retryable: bool = True,
    ) -> "FailureDetail":
        if hasattr(exc, "as_dict"):
            payload = exc.as_dict()  # type: ignore[union-attr]
            merged = dict(payload.get("details") or {})
            merged.update(details or {})
            return cls(
                code=str(payload.get("code") or code),
                message=str(payload.get("message") or exc),
                details=merged,
                retryable=bool(payload.get("retryable", retryable)),
            )
        return cls(
            code=code,
            message=str(exc),
            details=dict(details or {}),
            retryable=retryable,
        )


@dataclass(frozen=True, slots=True)
class ProposalReviewResult:
    action: ProposalAction
    page: RegionPageView
    compiled: bool
    derived_failure: FailureDetail | None = None


@dataclass(frozen=True, slots=True)
class LayoutFamilyResult:
    capability: str
    proposal: JsonMap


@dataclass(frozen=True, slots=True)
class TranslationPageCommand:
    item_id: str
    language: str
    page: int
    text: str
    expected_revision: str
    source_text: str = ""
    source_layer: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class TranslationDocumentView:
    item_id: str
    language: str
    revision: str
    pages: JsonMap


@dataclass(frozen=True, slots=True)
class TranslationStatus:
    stale: tuple[int, ...] = ()
    untracked: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()
    orphaned: tuple[int, ...] = ()
