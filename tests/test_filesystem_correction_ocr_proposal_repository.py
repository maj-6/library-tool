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
    )


def _repository(tmp_path, source_bytes_for=None):
    return FilesystemCorrectionOcrProposalRepository(
        RecoverableWriteSet(tmp_path),
        source_bytes_for=source_bytes_for or (lambda _source: SOURCE_BYTES),
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
    assert "human_assertions" not in document
    assert "canonical_text" not in document


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

    def read_source(source):
        calls.append(source)
        return SOURCE_BYTES

    repository = _repository(tmp_path, read_source)
    request = _request()

    assert repository.read_source(request) == SOURCE_BYTES
    assert calls == [request.source]

    tampered = _repository(tmp_path, lambda _source: b"tampered")
    with pytest.raises(RepositoryError) as mismatch:
        tampered.read_source(request)
    assert mismatch.value.code == "correction_ocr_source_checksum_mismatch"

    missing = _repository(tmp_path, lambda _source: None)
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
    document["provider"]["provider_id"] = "different"
    document_path.write_text(
        json.dumps(document),
        encoding="utf-8",
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
