from __future__ import annotations

import hashlib

import pytest

from librarytool.engine.correction_ocr import (
    CORRECTION_OCR_JOB_KIND,
    CORRECTION_OCR_PROPOSAL_POLICY,
    CorrectionOcrFollowupService,
    CorrectionOcrProposalRepositoryPort,
    CorrectionOcrProviderPort,
    CorrectionOcrProviderSelection,
    CorrectionOcrRecognition,
    StoredCorrectionOcrProposal,
)
from librarytool.engine.correction_transforms import (
    CommittedCorrectionOutput,
    CorrectionTransformHooksPort,
    OcrFollowupRequest,
    OcrFollowupState,
)
from librarytool.engine.jobs import JobManager


def _source(content: bytes = b"exact corrected image") -> CommittedCorrectionOutput:
    return CommittedCorrectionOutput(
        "ocr-ready",
        "corrected-ocr-ready",
        "corrected-r1",
        hashlib.sha256(content).hexdigest(),
    )


def _request(content: bytes = b"exact corrected image") -> OcrFollowupRequest:
    return OcrFollowupRequest("parent-operation-1", "book-1", _source(content))


class Hooks:
    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.progress = []

    def is_cancelled(self):
        return self.cancelled

    def report_progress(self, progress):
        self.progress.append(progress)


class MemoryProposalRepository:
    def __init__(self, content: bytes = b"exact corrected image") -> None:
        self.content = content
        self.stored = None
        self.reads = 0
        self.commits = 0

    def read_source(self, request):
        self.reads += 1
        return self.content

    def find_proposal(self, request):
        if self.stored is not None and self.stored.source == request.source:
            return self.stored
        return None

    def commit_proposal(self, request, recognition):
        self.commits += 1
        if self.stored is None:
            self.stored = StoredCorrectionOcrProposal(
                "ocr-proposal-parent-operation-1",
                request.source,
                CorrectionOcrProviderSelection(
                    recognition.provider_id,
                    recognition.model,
                ),
            )
        return self.stored


class Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.contents = []

    def select_provider(self):
        return CorrectionOcrProviderSelection("tesseract", "5.4")

    def recognize(self, selection, content, hooks):
        self.calls += 1
        self.contents.append(content)
        return CorrectionOcrRecognition(
            selection.provider_id,
            selection.model,
            {
                "text": "A verified-looking machine proposal",
                "words": [{"text": "machine", "box": [0.1, 0.2, 0.3, 0.4]}],
            },
        )


class FailingProvider(Provider):
    def recognize(self, selection, content, hooks):
        raise RuntimeError("provider unavailable")


def test_followup_runs_as_separate_job_and_publishes_only_a_proposal() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()

    result = service.run_ocr_followup(request, Hooks())

    assert result.state is OcrFollowupState.SUCCEEDED
    assert result.source == request.source
    assert result.proposal_ref == "ocr-proposal-parent-operation-1"
    assert repository.reads == 1
    assert repository.commits == 1
    assert provider.contents == [b"exact corrected image"]
    child = jobs.view(service.job_id_for(request.operation_id))
    assert child.kind == CORRECTION_OCR_JOB_KIND
    assert child.state.value == "done"
    assert child.subject.item_id == "book-1"
    assert child.subject.source_id == request.source.artifact_id
    assert child.input_revisions["artifact_revision"] == "corrected-r1"
    assert child.input_revisions["source_sha256"] == request.source.content_sha256
    assert (
        child.input_revisions["publication_policy"]
        == CORRECTION_OCR_PROPOSAL_POLICY
    )
    assert child.input_revisions["provider"] == {
        "provider_id": "tesseract",
        "model": "5.4",
    }
    assert [output.as_dict() for output in child.outputs] == [
        {
            "kind": "ocr-proposal",
            "ref": "ocr-proposal-parent-operation-1",
            "partial": False,
        }
    ]


def test_durable_proposal_replay_does_not_pay_for_ocr_again_after_job_prune() -> None:
    jobs = JobManager(keep=0, checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()

    first = service.run_ocr_followup(request, Hooks())
    replay = service.run_ocr_followup(request, Hooks())

    assert first == replay
    assert provider.calls == 1
    assert repository.reads == 1
    assert repository.commits == 1


def test_provider_failure_is_structured_on_the_child_job() -> None:
    jobs = JobManager(checkpoint_interval=0)
    service = CorrectionOcrFollowupService(
        jobs,
        MemoryProposalRepository(),
        FailingProvider(),
    )
    request = _request()

    result = service.run_ocr_followup(request, Hooks())

    assert result.state is OcrFollowupState.FAILED
    assert result.failure.code == "ocr_followup_failed"
    assert result.failure.retryable is True
    child = jobs.view(service.job_id_for(request.operation_id))
    assert child.state.value == "failed"
    assert child.error.code == "ocr_followup_failed"
    assert child.outputs == ()


def test_checksum_mismatch_fails_before_invoking_provider() -> None:
    jobs = JobManager(checkpoint_interval=0)
    provider = Provider()
    service = CorrectionOcrFollowupService(
        jobs,
        MemoryProposalRepository(b"tampered bytes"),
        provider,
    )

    result = service.run_ocr_followup(_request(), Hooks())

    assert result.state is OcrFollowupState.FAILED
    assert "checksum" in result.failure.message
    assert provider.calls == 0


def test_parent_cancellation_cancels_child_before_reading_source() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    service = CorrectionOcrFollowupService(jobs, repository, Provider())
    request = _request()

    result = service.run_ocr_followup(request, Hooks(cancelled=True))

    assert result.state is OcrFollowupState.CANCELLED
    assert repository.reads == 0
    child = jobs.view(service.job_id_for(request.operation_id))
    assert child.state.value == "cancelled"


def test_recognition_payload_is_json_only_bounded_and_defensively_frozen() -> None:
    raw = {"text": "machine", "regions": [{"score": 0.8}]}
    recognition = CorrectionOcrRecognition("mistral", "ocr-4", raw)
    raw["regions"][0]["score"] = 0.1

    assert recognition.as_dict()["payload"]["regions"][0]["score"] == 0.8
    with pytest.raises(TypeError):
        recognition.payload["text"] = "changed"
    with pytest.raises(ValueError, match="JSON"):
        CorrectionOcrRecognition("mistral", "", {"image": b"bytes"})
    with pytest.raises(ValueError, match="non-finite"):
        CorrectionOcrRecognition("mistral", "", {"score": float("nan")})


def test_ports_are_runtime_checkable_and_framework_neutral() -> None:
    assert isinstance(MemoryProposalRepository(), CorrectionOcrProposalRepositoryPort)
    assert isinstance(Provider(), CorrectionOcrProviderPort)
    assert isinstance(Hooks(), CorrectionTransformHooksPort)
