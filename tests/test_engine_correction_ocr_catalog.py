from __future__ import annotations

import hashlib
import json

import pytest

import librarytool.engine.correction_ocr as correction_ocr
from librarytool.engine import (
    CorrectionOcrProposalAvailability,
    CorrectionOcrProposalCatalogRepositoryPort,
    CorrectionOcrProposalCatalogService,
    CorrectionOcrProposalCatalogSnapshot,
    CorrectionOcrProposalPageView,
    CorrectionOcrProposalProviderView,
    CorrectionOcrProposalSummaryView,
)
from librarytool.engine.correction_transforms import CommittedCorrectionOutput
from librarytool.engine.errors import ConflictError, ValidationError


def _summary(
    suffix: str,
    *,
    operation_id: str | None = None,
) -> CorrectionOcrProposalSummaryView:
    content = f"proposal-{suffix}".encode()
    return CorrectionOcrProposalSummaryView(
        proposal_ref=f"cop-{suffix * 40}",
        operation_id=operation_id or f"operation-{suffix}",
        source=CommittedCorrectionOutput(
            "ocr-ready",
            f"ocr-ready-{suffix}",
            f"revision-{suffix}",
            hashlib.sha256(f"source-{suffix}".encode()).hexdigest(),
        ),
        provider=CorrectionOcrProposalProviderView("mistral", "ocr-4"),
        publication_policy="machine-proposal-only",
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


class _MemoryCatalog:
    def __init__(
        self,
        item_id: str,
        proposals: tuple[CorrectionOcrProposalSummaryView, ...],
    ) -> None:
        self.item_id = item_id
        self.proposals = proposals
        self.calls: list[str] = []

    def list_proposals(
        self,
        item_id: str,
    ) -> CorrectionOcrProposalCatalogSnapshot:
        self.calls.append(item_id)
        return CorrectionOcrProposalCatalogSnapshot(
            item_id=item_id,
            proposals=self.proposals if item_id == self.item_id else (),
        )


def test_summary_is_explicitly_available_and_omits_private_proposal_data() -> None:
    summary = _summary("a")

    assert summary.availability is CorrectionOcrProposalAvailability.AVAILABLE
    assert summary.provider.as_dict() == {
        "provider_id": "mistral",
        "model": "ocr-4",
    }
    public = summary.as_dict()
    encoded = json.dumps(public, sort_keys=True)
    assert public["availability"] == "available"
    assert "recognition" not in public
    assert "options" not in public["provider"]
    assert "path" not in encoded.casefold()
    assert "credential" not in encoded.casefold()

    with pytest.raises(TypeError):
        CorrectionOcrProposalSummaryView(
            proposal_ref=summary.proposal_ref,
            operation_id=summary.operation_id,
            source=summary.source,
            provider=summary.provider,
            publication_policy=summary.publication_policy,
            content_sha256=summary.content_sha256,
            availability="available",  # type: ignore[arg-type]
        )


def test_catalog_pages_are_deterministic_and_snapshot_pinned() -> None:
    proposals = (_summary("a"), _summary("b"), _summary("c"))
    repository = _MemoryCatalog("book-1", proposals)
    service = CorrectionOcrProposalCatalogService(repository)

    first = service.list_proposals("book-1", limit=1)
    repeated = service.list_proposals("book-1", limit=1)
    second = service.list_proposals(
        "book-1",
        cursor=first.next_cursor,
        limit=2,
        snapshot_revision=first.snapshot_revision,
    )

    assert isinstance(repository, CorrectionOcrProposalCatalogRepositoryPort)
    assert isinstance(first, CorrectionOcrProposalPageView)
    assert first.proposals == proposals[:1]
    assert first.total == 3
    assert first.next_cursor is not None
    assert repeated == first
    assert second.proposals == proposals[1:]
    assert second.snapshot_revision == first.snapshot_revision
    assert second.next_cursor is None
    assert repository.calls == ["book-1", "book-1", "book-1"]


def test_catalog_cursor_rejects_cross_item_and_changed_snapshots() -> None:
    repository = _MemoryCatalog("book-1", (_summary("a"), _summary("b")))
    service = CorrectionOcrProposalCatalogService(repository)
    first = service.list_proposals("book-1", limit=1)
    assert first.next_cursor is not None

    with pytest.raises(ValidationError) as cross_item:
        service.list_proposals("book-2", cursor=first.next_cursor, limit=1)
    assert (
        cross_item.value.code
        == "invalid_correction_ocr_proposal_catalog_query"
    )
    assert repository.calls == ["book-1"]

    repository.proposals = (
        _summary("a"),
        _summary("b"),
        _summary("c"),
    )
    with pytest.raises(ConflictError) as changed:
        service.list_proposals(
            "book-1",
            cursor=first.next_cursor,
            limit=1,
        )
    assert changed.value.code == "correction_ocr_proposal_catalog_changed"
    assert (
        changed.value.details["expected_revision"]
        == first.snapshot_revision
    )
    assert changed.value.details["actual_revision"] != first.snapshot_revision


@pytest.mark.parametrize(
    ("cursor", "limit", "snapshot_revision"),
    (
        ("", 1, None),
        ("not-a-cursor", 1, None),
        (None, 0, None),
        (None, True, None),
        (None, correction_ocr.CORRECTION_OCR_PROPOSAL_CATALOG_PAGE_LIMIT + 1, None),
        (None, 1, "not-a-snapshot"),
    ),
)
def test_catalog_query_arguments_are_strict(
    cursor,
    limit,
    snapshot_revision,
) -> None:
    service = CorrectionOcrProposalCatalogService(_MemoryCatalog("book-1", ()))

    with pytest.raises(ValidationError) as raised:
        service.list_proposals(
            "book-1",
            cursor=cursor,
            limit=limit,
            snapshot_revision=snapshot_revision,
        )

    assert raised.value.code == "invalid_correction_ocr_proposal_catalog_query"


def test_catalog_snapshot_enforces_count_and_stable_order(monkeypatch) -> None:
    with pytest.raises(ValueError):
        CorrectionOcrProposalCatalogSnapshot(
            "book-1",
            (_summary("b"), _summary("a")),
        )

    monkeypatch.setattr(
        correction_ocr,
        "CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT",
        1,
    )
    with pytest.raises(ValueError):
        CorrectionOcrProposalCatalogSnapshot(
            "book-1",
            (_summary("a"), _summary("b")),
        )


def test_empty_catalog_is_a_revisioned_snapshot_without_a_page_state() -> None:
    page = CorrectionOcrProposalCatalogService(
        _MemoryCatalog("book-1", ())
    ).list_proposals("book-1")

    assert page.proposals == ()
    assert page.total == 0
    assert page.next_cursor is None
    assert page.snapshot_revision.startswith("cops-")
    assert set(page.as_dict()) == {
        "item_id",
        "snapshot_revision",
        "proposals",
        "next_cursor",
        "total",
    }
