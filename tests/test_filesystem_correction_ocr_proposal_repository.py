from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import replace

import pytest

from librarytool.adapters.filesystem import (
    FilesystemCorrectionOcrProposalRepository,
    RecoverableWriteSet,
)
from librarytool.engine.correction_ocr import (
    CORRECTION_OCR_PROPOSAL_POLICY,
    CorrectionOcrProposalQueryRepositoryPort,
    CorrectionOcrProposalQueryService,
    CorrectionOcrProposalRepositoryPort,
    CorrectionOcrRecognition,
)
from librarytool.engine.correction_transforms import (
    CommittedCorrectionOutput,
    OcrFollowupRequest,
)
from librarytool.engine.errors import ConflictError, NotFoundError, RepositoryError


SOURCE_BYTES = b"immutable OCR-ready raster bytes"


def _source(content: bytes = SOURCE_BYTES) -> CommittedCorrectionOutput:
    return CommittedCorrectionOutput(
        "ocr-ready",
        "corrected-ocr-ready",
        "corrected-r1",
        hashlib.sha256(content).hexdigest(),
    )


def _request(
    *,
    operation_id: str = "parent-operation-1",
    source: CommittedCorrectionOutput | None = None,
) -> OcrFollowupRequest:
    return OcrFollowupRequest(
        operation_id,
        "book-1",
        source or _source(),
    )


def _recognition(text: str = "Machine proposal") -> CorrectionOcrRecognition:
    return CorrectionOcrRecognition(
        "mistral",
        "ocr-4",
        {
            "text": text,
            "regions": [
                {
                    "role": "marginalia",
                    "box": [0.1, 0.2, 0.3, 0.4],
                }
            ],
        },
        {"include_blocks": True},
    )


def _repository(tmp_path, source_bytes_for=None):
    return FilesystemCorrectionOcrProposalRepository(
        RecoverableWriteSet(tmp_path),
        source_bytes_for=source_bytes_for
        or (lambda _item_id, _operation_id, _source: SOURCE_BYTES),
        lock_context_for=nullcontext,
        recover=False,
    )


def test_commit_and_find_round_trip_an_immutable_machine_proposal(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()

    stored = repository.commit_proposal(request, _recognition())
    replay = repository.find_proposal(request)

    assert replay == stored
    assert stored.proposal_ref.startswith("cop-")
    assert "/" not in stored.proposal_ref
    assert stored.source == request.source
    assert stored.provider.provider_id == "mistral"
    assert stored.provider.options == {"include_blocks": True}
    assert isinstance(repository, CorrectionOcrProposalRepositoryPort)
    documents = list(
        (
            tmp_path
            / ".engine"
            / "correction-transforms"
            / "ocr-proposals"
        ).glob("*.json")
    )
    assert len(documents) == 1
    payload = documents[0].read_bytes()
    document = json.loads(payload)
    assert payload == json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert document["publication_policy"] == CORRECTION_OCR_PROPOSAL_POLICY
    assert document["recognition"]["payload"]["text"] == "Machine proposal"
    assert document["provider"]["options"] == {"include_blocks": True}
    assert document["recognition"]["options"] == {"include_blocks": True}
    assert "human_assertions" not in document
    assert "canonical_text" not in document
    receipts = list(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposals"
        ).glob("*.json")
    )
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["proposal_ref"] == stored.proposal_ref
    assert receipt["proposal_sha256"] == hashlib.sha256(payload).hexdigest()


def test_opaque_item_scoped_query_returns_verified_public_payload(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()
    recognition = CorrectionOcrRecognition(
        "tesseract",
        "local",
        {"text": "Machine proposal", "regions": []},
        {"tesseract": "C:\\private\\tesseract.exe"},
    )
    stored = repository.commit_proposal(request, recognition)

    service = CorrectionOcrProposalQueryService(repository)
    proposal = service.get_proposal(request.item_id, stored.proposal_ref)

    assert proposal is not None
    assert proposal.proposal_ref == stored.proposal_ref
    assert proposal.item_id == request.item_id
    assert proposal.operation_id == request.operation_id
    assert proposal.source == request.source
    assert proposal.provider.as_dict() == {
        "provider_id": "tesseract",
        "model": "local",
    }
    assert proposal.recognition == {
        "text": "Machine proposal",
        "regions": (),
    }
    public = proposal.as_dict()
    assert public["recognition"] == {
        "text": "Machine proposal",
        "regions": [],
    }
    assert "options" not in public["provider"]
    assert "C:\\\\private" not in json.dumps(public)
    assert "credential" not in json.dumps(public).casefold()
    assert "path" not in public
    assert service.get_proposal("another-book", stored.proposal_ref) is None
    assert isinstance(repository, CorrectionOcrProposalQueryRepositoryPort)


def test_opaque_query_fails_closed_when_operation_claim_is_tampered(
    tmp_path,
) -> None:
    repository = _repository(tmp_path)
    request = _request()
    stored = repository.commit_proposal(request, _recognition())
    operation_path = next(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposal-operations"
        ).glob("*.json")
    )
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation["item_id"] = "another-book"
    operation_path.write_bytes(
        json.dumps(
            operation,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    with pytest.raises(RepositoryError) as raised:
        repository.get_proposal(request.item_id, stored.proposal_ref)

    assert raised.value.code == "invalid_correction_ocr_proposal"


@pytest.mark.parametrize(
    "private_payload",
    (
        {"storage_path": "C:\\private\\proposal.json"},
        {"credential": "must-not-be-persisted"},
    ),
)
def test_public_proposal_rejects_private_execution_metadata(
    tmp_path,
    private_payload,
) -> None:
    repository = _repository(tmp_path)
    recognition = CorrectionOcrRecognition(
        "mistral",
        "ocr-4",
        {"text": "Machine proposal", **private_payload},
    )

    with pytest.raises(RepositoryError) as raised:
        repository.commit_proposal(_request(), recognition)

    assert raised.value.code == "invalid_correction_ocr_proposal"
    assert not list(tmp_path.rglob("*.json"))


def test_replay_returns_first_proposal_without_replacing_machine_output(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()

    first = repository.commit_proposal(request, _recognition("First"))
    replay = repository.commit_proposal(request, _recognition("Different retry"))

    assert replay == first
    document = next(
        (
            tmp_path
            / ".engine"
            / "correction-transforms"
            / "ocr-proposals"
        ).glob("*.json")
    )
    assert json.loads(document.read_text(encoding="utf-8"))["recognition"][
        "payload"
    ]["text"] == "First"


def test_operation_reuse_for_another_source_conflicts(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()
    repository.commit_proposal(request, _recognition())
    other_source = replace(
        request.source,
        artifact_id="another-ocr-ready",
    )

    with pytest.raises(ConflictError) as raised:
        repository.find_proposal(_request(source=other_source))

    assert raised.value.code == "correction_ocr_operation_conflict"


def test_source_reader_returns_only_exact_checksum_pinned_bytes(tmp_path) -> None:
    calls = []

    def read_source(item_id, operation_id, source):
        calls.append((item_id, operation_id, source))
        return SOURCE_BYTES

    repository = _repository(tmp_path, read_source)
    request = _request()

    assert repository.read_source(request) == SOURCE_BYTES
    assert calls == [
        ("book-1", "parent-operation-1", request.source)
    ]

    tampered = _repository(
        tmp_path,
        lambda _item_id, _operation_id, _source: b"tampered",
    )
    with pytest.raises(RepositoryError) as mismatch:
        tampered.read_source(request)
    assert mismatch.value.code == "correction_ocr_source_checksum_mismatch"

    missing = _repository(
        tmp_path,
        lambda _item_id, _operation_id, _source: None,
    )
    with pytest.raises(NotFoundError) as absent:
        missing.read_source(request)
    assert absent.value.code == "correction_ocr_source_not_found"


def test_tampered_or_noncanonical_proposal_fails_closed(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()
    repository.commit_proposal(request, _recognition())
    document_path = next(
        (
            tmp_path
            / ".engine"
            / "correction-transforms"
            / "ocr-proposals"
        ).glob("*.json")
    )
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["recognition"]["payload"]["text"] = "Tampered but canonical"
    document_path.write_bytes(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    with pytest.raises(RepositoryError) as raised:
        repository.find_proposal(request)

    assert raised.value.code == "invalid_correction_ocr_proposal"


def test_missing_proposal_receipt_fails_closed(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()
    repository.commit_proposal(request, _recognition())
    receipt = next(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposals"
        ).glob("*.json")
    )
    receipt.unlink()

    with pytest.raises(RepositoryError) as raised:
        repository.find_proposal(request)

    assert raised.value.code == "invalid_correction_ocr_proposal"


def test_recognition_options_must_match_the_pinned_provider(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()
    repository.commit_proposal(request, _recognition())
    document_path = next(
        (
            tmp_path
            / ".engine"
            / "correction-transforms"
            / "ocr-proposals"
        ).glob("*.json")
    )
    receipt_path = next(
        (
            tmp_path
            / ".engine"
            / "receipts"
            / "correction-ocr-proposals"
        ).glob("*.json")
    )
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["recognition"]["options"]["include_blocks"] = False
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["proposal_sha256"] = hashlib.sha256(payload).hexdigest()
    document_path.write_bytes(payload)
    receipt_path.write_bytes(
        json.dumps(
            receipt,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    with pytest.raises(RepositoryError) as raised:
        repository.find_proposal(request)

    assert raised.value.code == "invalid_correction_ocr_proposal"


def test_unsafe_proposal_target_fails_closed(tmp_path) -> None:
    repository = _repository(tmp_path)
    request = _request()
    repository.commit_proposal(request, _recognition())
    document_path = next(
        (
            tmp_path
            / ".engine"
            / "correction-transforms"
            / "ocr-proposals"
        ).glob("*.json")
    )
    replacement = document_path.with_suffix(".replacement")
    document_path.rename(replacement)
    try:
        document_path.symlink_to(replacement)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RepositoryError) as raised:
        repository.find_proposal(request)

    assert raised.value.code in {
        "invalid_correction_ocr_proposal",
        "unsafe_correction_ocr_path",
    }
