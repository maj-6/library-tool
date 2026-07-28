"""Executable Corrections release gates that cross engine boundaries.

These scenarios intentionally compose the durable review, transform, job, and
filesystem adapters without the Flask host.  They cover behavior that unit
tests for any one layer cannot prove on their own.
"""

from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import replace
from pathlib import Path

from PIL import Image

from librarytool.adapters.filesystem import (
    FilesystemCorrectionRepository,
    FilesystemCorrectionTransformStore,
    FilesystemJobHistoryRepository,
    RecoverableWriteSet,
)
from librarytool.engine.corrections import (
    AnnotationCorrectionSnapshot,
    ArtifactCorrectionSnapshot,
    CorrectionAggregateSnapshot,
    CorrectionReviewSnapshot,
    CorrectionService,
    MarkAttentionCommand,
    ReopenCorrectionsCommand,
    ResolveCorrectionsCommand,
)
from librarytool.engine.correction_transforms import (
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    CorrectionTransformRunResult,
    CorrectionTransformService,
    CorrectionTransformWorker,
    HumanTextAssertion,
    OcrFollowupState,
)
from librarytool.engine.jobs import JobManager
from librarytool.engine.raster_artifacts import (
    ArtifactProvenance,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    RasterArtifactKey,
    RasterArtifactView,
    RasterDimensions,
    RasterResourceRef,
    RasterSourceRef,
)
from librarytool.engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
)
from librarytool.processing.raster import ManualBinaryAdjustRecipe


FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


class _Revisions:
    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self, kind: str, target_id: str) -> str:
        with self._lock:
            self._count += 1
            return f"{kind}-{target_id}-gate-r{self._count}"


class _SourceAuthority:
    def __init__(self, source: CorrectionSourceSnapshot) -> None:
        self.source = source

    def __call__(self, key: RasterArtifactKey) -> CorrectionSourceSnapshot | None:
        return self.source if key == self.source.artifact.key else None


class _BlockingRecordingStore:
    """Pause immediately before atomic publication, outside adapter locks."""

    def __init__(self, delegate: FilesystemCorrectionTransformStore) -> None:
        self._delegate = delegate
        self.commit_started = threading.Event()
        self.release_commit = threading.Event()
        self.draft = None

    def load_source(self, key: RasterArtifactKey) -> CorrectionSourceSnapshot:
        return self._delegate.load_source(key)

    def commit_transform(self, draft):
        self.draft = draft
        self.commit_started.set()
        if not self.release_commit.wait(5):
            raise TimeoutError("release gate did not resume transform publication")
        return self._delegate.commit_transform(draft)


class _FailingOcr:
    def run_ocr_followup(self, _request, _hooks):
        raise RuntimeError("release-gate provider outage")


def _png() -> bytes:
    image = Image.new("RGB", (64, 48), color=(118, 92, 61))
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _machine_provenance() -> ArtifactProvenance:
    return ArtifactProvenance(
        origin="mistral",
        provider_id="mistral",
        model="pixtral",
    )


def _source() -> CorrectionSourceSnapshot:
    content = _png()
    artifact = RasterArtifactView(
        key=RasterArtifactKey("gate-book", "capture-title"),
        revision="capture-title-r1",
        kind="captured-image",
        media_type="image/png",
        content_sha256=hashlib.sha256(content).hexdigest(),
        dimensions=RasterDimensions(64, 48),
        source=RasterSourceRef(
            "capture",
            "capture-representation-r1",
            "canvas-title",
            "canvas-title-r1",
        ),
        resource_state="available",
        resource=RasterResourceRef("resource:capture-title", "bytes-r1"),
        category_assignments=(
            CategoryAssignment(
                "cover",
                "suggested",
                "category-machine-r1",
                provenance=_machine_provenance(),
            ),
            CategoryAssignment("title_page", "manual", "category-human-r2"),
        ),
        caption_assertions=(
            CaptionAssertion(
                "Machine title",
                "machine",
                "caption-machine-r1",
                provenance=_machine_provenance(),
            ),
            CaptionAssertion(
                "Reviewed title",
                "manual",
                "caption-human-r2",
                language="en",
            ),
        ),
    )
    annotation = SpatialAnnotationView(
        key=SpatialAnnotationKey("gate-book", "mistral-region-1"),
        revision="mistral-region-r1",
        source=SpatialSourceRef(
            "capture",
            "capture-representation-r1",
            "canvas-title",
            "canvas-title-r1",
        ),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            "canvas-title-r1",
            (
                NormalizedPoint(0.10, 0.15),
                NormalizedPoint(0.85, 0.15),
                NormalizedPoint(0.85, 0.80),
                NormalizedPoint(0.10, 0.80),
            ),
        ),
        role_assignments=(
            SpatialRoleAssignment(
                "marginalia",
                "machine",
                "role-machine-r1",
                provenance=_machine_provenance(),
            ),
            SpatialRoleAssignment("figure", "manual", "role-human-r2"),
        ),
        caption_assertions=(
            CaptionAssertion(
                "Machine region",
                "machine",
                "region-caption-machine-r1",
                provenance=_machine_provenance(),
            ),
            CaptionAssertion(
                "Reviewed illustration",
                "manual",
                "region-caption-human-r2",
                language="en",
            ),
        ),
    )
    return CorrectionSourceSnapshot(
        artifact=artifact,
        source_revision="bytes-r1",
        content=content,
        annotations=(annotation,),
        human_text_assertions=(
            HumanTextAssertion(
                "verified-text-1",
                "verified-text-r1",
                "Verified transcription",
                "verified",
                "en",
            ),
        ),
    )


def _aggregate(source: CorrectionSourceSnapshot) -> CorrectionAggregateSnapshot:
    artifact = ArtifactCorrectionSnapshot(
        key=source.artifact.key,
        revision=source.artifact.revision,
        category_assignments=source.artifact.category_assignments,
        caption_assertions=source.artifact.caption_assertions,
    )
    annotation = source.annotations[0]
    return CorrectionAggregateSnapshot(
        item_id=source.artifact.key.item_id,
        revision="aggregate-r1",
        artifacts=(artifact,),
        annotations=(
            AnnotationCorrectionSnapshot(
                key=annotation.key,
                revision=annotation.revision,
                linked_artifact_id=source.artifact.key.artifact_id,
                role_assignments=annotation.role_assignments,
            ),
        ),
        review=CorrectionReviewSnapshot("review-r1"),
    )


def _command(
    source: CorrectionSourceSnapshot,
    operation_id: str = "gate-transform-op",
) -> CorrectionTransformCommand:
    return CorrectionTransformCommand(
        item_id=source.artifact.key.item_id,
        artifact_id=source.artifact.key.artifact_id,
        artifact_revision=source.artifact.revision,
        source_revision=source.source_revision,
        source_sha256=source.source_sha256,
        quad=FULL_FRAME,
        adjustment=ManualBinaryAdjustRecipe(contrast=100, brightness=24),
        rerun_ocr=True,
        operation_id=operation_id,
    )


def _correction_repository(
    write_set: RecoverableWriteSet,
    initial: CorrectionAggregateSnapshot,
    revisions: _Revisions,
    authority_lock: threading.RLock,
) -> FilesystemCorrectionRepository:
    return FilesystemCorrectionRepository(
        write_set,
        load_aggregate=(
            lambda item_id: initial if item_id == initial.item_id else None
        ),
        revision_factory=revisions,
        clock=lambda: "2026-07-23T12:00:00Z",
        lock_context_for=lambda: authority_lock,
        recover=False,
    )


def _read_aggregate(
    repository: FilesystemCorrectionRepository,
    operation_id: str,
) -> CorrectionAggregateSnapshot:
    with repository.unit_of_work(operation_id=operation_id) as unit:
        aggregate = unit.get("gate-book")
    assert aggregate is not None
    return aggregate


def test_window_reopen_and_review_transitions_do_not_own_running_transform(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    source = _source()
    original_bytes = source.content
    initial = _aggregate(source)
    revisions = _Revisions()
    authority_lock = threading.RLock()
    write_set = RecoverableWriteSet(root)

    first_window_repository = _correction_repository(
        write_set,
        initial,
        revisions,
        authority_lock,
    )
    first_window = CorrectionService(first_window_repository)
    first_window.mark_attention(
        MarkAttentionCommand(
            "gate-book",
            "review-r1",
            "Caption and image need review",
            "curator-local",
            "gate-mark-op",
        )
    )

    durable_transform_store = FilesystemCorrectionTransformStore(
        write_set,
        source_snapshot_for=_SourceAuthority(source),
        lock_context_for=lambda: authority_lock,
        recover=False,
    )
    blocking_store = _BlockingRecordingStore(durable_transform_store)
    jobs = JobManager(checkpoint_interval=0)
    worker = CorrectionTransformWorker(
        jobs,
        blocking_store,
        ocr=_FailingOcr(),
    )
    transforms = CorrectionTransformService(jobs, executor=worker.run)
    command = _command(source)
    queued = transforms.queue(command)
    duplicate = transforms.queue(
        CorrectionTransformCommand.from_dict(command.as_dict())
    )
    assert queued.created is True
    assert duplicate.created is False
    assert duplicate.job_id == queued.job_id

    results: list[CorrectionTransformRunResult] = []
    failures: list[BaseException] = []

    def run_transform() -> None:
        try:
            results.append(transforms.execute_queued(command))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=run_transform, daemon=True)
    thread.start()
    assert blocking_store.commit_started.wait(5)
    assert jobs.view(queued.job_id).state.value == "running"

    # The first renderer/window may disappear here.  A fresh repository and
    # service load the durable review, while the host-owned job remains live.
    del first_window
    reopened_repository = _correction_repository(
        write_set,
        initial,
        revisions,
        authority_lock,
    )
    reopened_window = CorrectionService(reopened_repository)
    attention = _read_aggregate(reopened_repository, "gate-review-read-1").review
    reopened_window.resolve(
        ResolveCorrectionsCommand(
            "gate-book",
            attention.revision,
            "curator-local",
            "gate-resolve-op",
            "Image and caption checked",
        )
    )
    resolved = _read_aggregate(reopened_repository, "gate-review-read-2").review
    reopened_window.reopen(
        ReopenCorrectionsCommand(
            "gate-book",
            resolved.revision,
            "curator-local",
            "gate-reopen-op",
            "OCR proposal needs another look",
        )
    )

    blocking_store.release_commit.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1

    result = results[0]
    assert result.image_commit is not None
    assert result.ocr_followup.state is OcrFollowupState.FAILED
    assert result.ocr_followup.failure is not None
    assert result.ocr_followup.failure.code == "ocr_followup_failed"
    assert jobs.view(queued.job_id).state.value == "done"
    assert jobs.view(queued.job_id).note == "image committed; OCR follow-up failed"

    final = _read_aggregate(reopened_repository, "gate-review-read-3")
    assert final.review.state.value == "needs_attention"
    assert [event.action for event in final.review.history] == [
        "attention.mark",
        "attention.resolve",
        "attention.reopen",
    ]
    assert (
        final.artifact("capture-title").caption(CaptionOrigin.MANUAL).text
        == "Reviewed title"
    )
    assert (
        final.annotation("mistral-region-1")
        .role(RoleAssignmentOrigin.MANUAL)
        .role
        == "figure"
    )

    human = blocking_store.draft.human_assertions
    assert [value.category for value in human.artifact_categories] == ["title_page"]
    assert [value.text for value in human.artifact_captions] == ["Reviewed title"]
    assert [value.role for value in human.spatial[0].roles] == ["figure"]
    assert [value.text for value in human.spatial[0].captions] == [
        "Reviewed illustration"
    ]
    assert [value.text for value in human.text] == ["Verified transcription"]
    assert source.content == original_bytes


def test_cancel_and_process_restart_require_a_new_operation_and_keep_original(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    source = _source()
    original_digest = hashlib.sha256(source.content).hexdigest()
    authority_lock = threading.RLock()
    write_set = RecoverableWriteSet(root)
    transform_store = FilesystemCorrectionTransformStore(
        write_set,
        source_snapshot_for=_SourceAuthority(source),
        lock_context_for=lambda: authority_lock,
        recover=False,
    )
    history = FilesystemJobHistoryRepository(root / ".engine" / "jobs.json")

    first_jobs = JobManager(history, checkpoint_interval=0)
    first_worker = CorrectionTransformWorker(first_jobs, transform_store)
    first_service = CorrectionTransformService(
        first_jobs,
        executor=first_worker.run,
    )
    cancelled_command = _command(source, "gate-cancel-op")
    cancelled_queue = first_service.queue(cancelled_command)
    first_jobs.request_cancel(cancelled_queue.job_id)
    cancelled = first_service.execute_queued(cancelled_command)
    assert cancelled.cancelled_before_commit is True
    assert cancelled.image_commit is None
    assert first_jobs.view(cancelled_queue.job_id).state.value == "cancelled"

    after_cancel = JobManager(history, checkpoint_interval=0)
    after_cancel.rehydrate(strict=True)
    after_cancel_service = CorrectionTransformService(after_cancel)
    cancelled_replay = after_cancel_service.queue(cancelled_command)
    assert cancelled_replay.created is False
    assert cancelled_replay.job.state.value == "cancelled"

    interrupted_command = replace(
        cancelled_command,
        operation_id="gate-interrupted-op",
    )
    interrupted_queue = after_cancel_service.queue(interrupted_command)
    assert interrupted_queue.job.state.value == "queued"

    after_restart = JobManager(history, checkpoint_interval=0)
    after_restart.rehydrate(strict=True)
    after_restart_worker = CorrectionTransformWorker(
        after_restart,
        transform_store,
    )
    after_restart_service = CorrectionTransformService(
        after_restart,
        executor=after_restart_worker.run,
    )
    interrupted_replay = after_restart_service.queue(interrupted_command)
    assert interrupted_replay.created is False
    assert interrupted_replay.job.state.value == "interrupted"

    restarted_command = replace(
        cancelled_command,
        operation_id="gate-restarted-op",
        rerun_ocr=False,
    )
    restarted_queue = after_restart_service.queue(restarted_command)
    restarted = after_restart_service.execute_queued(restarted_command)
    assert restarted_queue.created is True
    assert restarted.image_commit is not None
    assert restarted.cancelled_before_commit is False
    assert restarted.ocr_followup.state is OcrFollowupState.NOT_REQUESTED
    assert all(
        output.artifact_id != source.artifact.key.artifact_id
        for output in restarted.image_commit.outputs
    )
    assert hashlib.sha256(source.content).hexdigest() == original_digest

    recovered_store = FilesystemCorrectionTransformStore(
        RecoverableWriteSet(root),
        source_snapshot_for=_SourceAuthority(source),
        lock_context_for=lambda: authority_lock,
    )
    recovered_original = recovered_store.load_source(source.artifact.key)
    assert recovered_original.artifact.revision == source.artifact.revision
    assert recovered_original.source_revision == source.source_revision
    assert hashlib.sha256(recovered_original.content).hexdigest() == original_digest
