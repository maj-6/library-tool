"""Provider-neutral OCR follow-up jobs for immutable correction outputs.

The image transform owns publication of the corrected raster.  This module
starts a separate, observable child job against that exact committed
``ocr-ready`` rendition and can only publish a machine proposal.  It has no
port for replacing canonical text or human assertions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import threading
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .correction_transforms import (
    CommittedCorrectionOutput,
    CorrectionTransformCancelled,
    CorrectionTransformHooksPort,
    OcrFollowupOutcome,
    OcrFollowupRequest,
    OcrFollowupState,
)
from .jobs import JobFailure, JobManager, JobOutput, JobProgress, JobState, JobView
from .errors import ConflictError, EngineError, ValidationError


CORRECTION_OCR_JOB_KIND = "correction.ocr-followup"
CORRECTION_OCR_PROPOSAL_POLICY = "machine-proposal-only"
CORRECTION_OCR_MAX_SOURCE_BYTES = 256 * 1024 * 1024
CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT = 10_000
CORRECTION_OCR_PROPOSAL_CATALOG_PAGE_LIMIT = 256
_PORTABLE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PROPOSAL_REF = re.compile(r"^cop-[0-9a-f]{40}$")
_PROPOSAL_CATALOG_REVISION = re.compile(r"^cops-[0-9a-f]{64}$")
_PROPOSAL_CATALOG_CURSOR = re.compile(r"^copc-[A-Za-z0-9_-]{1,2043}$")
_PROPOSAL_CATALOG_CURSOR_SCHEMA = (
    "librarytool.correction-ocr-proposal-cursor/1"
)
_PROPOSAL_CATALOG_SNAPSHOT_SCHEMA = (
    "librarytool.correction-ocr-proposal-catalog-snapshot/1"
)
_MAX_PROPOSAL_CATALOG_SNAPSHOT_BYTES = 32 * 1024 * 1024
_MAX_RECOGNITION_BYTES = 64 * 1024 * 1024
_MAX_JSON_DEPTH = 32
_MAX_JSON_NODES = 200_000
_MAX_PROVIDER_OPTIONS_BYTES = 64 * 1024
_PUBLIC_PRIVATE_KEYS = frozenset(
    {
        "absolute_path",
        "api_key",
        "asset_ref",
        "credential",
        "file",
        "file_name",
        "filename",
        "filepath",
        "local_path",
        "locator",
        "password",
        "path",
        "resource_ref",
        "secret",
        "secret_key",
        "storage_key",
        "storage_locator",
        "storage_path",
        "token",
        "uri",
        "url",
    }
)

log = logging.getLogger(__name__)


def _portable(value: object, field_name: str, *, optional: bool = False) -> str:
    text = value if isinstance(value, str) else ""
    if optional and not text:
        return ""
    if _PORTABLE_VALUE.fullmatch(text) is None:
        raise ValueError(f"{field_name} must be a portable identifier")
    return text


def _freeze_json(
    value: Any,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> Any:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > _MAX_JSON_NODES:
        raise ValueError("OCR recognition payload contains too many values")
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("OCR recognition payload is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("OCR recognition payload contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("OCR recognition payload keys must be strings")
            frozen[key] = _freeze_json(
                item,
                depth=depth + 1,
                budget=budget,
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, depth=depth + 1, budget=budget)
            for item in value
        )
    raise ValueError("OCR recognition payload must contain only JSON values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalized_public_key(value: str) -> str:
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").casefold()


def _require_public_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_public_key(key)
            if (
                normalized in _PUBLIC_PRIVATE_KEYS
                or normalized.rsplit("_", 1)[-1]
                in {"credential", "file", "filename", "filepath", "path", "uri", "url"}
            ):
                raise ValueError(
                    "OCR proposal payload contains private execution metadata"
                )
            _require_public_payload(item)
    elif isinstance(value, tuple):
        for item in value:
            _require_public_payload(item)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class CorrectionOcrProviderSelection:
    """Credential-free provider identity pinned before recognition."""

    provider_id: str
    model: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _portable(self.provider_id, "provider_id"),
        )
        if not isinstance(self.model, str) or len(self.model.strip()) > 512:
            raise ValueError("model must be a bounded string")
        object.__setattr__(self, "model", self.model.strip())
        if not isinstance(self.options, Mapping):
            raise TypeError("options must be a mapping")
        frozen = _freeze_json(self.options)
        if len(_canonical_json(_thaw_json(frozen))) > _MAX_PROVIDER_OPTIONS_BYTES:
            raise ValueError("provider options exceed their size budget")
        object.__setattr__(self, "options", frozen)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "options": _thaw_json(self.options),
        }


@dataclass(frozen=True, slots=True)
class CorrectionOcrRecognition:
    """One provider response normalized to immutable, provider-neutral JSON."""

    provider_id: str
    model: str
    payload: Mapping[str, Any]
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _portable(self.provider_id, "provider_id"),
        )
        if not isinstance(self.model, str) or len(self.model.strip()) > 512:
            raise ValueError("model must be a bounded string")
        object.__setattr__(self, "model", self.model.strip())
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        if not isinstance(self.options, Mapping):
            raise TypeError("options must be a mapping")
        frozen = _freeze_json(self.payload)
        encoded = _canonical_json(_thaw_json(frozen))
        if len(encoded) > _MAX_RECOGNITION_BYTES:
            raise ValueError("OCR recognition payload exceeds its size budget")
        object.__setattr__(self, "payload", frozen)
        frozen_options = _freeze_json(self.options)
        if (
            len(_canonical_json(_thaw_json(frozen_options)))
            > _MAX_PROVIDER_OPTIONS_BYTES
        ):
            raise ValueError("provider options exceed their size budget")
        object.__setattr__(self, "options", frozen_options)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
            "options": _thaw_json(self.options),
            "payload": _thaw_json(self.payload),
        }


@dataclass(frozen=True, slots=True)
class StoredCorrectionOcrProposal:
    """Durable proposal receipt returned without exposing a filesystem path."""

    proposal_ref: str
    source: CommittedCorrectionOutput
    provider: CorrectionOcrProviderSelection

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proposal_ref",
            _portable(self.proposal_ref, "proposal_ref"),
        )
        if not isinstance(self.source, CommittedCorrectionOutput):
            raise TypeError("source must be a CommittedCorrectionOutput")
        if self.source.kind != "ocr-ready":
            raise ValueError("proposal source must be the OCR-ready rendition")
        if not isinstance(self.provider, CorrectionOcrProviderSelection):
            raise TypeError("provider must be a CorrectionOcrProviderSelection")

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_ref": self.proposal_ref,
            "source": self.source.as_dict(),
            "provider": self.provider.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CorrectionOcrProposalProviderView:
    """Public provider identity with execution options deliberately omitted."""

    provider_id: str
    model: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _portable(self.provider_id, "provider_id"),
        )
        if not isinstance(self.model, str) or len(self.model.strip()) > 512:
            raise ValueError("model must be a bounded string")
        object.__setattr__(self, "model", self.model.strip())

    def as_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "model": self.model,
        }


@dataclass(frozen=True, slots=True)
class CorrectionOcrProposalView:
    """Immutable OCR proposal resource without private execution metadata."""

    proposal_ref: str
    item_id: str
    operation_id: str
    source: CommittedCorrectionOutput
    provider: CorrectionOcrProposalProviderView
    recognition: Mapping[str, Any]
    publication_policy: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proposal_ref",
            _portable(self.proposal_ref, "proposal_ref"),
        )
        if _PROPOSAL_REF.fullmatch(self.proposal_ref) is None:
            raise ValueError("proposal_ref must be an opaque OCR proposal reference")
        object.__setattr__(self, "item_id", _portable(self.item_id, "item_id"))
        object.__setattr__(
            self,
            "operation_id",
            _portable(self.operation_id, "operation_id"),
        )
        if not isinstance(self.source, CommittedCorrectionOutput):
            raise TypeError("source must be a CommittedCorrectionOutput")
        if self.source.kind != "ocr-ready":
            raise ValueError("proposal source must be the OCR-ready rendition")
        if not isinstance(self.provider, CorrectionOcrProposalProviderView):
            raise TypeError(
                "provider must be a CorrectionOcrProposalProviderView"
            )
        if not isinstance(self.recognition, Mapping):
            raise TypeError("recognition must be a mapping")
        recognition = _freeze_json(self.recognition)
        _require_public_payload(recognition)
        if len(_canonical_json(_thaw_json(recognition))) > _MAX_RECOGNITION_BYTES:
            raise ValueError("OCR recognition payload exceeds its size budget")
        object.__setattr__(self, "recognition", recognition)
        if self.publication_policy != CORRECTION_OCR_PROPOSAL_POLICY:
            raise ValueError("publication_policy must be machine-proposal-only")
        if (
            not isinstance(self.content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_ref": self.proposal_ref,
            "item_id": self.item_id,
            "operation_id": self.operation_id,
            "source": self.source.as_dict(),
            "provider": self.provider.as_dict(),
            "recognition": _thaw_json(self.recognition),
            "publication_policy": self.publication_policy,
            "content_sha256": self.content_sha256,
        }


class CorrectionOcrProposalAvailability(str, Enum):
    """Safe public state for one catalogued OCR proposal."""

    AVAILABLE = "available"


@dataclass(frozen=True, slots=True)
class CorrectionOcrProposalSummaryView:
    """Bounded catalog row without recognition data or execution options."""

    proposal_ref: str
    operation_id: str
    source: CommittedCorrectionOutput
    provider: CorrectionOcrProposalProviderView
    publication_policy: str
    content_sha256: str
    availability: CorrectionOcrProposalAvailability = (
        CorrectionOcrProposalAvailability.AVAILABLE
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "proposal_ref",
            _portable(self.proposal_ref, "proposal_ref"),
        )
        if _PROPOSAL_REF.fullmatch(self.proposal_ref) is None:
            raise ValueError("proposal_ref must be an opaque OCR proposal reference")
        object.__setattr__(
            self,
            "operation_id",
            _portable(self.operation_id, "operation_id"),
        )
        if not isinstance(self.source, CommittedCorrectionOutput):
            raise TypeError("source must be a CommittedCorrectionOutput")
        if self.source.kind != "ocr-ready":
            raise ValueError("proposal source must be the OCR-ready rendition")
        if not isinstance(self.provider, CorrectionOcrProposalProviderView):
            raise TypeError(
                "provider must be a CorrectionOcrProposalProviderView"
            )
        if self.publication_policy != CORRECTION_OCR_PROPOSAL_POLICY:
            raise ValueError("publication_policy must be machine-proposal-only")
        if (
            not isinstance(self.content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.availability, CorrectionOcrProposalAvailability):
            raise TypeError(
                "availability must be a CorrectionOcrProposalAvailability"
            )

    @classmethod
    def from_proposal(
        cls,
        proposal: CorrectionOcrProposalView,
    ) -> CorrectionOcrProposalSummaryView:
        if not isinstance(proposal, CorrectionOcrProposalView):
            raise TypeError("proposal must be a CorrectionOcrProposalView")
        return cls(
            proposal_ref=proposal.proposal_ref,
            operation_id=proposal.operation_id,
            source=proposal.source,
            provider=proposal.provider,
            publication_policy=proposal.publication_policy,
            content_sha256=proposal.content_sha256,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposal_ref": self.proposal_ref,
            "operation_id": self.operation_id,
            "source": self.source.as_dict(),
            "provider": self.provider.as_dict(),
            "publication_policy": self.publication_policy,
            "content_sha256": self.content_sha256,
            "availability": self.availability.value,
        }


def _proposal_catalog_revision(
    item_id: str,
    proposals: tuple[CorrectionOcrProposalSummaryView, ...],
) -> str:
    encoded = _canonical_json(
        {
            "schema": _PROPOSAL_CATALOG_SNAPSHOT_SCHEMA,
            "item_id": item_id,
            "proposals": [proposal.as_dict() for proposal in proposals],
        }
    )
    if len(encoded) > _MAX_PROPOSAL_CATALOG_SNAPSHOT_BYTES:
        raise ValueError("OCR proposal catalog snapshot exceeds its size budget")
    return "cops-" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CorrectionOcrProposalCatalogSnapshot:
    """One deterministic, verified item-scoped proposal snapshot."""

    item_id: str
    proposals: tuple[CorrectionOcrProposalSummaryView, ...] = ()
    snapshot_revision: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _portable(self.item_id, "item_id"))
        if not isinstance(self.proposals, tuple):
            raise TypeError("proposals must be a tuple")
        if len(self.proposals) > CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT:
            raise ValueError("OCR proposal catalog exceeds its count budget")
        if any(
            not isinstance(proposal, CorrectionOcrProposalSummaryView)
            for proposal in self.proposals
        ):
            raise TypeError(
                "proposals must contain CorrectionOcrProposalSummaryView values"
            )
        references = tuple(proposal.proposal_ref for proposal in self.proposals)
        if references != tuple(sorted(references)) or len(references) != len(
            set(references)
        ):
            raise ValueError(
                "OCR proposal catalog must use unique deterministic ordering"
            )
        object.__setattr__(
            self,
            "snapshot_revision",
            _proposal_catalog_revision(self.item_id, self.proposals),
        )


@dataclass(frozen=True, slots=True)
class CorrectionOcrProposalPageView:
    """One bounded page pinned to a complete catalog snapshot."""

    item_id: str
    snapshot_revision: str
    proposals: tuple[CorrectionOcrProposalSummaryView, ...]
    next_cursor: str | None
    total: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", _portable(self.item_id, "item_id"))
        if (
            not isinstance(self.snapshot_revision, str)
            or _PROPOSAL_CATALOG_REVISION.fullmatch(self.snapshot_revision) is None
        ):
            raise ValueError(
                "snapshot_revision must be an OCR proposal catalog revision"
            )
        if not isinstance(self.proposals, tuple):
            raise TypeError("proposals must be a tuple")
        if (
            len(self.proposals) > CORRECTION_OCR_PROPOSAL_CATALOG_PAGE_LIMIT
            or any(
                not isinstance(proposal, CorrectionOcrProposalSummaryView)
                for proposal in self.proposals
            )
        ):
            raise ValueError("proposals must be a bounded tuple of summary views")
        references = tuple(proposal.proposal_ref for proposal in self.proposals)
        if references != tuple(sorted(references)) or len(references) != len(
            set(references)
        ):
            raise ValueError("proposal page ordering is invalid")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str)
            or len(self.next_cursor) > 2048
            or _PROPOSAL_CATALOG_CURSOR.fullmatch(self.next_cursor) is None
        ):
            raise ValueError("next_cursor must be an opaque catalog cursor or None")
        if (
            isinstance(self.total, bool)
            or not isinstance(self.total, int)
            or self.total < len(self.proposals)
            or self.total > CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT
        ):
            raise ValueError("total is outside the OCR proposal catalog budget")

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "snapshot_revision": self.snapshot_revision,
            "proposals": [proposal.as_dict() for proposal in self.proposals],
            "next_cursor": self.next_cursor,
            "total": self.total,
        }


@runtime_checkable
class CorrectionOcrProposalQueryRepositoryPort(Protocol):
    """Resolve one immutable proposal without exposing storage addressing."""

    def get_proposal(
        self,
        item_id: str,
        proposal_ref: str,
    ) -> CorrectionOcrProposalView | None: ...


@runtime_checkable
class CorrectionOcrProposalCatalogRepositoryPort(Protocol):
    """Enumerate a verified immutable proposal snapshot for one item."""

    def list_proposals(
        self,
        item_id: str,
    ) -> CorrectionOcrProposalCatalogSnapshot: ...


class CorrectionOcrProposalQueryService:
    """Read OCR proposal artifacts through item-scoped opaque references."""

    def __init__(self, repository: CorrectionOcrProposalQueryRepositoryPort) -> None:
        if not isinstance(repository, CorrectionOcrProposalQueryRepositoryPort):
            raise TypeError(
                "repository must implement "
                "CorrectionOcrProposalQueryRepositoryPort"
            )
        self._repository = repository

    def get_proposal(
        self,
        item_id: str,
        proposal_ref: str,
    ) -> CorrectionOcrProposalView | None:
        try:
            item = _portable(item_id, "item_id")
            reference = _portable(proposal_ref, "proposal_ref")
            if _PROPOSAL_REF.fullmatch(reference) is None:
                raise ValueError(
                    "proposal_ref must be an opaque OCR proposal reference"
                )
        except ValueError as exc:
            raise ValidationError(
                "the OCR proposal query is invalid",
                code="invalid_correction_ocr_proposal_query",
            ) from exc
        proposal = self._repository.get_proposal(item, reference)
        if proposal is not None and (
            not isinstance(proposal, CorrectionOcrProposalView)
            or proposal.item_id != item
            or proposal.proposal_ref != reference
        ):
            raise TypeError("OCR proposal repository returned an invalid view")
        return proposal


def _encode_proposal_catalog_cursor(
    *,
    item_id: str,
    snapshot_revision: str,
    after_ref: str,
) -> str:
    payload = _canonical_json(
        {
            "schema": _PROPOSAL_CATALOG_CURSOR_SCHEMA,
            "item_sha256": hashlib.sha256(item_id.encode("utf-8")).hexdigest(),
            "snapshot_revision": snapshot_revision,
            "after_ref": after_ref,
        }
    )
    return "copc-" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_proposal_catalog_cursor(
    token: str,
    *,
    item_id: str,
) -> tuple[str, str]:
    if len(token) > 2048 or _PROPOSAL_CATALOG_CURSOR.fullmatch(token) is None:
        raise ValueError("cursor must be an opaque OCR proposal catalog cursor")
    encoded = token.removeprefix("copc-")
    try:
        padding = "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(decoded.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "cursor must be an opaque OCR proposal catalog cursor"
        ) from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", "item_sha256", "snapshot_revision", "after_ref"}
        or value.get("schema") != _PROPOSAL_CATALOG_CURSOR_SCHEMA
        or value.get("item_sha256")
        != hashlib.sha256(item_id.encode("utf-8")).hexdigest()
        or not isinstance(value.get("snapshot_revision"), str)
        or _PROPOSAL_CATALOG_REVISION.fullmatch(value["snapshot_revision"]) is None
        or not isinstance(value.get("after_ref"), str)
        or _PROPOSAL_REF.fullmatch(value["after_ref"]) is None
        or token
        != _encode_proposal_catalog_cursor(
            item_id=item_id,
            snapshot_revision=value["snapshot_revision"],
            after_ref=value["after_ref"],
        )
    ):
        raise ValueError("cursor must be an opaque OCR proposal catalog cursor")
    return value["snapshot_revision"], value["after_ref"]


class CorrectionOcrProposalCatalogService:
    """Page public proposal summaries through revision-pinned cursors."""

    def __init__(
        self,
        repository: CorrectionOcrProposalCatalogRepositoryPort,
    ) -> None:
        if not isinstance(repository, CorrectionOcrProposalCatalogRepositoryPort):
            raise TypeError(
                "repository must implement "
                "CorrectionOcrProposalCatalogRepositoryPort"
            )
        self._repository = repository

    def list_proposals(
        self,
        item_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        snapshot_revision: str | None = None,
    ) -> CorrectionOcrProposalPageView:
        try:
            item = _portable(item_id, "item_id")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or limit < 1
                or limit > CORRECTION_OCR_PROPOSAL_CATALOG_PAGE_LIMIT
            ):
                raise ValueError("limit is outside the catalog page budget")
            if snapshot_revision is not None and (
                not isinstance(snapshot_revision, str)
                or _PROPOSAL_CATALOG_REVISION.fullmatch(snapshot_revision) is None
            ):
                raise ValueError("snapshot_revision is invalid")
            cursor_revision = ""
            after_ref = ""
            if cursor is not None:
                if not isinstance(cursor, str) or not cursor:
                    raise ValueError("cursor is invalid")
                cursor_revision, after_ref = _decode_proposal_catalog_cursor(
                    cursor,
                    item_id=item,
                )
                if (
                    snapshot_revision is not None
                    and cursor_revision != snapshot_revision
                ):
                    raise ValueError(
                        "cursor and snapshot_revision identify different snapshots"
                    )
        except ValueError as exc:
            raise ValidationError(
                "the OCR proposal catalog query is invalid",
                code="invalid_correction_ocr_proposal_catalog_query",
            ) from exc

        snapshot = self._repository.list_proposals(item)
        if (
            not isinstance(snapshot, CorrectionOcrProposalCatalogSnapshot)
            or snapshot.item_id != item
        ):
            raise TypeError(
                "OCR proposal catalog repository returned an invalid snapshot"
            )
        expected_revision = cursor_revision or snapshot_revision
        if (
            expected_revision is not None
            and expected_revision
            and snapshot.snapshot_revision != expected_revision
        ):
            raise ConflictError(
                "the OCR proposal catalog changed while it was being paged",
                code="correction_ocr_proposal_catalog_changed",
                details={
                    "expected_revision": expected_revision,
                    "actual_revision": snapshot.snapshot_revision,
                },
            )

        offset = 0
        if after_ref:
            references = tuple(
                proposal.proposal_ref for proposal in snapshot.proposals
            )
            try:
                offset = references.index(after_ref) + 1
            except ValueError as exc:
                raise ValidationError(
                    "the OCR proposal catalog query is invalid",
                    code="invalid_correction_ocr_proposal_catalog_query",
                ) from exc
            if offset >= len(snapshot.proposals):
                raise ValidationError(
                    "the OCR proposal catalog query is invalid",
                    code="invalid_correction_ocr_proposal_catalog_query",
                )

        proposals = snapshot.proposals[offset : offset + limit]
        next_offset = offset + len(proposals)
        next_cursor = (
            _encode_proposal_catalog_cursor(
                item_id=item,
                snapshot_revision=snapshot.snapshot_revision,
                after_ref=proposals[-1].proposal_ref,
            )
            if proposals and next_offset < len(snapshot.proposals)
            else None
        )
        return CorrectionOcrProposalPageView(
            item_id=item,
            snapshot_revision=snapshot.snapshot_revision,
            proposals=proposals,
            next_cursor=next_cursor,
            total=len(snapshot.proposals),
        )


@runtime_checkable
class CorrectionOcrProviderPort(Protocol):
    """Select and invoke OCR without owning proposal persistence."""

    def select_provider(self) -> CorrectionOcrProviderSelection: ...

    def recognize(
        self,
        selection: CorrectionOcrProviderSelection,
        content: bytes,
        hooks: CorrectionTransformHooksPort,
    ) -> CorrectionOcrRecognition: ...


@runtime_checkable
class CorrectionOcrProposalRepositoryPort(Protocol):
    """Read the exact rendition and write only an immutable machine proposal."""

    def read_source(self, request: OcrFollowupRequest) -> bytes: ...

    def find_proposal(
        self,
        request: OcrFollowupRequest,
    ) -> StoredCorrectionOcrProposal | None: ...

    def commit_proposal(
        self,
        request: OcrFollowupRequest,
        recognition: CorrectionOcrRecognition,
    ) -> StoredCorrectionOcrProposal: ...


class _ChildHooks:
    def __init__(
        self,
        jobs: JobManager,
        record: MutableMapping[str, Any],
        parent: CorrectionTransformHooksPort,
    ) -> None:
        self._jobs = jobs
        self._record = record
        self._parent = parent

    def is_cancelled(self) -> bool:
        return self._parent.is_cancelled() or self._jobs.is_cancelled(self._record)

    def report_progress(self, progress: JobProgress) -> None:
        status = "cancelling" if self.is_cancelled() else "running"
        self._jobs.transition(
            self._record,
            status,
            done=progress.completed,
            total=progress.total,
            progress=progress.as_dict(),
            note=progress.phase,
        )


class CorrectionOcrFollowupService:
    """Run one durable, observable OCR proposal child job."""

    def __init__(
        self,
        jobs: JobManager,
        repository: CorrectionOcrProposalRepositoryPort,
        provider: CorrectionOcrProviderPort,
    ) -> None:
        if not isinstance(jobs, JobManager):
            raise TypeError("jobs must be a JobManager")
        if not isinstance(repository, CorrectionOcrProposalRepositoryPort):
            raise TypeError(
                "repository must implement CorrectionOcrProposalRepositoryPort"
            )
        if not isinstance(provider, CorrectionOcrProviderPort):
            raise TypeError("provider must implement CorrectionOcrProviderPort")
        self._jobs = jobs
        self._repository = repository
        self._provider = provider
        self._lock = threading.Lock()

    @staticmethod
    def job_id_for(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
        return f"correction-ocr-{digest}"

    @staticmethod
    def child_operation_id(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return f"correction-ocr:{digest[:48]}"

    @staticmethod
    def _command_sha256(request: OcrFollowupRequest) -> str:
        return hashlib.sha256(_canonical_json(request.as_dict())).hexdigest()

    def find_ocr_followup(
        self,
        request: OcrFollowupRequest,
    ) -> OcrFollowupOutcome | None:
        """Read a durable/terminal child outcome without starting provider work."""

        if not isinstance(request, OcrFollowupRequest):
            raise TypeError("request must be an OcrFollowupRequest")
        with self._lock:
            try:
                existing = self._existing_job(request)
                if existing is not None:
                    self._validate_existing(existing, request)
                stored = self._repository.find_proposal(request)
                if stored is not None:
                    self._validate_stored(stored, request)
                    if existing is not None:
                        pin_failure = self._stored_provider_pin_failure(
                            existing,
                            stored,
                        )
                        if pin_failure is not None:
                            raise EngineError(
                                pin_failure.message,
                                code=pin_failure.code,
                                details=pin_failure.details,
                                retryable=pin_failure.retryable,
                            )
                    return OcrFollowupOutcome(
                        OcrFollowupState.SUCCEEDED,
                        source=stored.source,
                        proposal_ref=stored.proposal_ref,
                    )
                if existing is None or existing.state in {
                    JobState.QUEUED,
                    JobState.RUNNING,
                    JobState.CANCELLING,
                    JobState.INTERRUPTED,
                }:
                    return None
                if existing.state is JobState.DONE:
                    return OcrFollowupOutcome(
                        OcrFollowupState.FAILED,
                        source=request.source,
                        failure=JobFailure(
                            "ocr_proposal_missing",
                            "the OCR child job has no durable proposal",
                            retryable=False,
                        ),
                    )
                return self._outcome_from_job(existing, request.source)
            except EngineError:
                raise
            except (TypeError, ValueError) as exc:
                raise ConflictError(
                    "durable OCR child state does not match its parent request",
                    code="correction_ocr_reconciliation_conflict",
                    details={"cause_type": type(exc).__name__},
                ) from exc

    def run_ocr_followup(
        self,
        request: OcrFollowupRequest,
        hooks: CorrectionTransformHooksPort,
    ) -> OcrFollowupOutcome:
        if not isinstance(request, OcrFollowupRequest):
            raise TypeError("request must be an OcrFollowupRequest")
        if not isinstance(hooks, CorrectionTransformHooksPort):
            raise TypeError("hooks must implement CorrectionTransformHooksPort")
        with self._lock:
            existing = self._existing_job(request)
            if existing is not None:
                self._validate_existing(existing, request)
                try:
                    stored = self._repository.find_proposal(request)
                except EngineError as exc:
                    return self._fail_existing(
                        existing,
                        request.source,
                        self._failure_from_engine_error(exc),
                    )
                except Exception:
                    log.exception("unexpected OCR proposal replay failure")
                    return self._fail_existing(
                        existing,
                        request.source,
                        JobFailure(
                            "ocr_proposal_unavailable",
                            "OCR proposal storage is unavailable",
                            retryable=False,
                        ),
                    )
                if stored is not None:
                    self._validate_stored(stored, request)
                    pin_failure = self._stored_provider_pin_failure(
                        existing,
                        stored,
                    )
                    if pin_failure is not None:
                        return self._fail_existing(
                            existing,
                            request.source,
                            pin_failure,
                        )
                    proposals = [
                        output
                        for output in existing.outputs
                        if output.kind == "ocr-proposal"
                        and not output.partial
                    ]
                    if (
                        existing.state is JobState.DONE
                        and len(proposals) == 1
                        and proposals[0].ref == stored.proposal_ref
                    ):
                        return OcrFollowupOutcome(
                            OcrFollowupState.SUCCEEDED,
                            source=stored.source,
                            proposal_ref=stored.proposal_ref,
                        )
                    if existing.job_id not in self._jobs.records:
                        return OcrFollowupOutcome(
                            OcrFollowupState.SUCCEEDED,
                            source=stored.source,
                            proposal_ref=stored.proposal_ref,
                        )
                    return self._succeed(
                        stored,
                        self._record_for(existing.job_id),
                    )
                if existing.state is JobState.DONE:
                    return self._fail_existing(
                        existing,
                        request.source,
                        JobFailure(
                            "ocr_proposal_missing",
                            "the OCR child job has no durable proposal",
                            retryable=False,
                        ),
                    )
                if existing.state is JobState.INTERRUPTED:
                    retried = self._jobs.retry_interrupted(existing.job_id)
                    if retried is None:
                        return self._outcome_from_job(
                            existing,
                            request.source,
                        )
                    record = self._record_for(existing.job_id)
                    return self._execute(
                        request,
                        record,
                        _ChildHooks(self._jobs, record, hooks),
                    )
                return self._outcome_from_job(existing, request.source)

            record: MutableMapping[str, Any] = {
                "id": self.job_id_for(request.operation_id),
                "kind": CORRECTION_OCR_JOB_KIND,
                "status": "queued",
                "operation_id": self.child_operation_id(request.operation_id),
                "command_sha256": self._command_sha256(request),
                "subject": {
                    "item_id": request.item_id,
                    "source_id": request.source.artifact_id,
                },
                "total": 4,
                "progress": JobProgress(0, 4, "phase", "queued").as_dict(),
                "input_revisions": {
                    "parent_operation_id": request.operation_id,
                    "artifact_id": request.source.artifact_id,
                    "artifact_revision": request.source.artifact_revision,
                    "source_sha256": request.source.content_sha256,
                    "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
                    "command_sha256": self._command_sha256(request),
                },
            }
            self._jobs.track(record, CORRECTION_OCR_JOB_KIND)
            child_hooks = _ChildHooks(self._jobs, record, hooks)
            return self._execute(request, record, child_hooks)

    def _existing_job(self, request: OcrFollowupRequest) -> JobView | None:
        existing = self._jobs.view(self.job_id_for(request.operation_id))
        if existing is not None:
            return existing
        receipt = self._jobs.command_receipt(
            self.child_operation_id(request.operation_id),
            self._command_sha256(request),
            kind=CORRECTION_OCR_JOB_KIND,
        )
        return receipt.job if receipt is not None else None

    def _record_for(self, job_id: str) -> MutableMapping[str, Any]:
        record = self._jobs.records.get(job_id)
        if record is None:
            raise RuntimeError("OCR child job record is unavailable")
        return record

    def _execute(
        self,
        request: OcrFollowupRequest,
        record: MutableMapping[str, Any],
        hooks: _ChildHooks,
    ) -> OcrFollowupOutcome:
        if hooks.is_cancelled():
            return self._cancel(request.source, record)
        self._jobs.transition(record, "running")
        provider_work = False
        try:
            hooks.report_progress(JobProgress(1, 4, "phase", "reading-rendition"))
            stored = self._repository.find_proposal(request)
            if stored is not None:
                self._validate_stored(stored, request)
                return self._succeed(stored, record)

            content = self._repository.read_source(request)
            if not isinstance(content, bytes):
                raise EngineError(
                    "the corrected OCR rendition is invalid",
                    code="invalid_correction_ocr_source",
                )
            if len(content) > CORRECTION_OCR_MAX_SOURCE_BYTES:
                raise EngineError(
                    "the corrected OCR rendition exceeds its size budget",
                    code="correction_ocr_source_too_large",
                )
            if hashlib.sha256(content).hexdigest() != request.source.content_sha256:
                raise EngineError(
                    "the corrected OCR rendition checksum changed",
                    code="correction_ocr_source_checksum_mismatch",
                )
            if hooks.is_cancelled():
                return self._cancel(request.source, record)

            provider_work = True
            selection = self._provider_selection_for_record(record)
            if selection is None:
                selection = self._provider.select_provider()
                if not isinstance(selection, CorrectionOcrProviderSelection):
                    raise EngineError(
                        "OCR provider selection is invalid",
                        code="invalid_ocr_provider_selection",
                    )
                inputs = self._install_provider_pin(record, selection)
                self._jobs.transition(
                    record,
                    "running",
                    input_revisions=inputs,
                )

            hooks.report_progress(JobProgress(2, 4, "phase", "recognizing"))
            recognition = self._provider.recognize(selection, content, hooks)
            if not isinstance(recognition, CorrectionOcrRecognition):
                raise TypeError("OCR provider returned an invalid recognition")
            if (
                recognition.provider_id != selection.provider_id
                or recognition.model != selection.model
                or recognition.options != selection.options
            ):
                raise ValueError("OCR recognition does not match its pinned provider")
            if hooks.is_cancelled():
                return self._cancel(request.source, record)

            hooks.report_progress(JobProgress(3, 4, "phase", "publishing-proposal"))
            stored = self._repository.commit_proposal(request, recognition)
            self._validate_stored(stored, request)
            if stored.provider != selection:
                raise ValueError("OCR proposal does not match its pinned provider")
            return self._succeed(stored, record)
        except CorrectionTransformCancelled:
            return self._cancel(request.source, record)
        except EngineError as exc:
            failure = self._failure_from_engine_error(exc)
        except Exception as exc:
            log.exception("correction OCR follow-up failed")
            failure = JobFailure(
                "ocr_followup_failed",
                (
                    "OCR provider failed"
                    if provider_work
                    else "OCR follow-up failed"
                ),
                retryable=False,
                details={"exception": type(exc).__name__},
            )
        self._jobs.transition(
            record,
            "failed",
            errors=1,
            error=failure.message,
            failure=failure.as_dict(),
            note="OCR proposal failed",
            outputs=[],
        )
        return OcrFollowupOutcome(
            OcrFollowupState.FAILED,
            source=request.source,
            failure=failure,
        )

    def _succeed(
        self,
        stored: StoredCorrectionOcrProposal,
        record: MutableMapping[str, Any],
    ) -> OcrFollowupOutcome:
        pinned = self._provider_selection_for_record(record)
        if pinned is not None and pinned != stored.provider:
            raise EngineError(
                "OCR proposal does not match the child provider pin",
                code="correction_ocr_provider_pin_mismatch",
            )
        inputs = self._install_provider_pin(record, stored.provider)
        output = JobOutput("ocr-proposal", stored.proposal_ref).as_dict()
        self._jobs.transition(
            record,
            "done",
            done=4,
            total=4,
            progress=JobProgress(4, 4, "phase", "complete").as_dict(),
            input_revisions=inputs,
            outputs=[output],
            note="OCR proposal ready",
        )
        return OcrFollowupOutcome(
            OcrFollowupState.SUCCEEDED,
            source=stored.source,
            proposal_ref=stored.proposal_ref,
        )

    def _cancel(
        self,
        source: CommittedCorrectionOutput,
        record: MutableMapping[str, Any],
    ) -> OcrFollowupOutcome:
        self._jobs.transition(
            record,
            "cancelled",
            note="OCR proposal cancelled",
            outputs=[],
        )
        return OcrFollowupOutcome(OcrFollowupState.CANCELLED, source=source)

    def _fail_existing(
        self,
        job: JobView,
        source: CommittedCorrectionOutput,
        failure: JobFailure,
    ) -> OcrFollowupOutcome:
        record = self._jobs.records.get(job.job_id)
        if record is not None:
            self._jobs.transition(
                record,
                "failed",
                errors=1,
                error=failure.message,
                failure=failure.as_dict(),
                note="OCR proposal is unavailable",
                outputs=[],
            )
        return OcrFollowupOutcome(
            OcrFollowupState.FAILED,
            source=source,
            failure=failure,
        )

    @staticmethod
    def _failure_from_engine_error(exc: EngineError) -> JobFailure:
        return JobFailure(
            exc.code,
            exc.message,
            retryable=exc.retryable,
            details=exc.details,
        )

    def _provider_selection_for_record(
        self,
        record: Mapping[str, Any],
    ) -> CorrectionOcrProviderSelection | None:
        private = self._jobs.private_input_revisions(record)
        raw = private.get("provider")
        if raw is None:
            return None
        return self._selection_from_pin(raw)

    def _install_provider_pin(
        self,
        record: MutableMapping[str, Any],
        selection: CorrectionOcrProviderSelection,
    ) -> dict[str, Any]:
        if not isinstance(selection, CorrectionOcrProviderSelection):
            raise TypeError("selection must be a CorrectionOcrProviderSelection")
        private = self._jobs.private_input_revisions(record)
        existing = private.get("provider")
        if existing is not None and self._selection_from_pin(existing) != selection:
            raise EngineError(
                "OCR proposal does not match the child provider pin",
                code="correction_ocr_provider_pin_mismatch",
            )
        private["provider"] = selection.as_dict()
        self._jobs.set_private_input_revisions(record, private)
        inputs = dict(record.get("input_revisions") or {})
        inputs["provider"] = {
            "provider_id": selection.provider_id,
            "model": selection.model,
        }
        return inputs

    def _stored_provider_pin_failure(
        self,
        job: JobView,
        stored: StoredCorrectionOcrProposal,
    ) -> JobFailure | None:
        record = self._jobs.records.get(job.job_id)
        if record is not None:
            private = self._jobs.private_input_revisions(record)
            raw_pin = private.get("provider")
            if raw_pin is None:
                return JobFailure(
                    "invalid_ocr_provider_pin",
                    "OCR child provider pin is invalid",
                    retryable=False,
                )
            try:
                pinned = self._selection_from_pin(raw_pin)
            except EngineError as exc:
                return self._failure_from_engine_error(exc)
            if pinned != stored.provider:
                return JobFailure(
                    "correction_ocr_provider_pin_mismatch",
                    "OCR proposal does not match the child provider pin",
                    retryable=False,
                )
            return None

        # A pruned terminal receipt intentionally retains only the public
        # provider identity. The integrity-checked proposal remains the
        # authority for the exact private execution options; compare the
        # observable identity without requiring those options to cross the
        # job API boundary.
        raw_public = job.input_revisions.get("provider")
        if raw_public is None:
            return None
        if not isinstance(raw_public, Mapping) or set(raw_public) != {
            "provider_id",
            "model",
        }:
            return JobFailure(
                "invalid_ocr_provider_pin",
                "OCR child provider pin is invalid",
                retryable=False,
            )
        try:
            identity = CorrectionOcrProviderSelection(
                raw_public.get("provider_id"),
                raw_public.get("model"),
            )
        except (TypeError, ValueError):
            return JobFailure(
                "invalid_ocr_provider_pin",
                "OCR child provider pin is invalid",
                retryable=False,
            )
        if (
            identity.provider_id != stored.provider.provider_id
            or identity.model != stored.provider.model
        ):
            return JobFailure(
                "correction_ocr_provider_pin_mismatch",
                "OCR proposal does not match the child provider pin",
                retryable=False,
            )
        return None

    @staticmethod
    def _selection_from_pin(
        raw: object,
    ) -> CorrectionOcrProviderSelection:
        if not isinstance(raw, Mapping) or set(raw) != {
            "provider_id",
            "model",
            "options",
        }:
            raise EngineError(
                "OCR child provider pin is invalid",
                code="invalid_ocr_provider_pin",
            )
        try:
            return CorrectionOcrProviderSelection(
                raw["provider_id"],
                raw["model"],
                raw["options"],
            )
        except (TypeError, ValueError) as exc:
            raise EngineError(
                "OCR child provider pin is invalid",
                code="invalid_ocr_provider_pin",
                details={"cause_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _validate_stored(
        stored: StoredCorrectionOcrProposal,
        request: OcrFollowupRequest,
    ) -> None:
        if not isinstance(stored, StoredCorrectionOcrProposal):
            raise TypeError("OCR proposal repository returned an invalid receipt")
        if stored.source != request.source:
            raise ValueError("OCR proposal is not pinned to the requested rendition")

    @staticmethod
    def _validate_existing(
        job: JobView,
        request: OcrFollowupRequest,
    ) -> None:
        pins = job.input_revisions
        if (
            job.kind != CORRECTION_OCR_JOB_KIND
            or job.subject.item_id != request.item_id
            or job.subject.source_id != request.source.artifact_id
            or pins.get("parent_operation_id") != request.operation_id
            or pins.get("artifact_id") != request.source.artifact_id
            or pins.get("artifact_revision")
            != request.source.artifact_revision
            or pins.get("source_sha256") != request.source.content_sha256
            or pins.get("command_sha256")
            != CorrectionOcrFollowupService._command_sha256(request)
            or pins.get("publication_policy")
            != CORRECTION_OCR_PROPOSAL_POLICY
        ):
            raise ValueError("existing OCR child job does not match its parent request")

    @staticmethod
    def _outcome_from_job(
        job: JobView,
        source: CommittedCorrectionOutput,
    ) -> OcrFollowupOutcome:
        if job.state is JobState.DONE:
            proposals = [
                output
                for output in job.outputs
                if output.kind == "ocr-proposal" and not output.partial
            ]
            if len(proposals) == 1:
                return OcrFollowupOutcome(
                    OcrFollowupState.SUCCEEDED,
                    source=source,
                    proposal_ref=proposals[0].ref,
                )
        if job.state is JobState.CANCELLED:
            return OcrFollowupOutcome(OcrFollowupState.CANCELLED, source=source)
        failure = job.error or JobFailure(
            "ocr_followup_incomplete",
            f"OCR follow-up is {job.state.value}",
            retryable=False,
        )
        return OcrFollowupOutcome(
            OcrFollowupState.FAILED,
            source=source,
            failure=failure,
        )


__all__ = [
    "CORRECTION_OCR_JOB_KIND",
    "CORRECTION_OCR_MAX_SOURCE_BYTES",
    "CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT",
    "CORRECTION_OCR_PROPOSAL_CATALOG_PAGE_LIMIT",
    "CORRECTION_OCR_PROPOSAL_POLICY",
    "CorrectionOcrFollowupService",
    "CorrectionOcrProposalAvailability",
    "CorrectionOcrProposalCatalogRepositoryPort",
    "CorrectionOcrProposalCatalogService",
    "CorrectionOcrProposalCatalogSnapshot",
    "CorrectionOcrProposalPageView",
    "CorrectionOcrProposalQueryRepositoryPort",
    "CorrectionOcrProposalQueryService",
    "CorrectionOcrProposalProviderView",
    "CorrectionOcrProposalSummaryView",
    "CorrectionOcrProposalView",
    "CorrectionOcrProposalRepositoryPort",
    "CorrectionOcrProviderPort",
    "CorrectionOcrProviderSelection",
    "CorrectionOcrRecognition",
    "StoredCorrectionOcrProposal",
]
