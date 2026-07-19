"""Replica application service: commands and queries over one workspace."""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import (
    FailureDetail,
    LayoutFamilyQuery,
    LayoutFamilyResult,
    PageKey,
    ProposalAction,
    ProposalReviewResult,
    RecompileRegionPagesCommand,
    RecompileRegionPagesResult,
    RegionPageView,
    ReplaceRegionPageCommand,
    ReviewRegionProposalCommand,
)
from .errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    ValidationError,
)
from .ports import ItemRepositoryPort, ReplicaPolicyPort, ReplicaRepositoryPort
from .text_layers import TextLayerService


_FAMILY_OPTIONS = {
    "similarity_threshold",
    "min_family_size",
    "low_confidence_threshold",
    "max_families",
    "max_regions_per_page",
}


class ReplicaApplicationService:
    """Framework-neutral Replica use cases.

    The service owns optimistic concurrency and mutation ordering.  The
    repository owns serialization and locking; injected policies retain the
    existing format/domain behavior while the package boundary is introduced.
    """

    def __init__(
        self,
        items: ItemRepositoryPort,
        repository: ReplicaRepositoryPort,
        policies: ReplicaPolicyPort,
        text_layers: TextLayerService,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._items = items
        self._repository = repository
        self._policies = policies
        self._text_layers = text_layers
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def get_region_page(self, key: PageKey) -> RegionPageView:
        key = self._require_page_key(key)
        workspace = self._repository.snapshot(key.item_id)
        return self._page_view(workspace, key)

    def replace_region_page(
        self, command: ReplaceRegionPageCommand
    ) -> RegionPageView:
        key = self._require_page_key(command.key)
        expected = str(command.expected_revision or "").strip()
        if not expected:
            raise PreconditionRequiredError(
                "a region revision is required",
                code="region_revision_required",
                details=self._key_details(key),
            )
        if not isinstance(command.items, (list, tuple)):
            raise ValidationError(
                "items must be a sequence",
                code="invalid_region_items",
                details=self._key_details(key),
            )
        raw_items = list(command.items)
        duplicates = self._policies.duplicate_rids(raw_items)
        if duplicates:
            raise ValidationError(
                "duplicate region identity in the page",
                code="duplicate_region_identity",
                details={
                    **self._key_details(key),
                    "duplicate_rids": sorted(duplicates),
                },
            )
        try:
            items = self._policies.sanitize_region_items(
                raw_items, source_type="human"
            )
            dims = self._policies.sanitize_dims(command.dims)
            doc = self._policies.sanitize_document_name(command.doc)
        except EngineError:
            raise
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                str(exc) or "the region page is invalid",
                code="invalid_region_page",
                details=self._key_details(key),
            ) from exc

        with self._repository.unit_of_work(key.item_id) as uow:
            workspace = uow.workspace
            current = self._region(workspace, key)
            current_revision = self._policies.content_revision(current, "rr")
            if expected != current_revision:
                raise ConflictError(
                    "the region page changed; reload it before saving",
                    code="stale_region_revision",
                    details={
                        **self._key_details(key),
                        "expected_revision": expected,
                        "current_revision": current_revision,
                    },
                    retryable=True,
                )

            incoming_rids = {
                rid
                for item in items
                if (rid := self._policies.clean_rid(item.get("rid")))
            }
            collisions = sorted(
                incoming_rids & self._other_region_rids(workspace, key)
            )
            if collisions:
                raise ConflictError(
                    "a region identity is already used on another page",
                    code="cross_page_region_identity",
                    details={
                        **self._key_details(key),
                        "duplicate_rids": collisions,
                    },
                )

            if items:
                record: dict[str, Any] = {
                    "doc": doc,
                    "dims": dims,
                    "items": items,
                    "origin": "human",
                }
                if command.preserve_ext:
                    page_ext = copy.deepcopy((current or {}).get("ext") or {})
                else:
                    page_ext = self._policies.sanitize_ext(command.ext)
                if page_ext:
                    record["ext"] = page_ext
                if command.state == "verified":
                    record["state"] = "verified"
                self._set_region(workspace, key, record)
            else:
                self._set_region(workspace, key, None)
            uow.commit()
            return self._page_view(workspace, key)

    def review_region_proposal(
        self, command: ReviewRegionProposalCommand
    ) -> ProposalReviewResult:
        key = self._require_page_key(command.key)
        raw_action = (
            command.action.value
            if isinstance(command.action, ProposalAction)
            else str(command.action)
        )
        try:
            action = ProposalAction(raw_action.lower())
        except ValueError as exc:
            raise ValidationError(
                "proposal action must be apply or dismiss",
                code="invalid_proposal_action",
                details={
                    **self._key_details(key),
                    "action": str(command.action),
                },
            ) from exc
        expected_region = str(command.expected_region_revision or "").strip()
        expected_proposal = str(
            command.expected_proposal_revision or ""
        ).strip()
        if not expected_region or not expected_proposal:
            raise PreconditionRequiredError(
                "region and proposal revisions are required",
                code="proposal_revision_required",
                details=self._key_details(key),
            )

        with self._repository.unit_of_work(key.item_id) as uow:
            workspace = uow.workspace
            current = self._region(workspace, key)
            proposal = self._proposal(workspace, key)
            if proposal is None:
                raise NotFoundError(
                    "this page has no current proposal",
                    code="proposal_not_found",
                    details=self._key_details(key),
                )
            current_revision = self._policies.content_revision(current, "rr")
            proposal_revision = self._policies.proposal_revision(proposal)
            if (
                expected_region != current_revision
                or expected_proposal != proposal_revision
            ):
                raise ConflictError(
                    "the page or proposal changed; reload before continuing",
                    code="stale_proposal_revision",
                    details={
                        **self._key_details(key),
                        "expected_region_revision": expected_region,
                        "current_region_revision": current_revision,
                        "expected_proposal_revision": expected_proposal,
                        "current_proposal_revision": proposal_revision,
                    },
                    retryable=True,
                )
            if (
                action is ProposalAction.APPLY
                and str(proposal.get("base_revision") or "")
                != current_revision
            ):
                raise ConflictError(
                    "the canonical page changed after this proposal",
                    code="proposal_base_changed",
                    details={
                        **self._key_details(key),
                        "base_revision": str(proposal.get("base_revision") or ""),
                        "current_region_revision": current_revision,
                        "proposal_revision": proposal_revision,
                    },
                )

            if action is ProposalAction.DISMISS:
                record = self._policies.dismiss_region_proposal(current)
                self._set_region(workspace, key, record)
                self._set_proposal(workspace, key, None)
                uow.commit()
                return ProposalReviewResult(
                    action=action,
                    page=self._page_view(workspace, key),
                    compiled=True,
                )

            record = self._policies.accept_region_proposal(current, proposal)
            self._set_region(workspace, key, record)
            self._set_proposal(workspace, key, None)
            doc = self._policies.sanitize_document_name(
                str(proposal.get("doc") or "compiled.txt")
            )
            compiled_text = (
                str(proposal.get("text") or "")
                if "text" in proposal
                else self._text_layers.compose(
                    (record or {}).get("items") or (), layer="text"
                )
            )
            pending = {
                "doc": doc,
                "proposal_revision": proposal_revision,
                "provider": str(proposal.get("provider") or "unknown"),
                "text": compiled_text,
                "at": self._timestamp(),
            }
            self._set_pending(workspace, key, pending)

            # This is intentionally a durable mid-command commit.  Canonical
            # acceptance and its recovery journal survive a failed derived
            # text write or process interruption.
            uow.commit()
            try:
                self._text_layers.merge_explicit(key, doc, compiled_text)
            except Exception as exc:
                failure = FailureDetail.from_exception(
                    exc,
                    code="compiled_text_pending",
                    details={
                        **self._key_details(key),
                        "document": doc,
                        "proposal_revision": proposal_revision,
                    },
                )
                return ProposalReviewResult(
                    action=action,
                    page=self._page_view(workspace, key),
                    compiled=False,
                    derived_failure=failure,
                )

            self._set_pending(workspace, key, None)
            uow.commit()
            return ProposalReviewResult(
                action=action,
                page=self._page_view(workspace, key),
                compiled=True,
            )

    def propose_layout_families(
        self, query: LayoutFamilyQuery
    ) -> LayoutFamilyResult:
        source = self._require_source(query.item_id, query.source_id)
        unknown = sorted(set(query.options) - _FAMILY_OPTIONS)
        if unknown:
            raise ValidationError(
                "one or more layout-family options are unknown",
                code="invalid_layout_family_options",
                details={"unknown_options": unknown},
            )
        workspace = self._repository.snapshot(query.item_id)
        pages = copy.deepcopy(
            ((workspace.get("regions") or {}).get(source) or {})
        )
        try:
            proposal = self._policies.propose_layout_families(
                pages, **dict(query.options)
            )
        except EngineError:
            raise
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                str(exc) or "layout-family options are invalid",
                code="invalid_layout_family_options",
                details={"options": dict(query.options)},
            ) from exc
        return LayoutFamilyResult(
            capability="replica.layout-families.propose@1",
            proposal=copy.deepcopy(dict(proposal)),
        )

    def recompile_region_pages(
        self, command: RecompileRegionPagesCommand
    ) -> RecompileRegionPagesResult:
        """Rebuild canonical derived text through the text-layer port.

        Pending proposal output is attempted first and its recovery marker is
        cleared only after the derived write succeeds. Canonical page records
        then compose through the same service, so HTTP, CLI, and future GUI
        clients cannot acquire different text-layer semantics.
        """
        source = self._require_source(command.item_id, command.source_id)
        layer = str(command.layer or "text").strip().lower()
        if layer == "normalized":
            layer = "norm"
        if layer not in ("text", "norm"):
            raise ValidationError(
                "the text layer must be text or norm",
                code="invalid_text_layer",
                details={"layer": layer},
            )
        only = command.page
        if only is not None and (
            not isinstance(only, int) or isinstance(only, bool) or only < 1
        ):
            raise ValidationError(
                "page must be a positive integer",
                code="invalid_page",
                details={"page": only},
            )

        completed = 0
        documents: set[str] = set()
        with self._repository.unit_of_work(command.item_id) as uow:
            workspace = uow.workspace
            processed: set[int] = set()
            if layer == "text":
                pending_pages = (
                    (workspace.get("region_compile_pending") or {})
                    .get(source) or {}
                )
                for page_key in sorted(
                    (key for key in list(pending_pages) if str(key).isdigit()),
                    key=int,
                ):
                    page = int(page_key)
                    pending = pending_pages.get(page_key)
                    if not isinstance(pending, Mapping) or (
                        only is not None and page != only
                    ):
                        continue
                    key = PageKey(command.item_id, source, page)
                    document = self._text_layers.merge_explicit(
                        key,
                        str(pending.get("doc") or "compiled.txt"),
                        str(pending.get("text") or ""),
                    )
                    self._set_pending(workspace, key, None)
                    processed.add(page)
                    documents.add(document)
                    completed += 1

            region_pages = ((workspace.get("regions") or {}).get(source) or {})
            for page_key in sorted(
                (key for key in region_pages if str(key).isdigit()), key=int
            ):
                page = int(page_key)
                record = region_pages.get(page_key)
                if (
                    page in processed
                    or not isinstance(record, Mapping)
                    or (only is not None and page != only)
                ):
                    continue
                key = PageKey(command.item_id, source, page)
                document = self._text_layers.compile_region_page(
                    key,
                    record,
                    layer=layer,
                    target=str(command.target or ""),
                )
                documents.add(document)
                completed += 1

            if processed:
                uow.commit()
        return RecompileRegionPagesResult(
            pages=completed,
            documents=tuple(sorted(documents)),
        )

    def _require_page_key(self, key: PageKey) -> PageKey:
        source = self._require_source(key.item_id, key.source_id)
        if not isinstance(key.page, int) or isinstance(key.page, bool) or key.page < 1:
            raise ValidationError(
                "page must be a positive integer",
                code="invalid_page",
                details={"page": key.page},
            )
        return PageKey(item_id=key.item_id, source_id=source, page=key.page)

    def _require_source(self, item_id: str, source_id: str) -> str:
        value = str(item_id or "").strip()
        item = self._items.get(value) if value else None
        if item is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": value},
            )
        source = str(source_id or "primary").strip() or "primary"
        available = {"primary", *item.sources}
        if source not in available:
            raise ValidationError(
                "the source does not belong to this item",
                code="unknown_source",
                details={
                    "item_id": value,
                    "source_id": source,
                    "available_sources": sorted(available),
                },
            )
        return source

    def _page_view(
        self, workspace: Mapping[str, Any], key: PageKey
    ) -> RegionPageView:
        record = self._region(workspace, key)
        proposal = self._proposal(workspace, key)
        pending = self._pending(workspace, key)
        return RegionPageView(
            key=key,
            found=record is not None,
            revision=self._policies.content_revision(record, "rr"),
            doc=str((record or {}).get("doc") or ""),
            dims=copy.deepcopy((record or {}).get("dims") or {}),
            state=str((record or {}).get("state") or ""),
            stale=copy.deepcopy((record or {}).get("stale") or {}),
            ext=copy.deepcopy((record or {}).get("ext") or {}),
            items=tuple(copy.deepcopy((record or {}).get("items") or ())),
            proposal=copy.deepcopy(proposal),
            compile_pending=copy.deepcopy(pending),
        )

    @staticmethod
    def _record_at(
        workspace: Mapping[str, Any], section: str, key: PageKey
    ) -> dict[str, Any] | None:
        sources = workspace.get(section)
        pages = sources.get(key.source_id) if isinstance(sources, Mapping) else None
        record = pages.get(str(key.page)) if isinstance(pages, Mapping) else None
        return record if isinstance(record, dict) else None

    def _region(
        self, workspace: Mapping[str, Any], key: PageKey
    ) -> dict[str, Any] | None:
        return self._record_at(workspace, "regions", key)

    def _proposal(
        self, workspace: Mapping[str, Any], key: PageKey
    ) -> dict[str, Any] | None:
        return self._record_at(workspace, "region_proposals", key)

    def _pending(
        self, workspace: Mapping[str, Any], key: PageKey
    ) -> dict[str, Any] | None:
        return self._record_at(workspace, "region_compile_pending", key)

    @staticmethod
    def _set_record(
        workspace: MutableMapping[str, Any],
        section: str,
        key: PageKey,
        record: Mapping[str, Any] | None,
    ) -> None:
        if record is not None:
            sources = workspace.setdefault(section, {})
            pages = sources.setdefault(key.source_id, {})
            pages[str(key.page)] = copy.deepcopy(dict(record))
            return
        sources = workspace.get(section)
        if not isinstance(sources, MutableMapping):
            return
        pages = sources.get(key.source_id)
        if not isinstance(pages, MutableMapping):
            return
        pages.pop(str(key.page), None)
        if not pages:
            sources.pop(key.source_id, None)
        if not sources:
            workspace.pop(section, None)

    def _set_region(
        self,
        workspace: MutableMapping[str, Any],
        key: PageKey,
        record: Mapping[str, Any] | None,
    ) -> None:
        self._set_record(workspace, "regions", key, record)

    def _set_proposal(
        self,
        workspace: MutableMapping[str, Any],
        key: PageKey,
        record: Mapping[str, Any] | None,
    ) -> None:
        self._set_record(workspace, "region_proposals", key, record)

    def _set_pending(
        self,
        workspace: MutableMapping[str, Any],
        key: PageKey,
        record: Mapping[str, Any] | None,
    ) -> None:
        self._set_record(workspace, "region_compile_pending", key, record)

    def _other_region_rids(
        self, workspace: Mapping[str, Any], key: PageKey
    ) -> set[str]:
        result: set[str] = set()
        sources = workspace.get("regions")
        if not isinstance(sources, Mapping):
            return result
        for source_id, pages in sources.items():
            if not isinstance(pages, Mapping):
                continue
            for page, record in pages.items():
                if source_id == key.source_id and str(page) == str(key.page):
                    continue
                if not isinstance(record, Mapping):
                    continue
                for item in record.get("items") or ():
                    if not isinstance(item, Mapping):
                        continue
                    rid = self._policies.clean_rid(item.get("rid"))
                    if rid:
                        result.add(rid)
        return result

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _key_details(key: PageKey) -> dict[str, Any]:
        return {
            "item_id": key.item_id,
            "source_id": key.source_id,
            "page": key.page,
        }
