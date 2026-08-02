from __future__ import annotations

import hashlib

import pytest

from librarytool.engine.correction_ocr import (
    CORRECTION_OCR_JOB_KIND,
    CORRECTION_REOCR_OPERATION_PREFIX,
    CorrectionOcrFollowupService,
    CorrectionOcrProviderSelection,
    CorrectionOcrRecognition,
    CorrectionReocrService,
    StoredCorrectionOcrProposal,
)
from librarytool.engine.correction_transforms import (
    CommittedCorrectionOutput,
    OcrFollowupRequest,
    OcrFollowupState,
)
from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from librarytool.engine.jobs import JobManager
from librarytool.engine.raster_artifacts import (
    ArtifactFreshness,
    ArtifactProvenance,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)


CONTENT = b"exact OCR-ready rendition"


class Hooks:
    def is_cancelled(self):
        return False

    def report_progress(self, _progress):
        return None


def _view(
    artifact_id: str,
    *,
    kind: str = "processed-source",
    content: bytes = CONTENT,
    extensions=None,
) -> RasterArtifactView:
    return RasterArtifactView(
        key=RasterArtifactKey("book-1", artifact_id),
        revision=f"{artifact_id}-r1",
        kind=kind,
        media_type="image/png",
        content_sha256=hashlib.sha256(content).hexdigest(),
        dimensions=RasterDimensions(40, 60),
        source=RasterSourceRef(
            "capture",
            "capture-r1",
            "page-1",
            "page-1-r1",
        ),
        resource_state=ResourceState.AVAILABLE,
        resource=RasterResourceRef(
            f"correction-raster:{artifact_id}",
            f"{artifact_id}-resource-r1",
        ),
        label=artifact_id,
        freshness=ArtifactFreshness.UNTRACKED,
        provenance=ArtifactProvenance(origin="transform"),
        extensions=extensions or {},
    )


def _transform_view(
    artifact_id: str,
    *,
    output_kind: str,
    operation_id: str = "transform-op-1",
    content: bytes = CONTENT,
) -> RasterArtifactView:
    return _view(
        artifact_id,
        kind=(
            "corrected-image"
            if output_kind == "corrected-display"
            else "processed-source"
        ),
        content=content,
        extensions={
            "correction_transform": {
                "operation_id": operation_id,
                "output_kind": output_kind,
                "source_revision": "source-r1",
            }
        },
    )


class _Rasters:
    def __init__(self, rows):
        self.rows = tuple(rows)

    def list_raster_artifacts(self, item_id):
        return tuple(row for row in self.rows if row.key.item_id == item_id)

    def get_raster_artifact(self, key):
        return next((row for row in self.rows if row.key == key), None)


class MemoryProposalRepository:
    """Per-operation proposals, mirroring the durable operation receipt."""

    def __init__(self, content: bytes = CONTENT) -> None:
        self.content = content
        self.stored: dict[str, StoredCorrectionOcrProposal] = {}
        self.reads = 0
        self.commits = 0

    def read_source(self, request):
        self.reads += 1
        return self.content

    def find_proposal(self, request):
        return self.stored.get(request.operation_id)

    def commit_proposal(self, request, recognition):
        self.commits += 1
        return self.stored.setdefault(
            request.operation_id,
            StoredCorrectionOcrProposal(
                f"proposal-{len(self.stored) + 1}",
                request.source,
                CorrectionOcrProviderSelection(
                    recognition.provider_id,
                    recognition.model,
                    recognition.options,
                ),
            ),
        )


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    def select_provider(self):
        return CorrectionOcrProviderSelection("tesseract", "5.4")

    def recognize(self, selection, content, hooks):
        self.calls += 1
        return CorrectionOcrRecognition(
            selection.provider_id,
            selection.model,
            {"text": "A machine proposal"},
            selection.options,
        )


def _harness():
    jobs = JobManager(checkpoint_interval=0)
    repository = MemoryProposalRepository()
    provider = Provider()
    followup = CorrectionOcrFollowupService(jobs, repository, provider)
    display = _transform_view(
        "ctr-display-1",
        output_kind="corrected-display",
        content=b"display bytes",
    )
    ocr_ready = _transform_view("ctr-ocr-1", output_kind="ocr-ready")
    service = CorrectionReocrService(
        followup,
        _Rasters((display, ocr_ready, _view("capture-base-1"))),
    )
    return service, followup, jobs, repository, provider, display, ocr_ready


def test_fresh_key_mints_a_new_proposal_for_the_same_rendition() -> None:
    service, _followup, jobs, repository, provider, display, ocr_ready = (
        _harness()
    )

    first = service.queue_reocr(
        "book-1",
        display.key.artifact_id,
        expected_artifact_revision=display.revision,
        idempotency_key="key-one",
    )
    first_outcome = service.execute_queued(first.request)
    second = service.queue_reocr(
        "book-1",
        display.key.artifact_id,
        expected_artifact_revision=display.revision,
        idempotency_key="key-two",
    )
    second_outcome = service.execute_queued(second.request)

    assert first.created is True
    assert second.created is True
    assert first.request.operation_id != second.request.operation_id
    assert first.request.source == second.request.source
    assert first.request.source.artifact_id == ocr_ready.key.artifact_id
    assert first_outcome.state is OcrFollowupState.SUCCEEDED
    assert second_outcome.state is OcrFollowupState.SUCCEEDED
    assert first_outcome.proposal_ref != second_outcome.proposal_ref
    assert provider.calls == 2
    assert repository.commits == 2
    for queued in (first, second):
        view = jobs.view(queued.job_id)
        assert view.kind == CORRECTION_OCR_JOB_KIND
        assert view.state.value == "done"


def test_same_key_replays_the_stored_proposal_without_provider_work() -> None:
    service, _followup, _jobs, repository, provider, display, _ocr_ready = (
        _harness()
    )
    first = service.queue_reocr(
        "book-1",
        display.key.artifact_id,
        expected_artifact_revision=display.revision,
        idempotency_key="key-one",
    )
    outcome = service.execute_queued(first.request)

    replay = service.queue_reocr(
        "book-1",
        display.key.artifact_id,
        expected_artifact_revision=display.revision,
        idempotency_key="key-one",
    )

    assert replay.created is False
    assert replay.job_id == first.job_id
    assert replay.job.state.value == "done"
    assert [output.as_dict() for output in replay.job.outputs] == [
        {
            "kind": "ocr-proposal",
            "ref": outcome.proposal_ref,
            "partial": False,
        }
    ]
    assert provider.calls == 1
    assert repository.commits == 1
    with pytest.raises(ConflictError) as claimed:
        service.execute_queued(replay.request)
    assert claimed.value.code == "correction_ocr_job_already_claimed"


def test_ocr_ready_artifact_is_its_own_reocr_source() -> None:
    service, _followup, _jobs, _repository, _provider, _display, ocr_ready = (
        _harness()
    )

    queued = service.queue_reocr(
        "book-1",
        ocr_ready.key.artifact_id,
        expected_artifact_revision=ocr_ready.revision,
        idempotency_key="key-one",
    )

    assert queued.request.source.artifact_id == ocr_ready.key.artifact_id
    assert queued.request.source.content_sha256 == ocr_ready.content_sha256


def test_standalone_operations_stay_disjoint_from_transform_operations() -> None:
    service, followup, jobs, repository, provider, display, ocr_ready = (
        _harness()
    )
    derived = CorrectionReocrService.operation_id_for("parent-operation-1")
    assert derived.startswith(CORRECTION_REOCR_OPERATION_PREFIX)
    assert derived != "parent-operation-1"

    transform_request = OcrFollowupRequest(
        "parent-operation-1",
        "book-1",
        CommittedCorrectionOutput(
            "ocr-ready",
            ocr_ready.key.artifact_id,
            ocr_ready.revision,
            ocr_ready.content_sha256,
        ),
    )
    transform_outcome = followup.run_ocr_followup(transform_request, Hooks())

    queued = service.queue_reocr(
        "book-1",
        display.key.artifact_id,
        expected_artifact_revision=display.revision,
        idempotency_key="parent-operation-1",
    )
    standalone_outcome = service.execute_queued(queued.request)

    assert queued.created is True
    assert queued.request.operation_id == derived
    assert transform_outcome.state is OcrFollowupState.SUCCEEDED
    assert standalone_outcome.state is OcrFollowupState.SUCCEEDED
    assert standalone_outcome.proposal_ref != transform_outcome.proposal_ref
    assert provider.calls == 2
    transform_job = jobs.view(
        CorrectionOcrFollowupService.job_id_for("parent-operation-1")
    )
    assert transform_job.state.value == "done"
    assert [output.ref for output in transform_job.outputs] == [
        transform_outcome.proposal_ref
    ]

    # A colliding operation id that pins a different rendition must conflict
    # during replay validation rather than adopt the standalone child job.
    different_source = CommittedCorrectionOutput(
        "ocr-ready",
        ocr_ready.key.artifact_id,
        ocr_ready.revision,
        hashlib.sha256(b"another rendition").hexdigest(),
    )
    with pytest.raises(ConflictError) as conflicted:
        followup.find_ocr_followup(
            OcrFollowupRequest(derived, "book-1", different_source)
        )
    assert conflicted.value.code == "correction_ocr_reconciliation_conflict"
    assert repository.commits == 2


def test_queue_validates_target_revision_and_key() -> None:
    service, _followup, _jobs, _repository, _provider, display, _ocr_ready = (
        _harness()
    )

    with pytest.raises(NotFoundError):
        service.queue_reocr(
            "book-1",
            "missing-artifact",
            expected_artifact_revision="whatever",
            idempotency_key="key-one",
        )
    with pytest.raises(ValidationError) as not_transform:
        service.queue_reocr(
            "book-1",
            "capture-base-1",
            expected_artifact_revision="capture-base-1-r1",
            idempotency_key="key-one",
        )
    assert not_transform.value.code == (
        "correction_reocr_source_not_transform_output"
    )
    with pytest.raises(ConflictError) as conflicted:
        service.queue_reocr(
            "book-1",
            display.key.artifact_id,
            expected_artifact_revision="stale-revision",
            idempotency_key="key-one",
        )
    assert conflicted.value.code == "artifact_revision_conflict"
    with pytest.raises(ValidationError) as invalid_key:
        service.queue_reocr(
            "book-1",
            display.key.artifact_id,
            expected_artifact_revision=display.revision,
            idempotency_key="not portable!",
        )
    assert invalid_key.value.code == "invalid_correction_reocr_request"
