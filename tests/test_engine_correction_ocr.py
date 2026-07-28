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
from librarytool.engine.errors import ConflictError
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
                    recognition.options,
                ),
            )
        return self.stored


class Provider:
    def __init__(self) -> None:
        self.calls = 0
        self.contents = []
        self.selections = []

    def select_provider(self):
        return CorrectionOcrProviderSelection("tesseract", "5.4")

    def recognize(self, selection, content, hooks):
        self.calls += 1
        self.contents.append(content)
        self.selections.append(selection)
        return CorrectionOcrRecognition(
            selection.provider_id,
            selection.model,
            {
                "text": "A verified-looking machine proposal",
                "words": [{"text": "machine", "box": [0.1, 0.2, 0.3, 0.4]}],
            },
            selection.options,
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
        "options": {},
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


def test_interrupted_child_reconciles_a_proposal_committed_before_crash() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    first = service.run_ocr_followup(request, Hooks())
    record = jobs.records[service.job_id_for(request.operation_id)]
    jobs.transition(
        record,
        "interrupted",
        outputs=[],
        note="process stopped after proposal commit",
    )

    recovered = service.run_ocr_followup(request, Hooks())

    assert recovered == first
    assert provider.calls == 1
    child = jobs.view(service.job_id_for(request.operation_id))
    assert child.state.value == "done"
    assert child.outputs[0].ref == first.proposal_ref


def test_outcome_query_prefers_durable_proposal_without_mutating_child() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    first = service.run_ocr_followup(request, Hooks())
    record = jobs.records[service.job_id_for(request.operation_id)]
    jobs.transition(
        record,
        "cancelled",
        outputs=[],
        note="stale cancellation after proposal publication",
    )

    recovered = service.find_ocr_followup(request)

    assert recovered == first
    assert jobs.view(record["id"]).state.value == "cancelled"
    assert jobs.view(record["id"]).outputs == ()
    assert provider.calls == 1
    assert repository.commits == 1


def test_outcome_query_does_not_resume_interrupted_child() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    record = {
        "id": service.job_id_for(request.operation_id),
        "kind": CORRECTION_OCR_JOB_KIND,
        "status": "running",
        "subject": {
            "item_id": request.item_id,
            "source_id": request.source.artifact_id,
        },
        "input_revisions": {
            "parent_operation_id": request.operation_id,
            "artifact_id": request.source.artifact_id,
            "artifact_revision": request.source.artifact_revision,
            "source_sha256": request.source.content_sha256,
            "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
            "command_sha256": service._command_sha256(request),
        },
    }
    jobs.track(record, CORRECTION_OCR_JOB_KIND)
    jobs.transition(record, "interrupted")

    assert service.find_ocr_followup(request) is None
    assert jobs.view(record["id"]).state.value == "interrupted"
    assert repository.reads == 0
    assert provider.calls == 0


def test_outcome_query_recovers_pruned_failed_child_receipt() -> None:
    jobs = JobManager(keep=0, checkpoint_interval=0)
    repository = MemoryProposalRepository()
    service = CorrectionOcrFollowupService(
        jobs,
        repository,
        FailingProvider(),
    )
    request = _request()
    failed = service.run_ocr_followup(request, Hooks())

    assert failed.state is OcrFollowupState.FAILED
    assert jobs.view(service.job_id_for(request.operation_id)) is None

    recovered = service.find_ocr_followup(request)

    assert recovered == failed
    assert recovered.failure.code == "ocr_followup_failed"
    assert repository.reads == 1
    assert repository.commits == 0


def test_outcome_query_recovers_pruned_cancelled_child_receipt() -> None:
    jobs = JobManager(keep=0, checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    cancelled = service.run_ocr_followup(
        request,
        Hooks(cancelled=True),
    )

    assert cancelled.state is OcrFollowupState.CANCELLED
    assert jobs.view(service.job_id_for(request.operation_id)) is None

    recovered = service.find_ocr_followup(request)

    assert recovered == cancelled
    assert repository.reads == 0
    assert provider.calls == 0


def test_interrupted_child_without_proposal_retries_exact_source() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    record = {
        "id": service.job_id_for(request.operation_id),
        "kind": CORRECTION_OCR_JOB_KIND,
        "status": "running",
        "subject": {
            "item_id": request.item_id,
            "source_id": request.source.artifact_id,
        },
        "total": 4,
        "input_revisions": {
            "parent_operation_id": request.operation_id,
            "artifact_id": request.source.artifact_id,
            "artifact_revision": request.source.artifact_revision,
            "source_sha256": request.source.content_sha256,
            "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
            "command_sha256": service._command_sha256(request),
        },
    }
    jobs.track(record, CORRECTION_OCR_JOB_KIND)
    jobs.transition(record, "interrupted")

    result = service.run_ocr_followup(request, Hooks())

    assert result.state is OcrFollowupState.SUCCEEDED
    assert provider.calls == 1
    assert repository.reads == 1
    assert jobs.view(record["id"]).state.value == "done"


def test_interrupted_child_reuses_its_persisted_provider_selection() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()

    class ChangedProvider(Provider):
        def __init__(self):
            super().__init__()
            self.select_calls = 0

        def select_provider(self):
            self.select_calls += 1
            return CorrectionOcrProviderSelection(
                "textract",
                "detect-document-text",
                {"aws_region": "eu-central-1"},
            )

    provider = ChangedProvider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    pinned = CorrectionOcrProviderSelection(
        "textract",
        "detect-document-text",
        {"aws_region": "us-west-2"},
    )
    record = {
        "id": service.job_id_for(request.operation_id),
        "kind": CORRECTION_OCR_JOB_KIND,
        "status": "running",
        "subject": {
            "item_id": request.item_id,
            "source_id": request.source.artifact_id,
        },
        "total": 4,
        "input_revisions": {
            "parent_operation_id": request.operation_id,
            "artifact_id": request.source.artifact_id,
            "artifact_revision": request.source.artifact_revision,
            "source_sha256": request.source.content_sha256,
            "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
            "command_sha256": service._command_sha256(request),
            "provider": pinned.as_dict(),
        },
    }
    jobs.track(record, CORRECTION_OCR_JOB_KIND)
    jobs.transition(record, "interrupted")

    result = service.run_ocr_followup(request, Hooks())

    assert result.state is OcrFollowupState.SUCCEEDED
    assert provider.select_calls == 0
    assert provider.selections == [pinned]
    assert jobs.view(record["id"]).input_revisions["provider"] == (
        pinned.as_dict()
    )


def test_durable_proposal_restores_provider_pin_after_job_history_loss() -> None:
    repository = MemoryProposalRepository()
    initial = Provider()
    first_jobs = JobManager(keep=0, checkpoint_interval=0)
    request = _request()
    CorrectionOcrFollowupService(
        first_jobs,
        repository,
        initial,
    ).run_ocr_followup(request, Hooks())

    class MustNotRun(Provider):
        def select_provider(self):
            raise AssertionError("durable replay must not select a provider")

        def recognize(self, selection, content, hooks):
            raise AssertionError("durable replay must not invoke OCR")

    reopened_jobs = JobManager(keep=1, checkpoint_interval=0)
    service = CorrectionOcrFollowupService(
        reopened_jobs,
        repository,
        MustNotRun(),
    )
    replay = service.run_ocr_followup(request, Hooks())

    assert replay.state is OcrFollowupState.SUCCEEDED
    child = reopened_jobs.view(service.job_id_for(request.operation_id))
    assert child.input_revisions["provider"] == (
        repository.stored.provider.as_dict()
    )


@pytest.mark.parametrize(
    ("pin_name", "pin_value"),
    (
        ("artifact_id", "other-artifact"),
        ("artifact_id", None),
        ("command_sha256", "b" * 64),
        ("command_sha256", None),
    ),
)
def test_existing_child_requires_every_exact_request_pin(
    pin_name,
    pin_value,
) -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    service = CorrectionOcrFollowupService(jobs, repository, provider)
    request = _request()
    pins = {
        "parent_operation_id": request.operation_id,
        "artifact_id": request.source.artifact_id,
        "artifact_revision": request.source.artifact_revision,
        "source_sha256": request.source.content_sha256,
        "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
        "command_sha256": service._command_sha256(request),
    }
    if pin_value is None:
        pins.pop(pin_name)
    else:
        pins[pin_name] = pin_value
    record = {
        "id": service.job_id_for(request.operation_id),
        "kind": CORRECTION_OCR_JOB_KIND,
        "status": "interrupted",
        "subject": {
            "item_id": request.item_id,
            "source_id": request.source.artifact_id,
        },
        "input_revisions": pins,
    }
    jobs.track(record, CORRECTION_OCR_JOB_KIND)
    jobs.transition(record, "interrupted")

    with pytest.raises(ValueError, match="does not match"):
        service.run_ocr_followup(request, Hooks())

    assert provider.calls == 0


def test_outcome_query_normalizes_invalid_child_pins_to_engine_conflict() -> None:
    jobs = JobManager(checkpoint_interval=0)
    service = CorrectionOcrFollowupService(
        jobs,
        MemoryProposalRepository(),
        Provider(),
    )
    request = _request()
    record = {
        "id": service.job_id_for(request.operation_id),
        "kind": CORRECTION_OCR_JOB_KIND,
        "status": "interrupted",
        "subject": {
            "item_id": request.item_id,
            "source_id": request.source.artifact_id,
        },
        "input_revisions": {
            "parent_operation_id": request.operation_id,
            "artifact_id": "different-artifact",
            "artifact_revision": request.source.artifact_revision,
            "source_sha256": request.source.content_sha256,
            "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
            "command_sha256": service._command_sha256(request),
        },
    }
    jobs.track(record, CORRECTION_OCR_JOB_KIND)
    jobs.transition(record, "interrupted")

    with pytest.raises(ConflictError) as raised:
        service.find_ocr_followup(request)

    assert raised.value.code == "correction_ocr_reconciliation_conflict"


def test_done_child_fails_if_its_durable_proposal_disappears() -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    service = CorrectionOcrFollowupService(jobs, repository, Provider())
    request = _request()
    service.run_ocr_followup(request, Hooks())
    repository.stored = None

    result = service.run_ocr_followup(request, Hooks())

    assert result.state is OcrFollowupState.FAILED
    assert result.failure.code == "ocr_proposal_missing"
    child = jobs.view(service.job_id_for(request.operation_id))
    assert child.state.value == "failed"
    assert child.outputs == ()


@pytest.mark.parametrize(
    "provider_pin",
    (
        None,
        {
            "provider_id": "textract",
            "model": "detect-document-text",
            "options": {"aws_region": "us-west-2"},
        },
    ),
)
def test_done_child_replay_requires_its_exact_provider_pin(
    provider_pin,
) -> None:
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    service = CorrectionOcrFollowupService(
        jobs,
        repository,
        Provider(),
    )
    request = _request()
    service.run_ocr_followup(request, Hooks())
    record = jobs.records[service.job_id_for(request.operation_id)]
    inputs = dict(record["input_revisions"])
    if provider_pin is None:
        inputs.pop("provider")
    else:
        inputs["provider"] = provider_pin
    record["input_revisions"] = inputs

    replay = service.run_ocr_followup(request, Hooks())

    assert replay.state is OcrFollowupState.FAILED
    assert replay.failure.code in {
        "invalid_ocr_provider_pin",
        "correction_ocr_provider_pin_mismatch",
    }
    child = jobs.view(record["id"])
    assert child.state.value == "failed"
    assert child.outputs == ()


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
    assert result.failure.retryable is False
    child = jobs.view(service.job_id_for(request.operation_id))
    assert child.state.value == "failed"
    assert child.error.code == "ocr_followup_failed"
    assert child.outputs == ()


def test_provider_exception_details_never_enter_public_job_state() -> None:
    secret = "Authorization: Bearer TOP-SECRET-SENTINEL"

    class LeakingProvider(Provider):
        def recognize(self, selection, content, hooks):
            raise RuntimeError(secret)

    jobs = JobManager(checkpoint_interval=0)
    service = CorrectionOcrFollowupService(
        jobs,
        MemoryProposalRepository(),
        LeakingProvider(),
    )

    result = service.run_ocr_followup(_request(), Hooks())
    public = jobs.get(service.job_id_for(_request().operation_id))

    assert result.failure.message == "OCR provider failed"
    assert secret not in str(result.as_dict())
    assert secret not in str(public)


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
