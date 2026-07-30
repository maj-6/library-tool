from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext

import pytest

import librarytool.adapters.filesystem.correction_ocr_proposal_repository as catalog_repository
from librarytool.adapters.filesystem import (
    FilesystemCorrectionOcrProposalRepository,
    RecoverableWriteSet,
)
from librarytool.engine.correction_ocr import (
    CorrectionOcrProposalAvailability,
    CorrectionOcrProposalCatalogRepositoryPort,
    CorrectionOcrRecognition,
)
from librarytool.engine.correction_transforms import (
    CommittedCorrectionOutput,
    OcrFollowupRequest,
)
from librarytool.engine.errors import RepositoryError


def _source(suffix: str) -> CommittedCorrectionOutput:
    return CommittedCorrectionOutput(
        "ocr-ready",
        f"ocr-ready-{suffix}",
        f"revision-{suffix}",
        hashlib.sha256(f"source-{suffix}".encode()).hexdigest(),
    )


def _request(
    item_id: str,
    operation_id: str,
    suffix: str,
) -> OcrFollowupRequest:
    return OcrFollowupRequest(operation_id, item_id, _source(suffix))


def _recognition(text: str = "Machine proposal") -> CorrectionOcrRecognition:
    return CorrectionOcrRecognition(
        "tesseract",
        "local",
        {
            "text": text,
            "regions": [{"role": "illustration", "box": [0, 0, 1, 1]}],
        },
        {
            "tesseract": r"C:\private tools\tesseract.exe",
            "credential": "PRIVATE-PROVIDER-PIN",
        },
    )


def _repository(tmp_path) -> FilesystemCorrectionOcrProposalRepository:
    return FilesystemCorrectionOcrProposalRepository(
        RecoverableWriteSet(tmp_path),
        source_bytes_for=lambda _item, _operation, _source: None,
        lock_context_for=nullcontext,
        recover=False,
    )


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_filesystem_catalog_lists_only_fully_verified_item_summaries(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    first = repository.commit_proposal(
        _request("book-1", "operation-2", "b"),
        _recognition(),
    )
    repository.commit_proposal(
        _request("book-2", "operation-other", "c"),
        _recognition(),
    )
    second = repository.commit_proposal(
        _request("book-1", "operation-1", "a"),
        _recognition(),
    )

    snapshot = repository.list_proposals("book-1")
    reopened = _repository(tmp_path).list_proposals("book-1")

    assert isinstance(repository, CorrectionOcrProposalCatalogRepositoryPort)
    assert snapshot == reopened
    assert snapshot.item_id == "book-1"
    assert tuple(value.proposal_ref for value in snapshot.proposals) == tuple(
        sorted((first.proposal_ref, second.proposal_ref))
    )
    assert all(
        value.availability is CorrectionOcrProposalAvailability.AVAILABLE
        for value in snapshot.proposals
    )
    assert {value.operation_id for value in snapshot.proposals} == {
        "operation-1",
        "operation-2",
    }
    public = json.dumps(
        [value.as_dict() for value in snapshot.proposals],
        sort_keys=True,
    )
    assert "recognition" not in public
    assert "options" not in public
    assert "PRIVATE-PROVIDER-PIN" not in public
    assert r"C:\\private tools" not in public
    assert ".engine" not in public


def test_empty_catalog_read_is_revisioned_and_does_not_mint_storage(tmp_path) -> None:
    repository = _repository(tmp_path)
    proposal_root = (
        tmp_path
        / ".engine"
        / "correction-transforms"
        / "ocr-proposals"
    )
    receipt_root = (
        tmp_path
        / ".engine"
        / "receipts"
        / "correction-ocr-proposals"
    )

    snapshot = repository.list_proposals("book-1")

    assert snapshot.proposals == ()
    assert snapshot.snapshot_revision.startswith("cops-")
    assert not proposal_root.exists()
    assert not receipt_root.exists()


def test_catalog_is_item_kind_neutral_for_capture_only_corrections_items(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    stored = repository.commit_proposal(
        _request("capture-session-1", "capture-operation-1", "a"),
        _recognition(),
    )

    snapshot = repository.list_proposals("capture-session-1")

    assert tuple(value.proposal_ref for value in snapshot.proposals) == (
        stored.proposal_ref,
    )
    assert repository.list_proposals("book-1").proposals == ()


def test_catalog_read_does_not_rewrite_or_mint_proposal_authority_files(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    repository.commit_proposal(
        _request("book-1", "operation-1", "a"),
        _recognition(),
    )

    def authority_snapshot():
        return {
            path.relative_to(tmp_path).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in (tmp_path / ".engine").rglob("*.json")
        }

    before = authority_snapshot()
    repository.list_proposals("book-1")
    after = authority_snapshot()

    assert after == before


def test_catalog_fails_closed_for_a_tampered_candidate_from_another_item(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    repository.commit_proposal(
        _request("book-2", "operation-other", "a"),
        _recognition(),
    )
    operation_path = next(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposal-operations"
        ).glob("*.json")
    )
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation["item_id"] = "book-private"
    operation_path.write_bytes(_canonical(operation))

    with pytest.raises(RepositoryError) as raised:
        repository.list_proposals("book-1")

    assert raised.value.code == "invalid_correction_ocr_proposal"
    assert "book-private" not in json.dumps(dict(raised.value.details))


def test_catalog_does_not_silently_prefilter_a_corrupt_cross_item_receipt(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    repository.commit_proposal(
        _request("book-2", "operation-other", "a"),
        _recognition(),
    )
    receipt_path = next(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposals"
        ).glob("*.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_sha256"] = "not-a-valid-digest"
    receipt_path.write_bytes(_canonical(receipt))

    with pytest.raises(RepositoryError) as raised:
        repository.list_proposals("book-1")

    assert raised.value.code == "invalid_correction_ocr_proposal"


def test_catalog_fails_closed_for_incomplete_or_untrusted_entries(tmp_path) -> None:
    repository = _repository(tmp_path)
    repository.commit_proposal(
        _request("book-1", "operation-1", "a"),
        _recognition(),
    )
    receipt = next(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposals"
        ).glob("*.json")
    )
    receipt.unlink()

    with pytest.raises(RepositoryError) as incomplete:
        repository.list_proposals("book-1")
    assert incomplete.value.code == "invalid_correction_ocr_proposal_catalog"

    private_candidate = receipt.parent / "private-path.json"
    private_candidate.parent.mkdir(parents=True, exist_ok=True)
    private_candidate.write_bytes(b"{}")
    with pytest.raises(RepositoryError) as untrusted:
        repository.list_proposals("book-1")
    assert untrusted.value.code == "invalid_correction_ocr_proposal_catalog"
    assert str(private_candidate) not in str(untrusted.value)
    assert str(private_candidate) not in json.dumps(dict(untrusted.value.details))


def test_unrelated_volume_does_not_consume_requested_item_budgets(
    tmp_path,
    monkeypatch,
) -> None:
    repository = _repository(tmp_path)
    requested = repository.commit_proposal(
        _request("book-1", "operation-requested", "a"),
        _recognition(),
    )
    for index, suffix in enumerate(("b", "c", "d"), start=1):
        repository.commit_proposal(
            _request(
                f"unrelated-{index}",
                f"operation-unrelated-{index}",
                suffix,
            ),
            _recognition(),
        )
    monkeypatch.setattr(
        catalog_repository,
        "CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT",
        1,
    )
    monkeypatch.setattr(catalog_repository, "_MAX_CATALOG_JSON_DOCUMENTS", 3)

    snapshot = repository.list_proposals("book-1")

    assert tuple(value.proposal_ref for value in snapshot.proposals) == (
        requested.proposal_ref,
    )


def test_unrelated_recognition_bytes_do_not_consume_requested_item_budget(
    tmp_path,
    monkeypatch,
) -> None:
    repository = _repository(tmp_path)
    requested = repository.commit_proposal(
        _request("book-1", "operation-requested", "a"),
        _recognition("small requested proposal"),
    )
    unrelated = repository.commit_proposal(
        _request("book-2", "operation-unrelated", "b"),
        _recognition("x" * (2 * 1024 * 1024)),
    )
    proposal_root = (
        tmp_path
        / ".engine"
        / "correction-transforms"
        / "ocr-proposals"
    )
    receipt_root = (
        tmp_path
        / ".engine"
        / "receipts"
        / "correction-ocr-proposals"
    )
    operation_root = (
        tmp_path
        / ".engine"
        / "receipts"
        / "correction-ocr-proposal-operations"
    )
    requested_operation = next(
        path
        for path in operation_root.glob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["operation_id"]
        == "operation-requested"
    )
    requested_bytes = sum(
        path.stat().st_size
        for path in (
            proposal_root / f"{requested.proposal_ref}.json",
            receipt_root / f"{requested.proposal_ref}.json",
            requested_operation,
        )
    )
    assert (
        proposal_root / f"{unrelated.proposal_ref}.json"
    ).stat().st_size > requested_bytes
    monkeypatch.setattr(
        catalog_repository,
        "_MAX_CATALOG_JSON_BYTES",
        requested_bytes,
    )

    snapshot = repository.list_proposals("book-1")

    assert tuple(value.proposal_ref for value in snapshot.proposals) == (
        requested.proposal_ref,
    )


@pytest.mark.parametrize(
    ("budget_name", "maximum"),
    (
        ("_MAX_CATALOG_DIRECTORY_ENTRIES", 1),
        ("_MAX_CATALOG_WORKSPACE_PROPOSALS", 0),
        ("_MAX_CATALOG_CLAIM_JSON_DOCUMENTS", 1),
        ("_MAX_CATALOG_CLAIM_JSON_BYTES", 1),
        ("CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT", 0),
        ("_MAX_CATALOG_JSON_DOCUMENTS", 2),
        ("_MAX_CATALOG_JSON_BYTES", 1),
    ),
)
def test_catalog_enforces_scan_count_and_json_budgets(
    tmp_path,
    monkeypatch,
    budget_name,
    maximum,
) -> None:
    repository = _repository(tmp_path)
    repository.commit_proposal(
        _request("book-1", "operation-1", "a"),
        _recognition(),
    )
    monkeypatch.setattr(catalog_repository, budget_name, maximum)

    with pytest.raises(RepositoryError) as raised:
        repository.list_proposals("book-1")

    assert (
        raised.value.code
        == "correction_ocr_proposal_catalog_budget_exceeded"
    )
    assert raised.value.details["maximum"] == maximum
    assert "path" not in json.dumps(dict(raised.value.details)).casefold()


def test_catalog_revision_changes_only_when_verified_receipts_change(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    first = repository.list_proposals("book-1")
    repository.commit_proposal(
        _request("book-2", "operation-other", "b"),
        _recognition(),
    )
    other_item = repository.list_proposals("book-1")
    repository.commit_proposal(
        _request("book-1", "operation-1", "a"),
        _recognition(),
    )
    changed = repository.list_proposals("book-1")

    assert other_item.snapshot_revision == first.snapshot_revision
    assert changed.snapshot_revision != first.snapshot_revision
