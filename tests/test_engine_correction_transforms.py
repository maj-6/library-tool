from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import FrozenInstanceError, replace

import pytest
from PIL import Image

from librarytool.engine.correction_transforms import (
    CORRECTION_OUTPUT_KINDS,
    CommittedCorrectionOutput,
    CorrectionSourceSnapshot,
    CorrectionTransformCommand,
    CorrectionTransformCommitDraft,
    CorrectionTransformCommitResult,
    CorrectionTransformHooksPort,
    CorrectionTransformService,
    CorrectionTransformStorePort,
    CorrectionTransformWorker,
    HumanTextAssertion,
    OcrFollowupOutcome,
    OcrFollowupOutcomePort,
    OcrFollowupPort,
    OcrFollowupState,
)
from librarytool.engine.errors import ConflictError, ValidationError
from librarytool.engine.jobs import JobManager, JobProgress
from librarytool.engine.raster_artifacts import (
    CaptionAssertion,
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
    SpatialAnnotationKey,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
)
from librarytool.processing.raster import ManualBinaryAdjustRecipe


FULL_FRAME = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def _source_bytes(width: int = 100, height: int = 80) -> bytes:
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (x * 2, y * 3, (x + y) % 256)
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=False, compress_level=9)
    return output.getvalue()


def _artifact(content: bytes) -> RasterArtifactView:
    return RasterArtifactView(
        key=RasterArtifactKey("book-1", "source-image"),
        revision="artifact-r1",
        kind="captured-image",
        media_type="image/png",
        content_sha256=hashlib.sha256(content).hexdigest(),
        dimensions=RasterDimensions(100, 80),
        source=RasterSourceRef("capture", "representation-r1", "canvas-1", "canvas-r1"),
        resource_state="available",
        resource=RasterResourceRef("resource:source-image", "bytes-r1"),
        category_assignments=(
            CategoryAssignment("cover", "suggested", "category-machine-r1"),
            CategoryAssignment("title_page", "manual", "category-human-r2"),
        ),
        caption_assertions=(
            CaptionAssertion("Machine caption", "machine", "caption-machine-r1"),
            CaptionAssertion("Human caption", "manual", "caption-human-r2"),
        ),
    )


def _annotation(
    annotation_id: str = "region-1",
    *,
    points: tuple[tuple[float, float], ...] = (
        (0.2, 0.2),
        (0.4, 0.2),
        (0.4, 0.4),
        (0.2, 0.4),
    ),
) -> SpatialAnnotationView:
    return SpatialAnnotationView(
        key=SpatialAnnotationKey("book-1", annotation_id),
        revision=f"{annotation_id}-r1",
        source=SpatialSourceRef("capture", "representation-r1", "canvas-1", "canvas-r1"),
        selector=NormalizedPolygonSelector(
            "canvas-normalized",
            "canvas-r1",
            tuple(NormalizedPoint(x, y) for x, y in points),
        ),
        role_assignments=(
            SpatialRoleAssignment("figure", "machine", "role-machine-r1"),
            SpatialRoleAssignment("marginalia", "manual", "role-human-r2"),
        ),
        caption_assertions=(
            CaptionAssertion("Machine region caption", "machine", "region-caption-machine-r1"),
            CaptionAssertion("Human region caption", "manual", "region-caption-human-r2"),
        ),
    )


def _source(*, annotations=None) -> CorrectionSourceSnapshot:
    content = _source_bytes()
    return CorrectionSourceSnapshot(
        artifact=_artifact(content),
        source_revision="bytes-r1",
        content=content,
        annotations=tuple(annotations if annotations is not None else (_annotation(),)),
        human_text_assertions=(
            HumanTextAssertion(
                "text-human-1",
                "text-r3",
                "Verified transcription",
                "verified",
                "en",
            ),
        ),
    )


def _command(source: CorrectionSourceSnapshot | None = None, **changes) -> CorrectionTransformCommand:
    source = source or _source()
    values = {
        "item_id": "book-1",
        "artifact_id": "source-image",
        "artifact_revision": "artifact-r1",
        "source_revision": "bytes-r1",
        "source_sha256": source.source_sha256,
        "quad": FULL_FRAME,
        "adjustment": ManualBinaryAdjustRecipe(contrast=100, brightness=7),
        "rerun_ocr": False,
        "operation_id": "correction-op-1",
    }
    values.update(changes)
    return CorrectionTransformCommand(**values)


class MemoryStore:
    def __init__(self, source: CorrectionSourceSnapshot) -> None:
        self.source = source
        self.loads = 0
        self.commits: list[CorrectionTransformCommitDraft] = []
        self.stale_on_second_load = False
        self.assertions_stale_on_second_load = False
        self.stale_during_commit = False
        self.by_operation: dict[str, tuple[str, CorrectionTransformCommitResult]] = {}

    def load_source(self, key):
        assert key == self.source.artifact.key
        self.loads += 1
        if self.stale_on_second_load and self.loads >= 2:
            return replace(
                self.source,
                artifact=replace(self.source.artifact, revision="artifact-r2"),
            )
        if self.assertions_stale_on_second_load and self.loads >= 2:
            return replace(
                self.source,
                annotations=(
                    replace(self.source.annotations[0], revision="region-1-r2"),
                    *self.source.annotations[1:],
                ),
            )
        return self.source

    def commit_transform(self, draft):
        if self.stale_during_commit:
            raise ConflictError(
                "source changed inside commit",
                code="correction_source_stale",
            )
        existing = self.by_operation.get(draft.command.operation_id)
        if existing is not None:
            fingerprint, result = existing
            if fingerprint != draft.command.fingerprint:
                raise ConflictError(
                    "operation reused",
                    code="correction_operation_conflict",
                )
            return result
        assert draft.command.artifact_revision == self.source.artifact.revision
        assert draft.command.source_revision == self.source.source_revision
        assert draft.command.source_sha256 == self.source.source_sha256
        assert (
            draft.source.dependent_revision_pins
            == self.source.dependent_revision_pins
        )
        self.commits.append(draft)
        outputs = tuple(
            CommittedCorrectionOutput(
                output.kind,
                f"artifact-{output.kind}",
                "output-r1",
                output.content_sha256,
            )
            for output in draft.outputs
        )
        result = CorrectionTransformCommitResult(draft.command.operation_id, outputs)
        self.by_operation[draft.command.operation_id] = (draft.command.fingerprint, result)
        return result


class InvalidOutputIdentityStore(MemoryStore):
    def commit_transform(self, draft):
        result = super().commit_transform(draft)
        outputs = list(result.outputs)
        outputs[0] = replace(outputs[0], artifact_id=draft.command.artifact_id)
        return CorrectionTransformCommitResult(result.operation_id, outputs)


class OutcomeRecorder:
    def __init__(self) -> None:
        self.values: list[tuple[str, OcrFollowupOutcome]] = []

    def record_ocr_followup(self, operation_id, outcome):
        self.values.append((operation_id, outcome))


class SuccessfulOcr:
    def __init__(self) -> None:
        self.requests = []

    def run_ocr_followup(self, request, hooks):
        self.requests.append(request)
        return OcrFollowupOutcome(
            OcrFollowupState.SUCCEEDED,
            source=request.source,
            proposal_ref="ocr-proposal-1",
        )


class FailingOcr:
    def run_ocr_followup(self, request, hooks):
        raise RuntimeError("provider unavailable")


class ObservingHooks:
    def __init__(self, *, cancel_at: str = "") -> None:
        self.cancel_at = cancel_at
        self.cancelled = False
        self.progress: list[JobProgress] = []

    def is_cancelled(self):
        return self.cancelled

    def report_progress(self, progress):
        self.progress.append(progress)
        if progress.phase == self.cancel_at:
            self.cancelled = True


def _queued(
    source: CorrectionSourceSnapshot,
    command: CorrectionTransformCommand,
    **worker_options,
):
    jobs = JobManager(checkpoint_interval=0)
    queue = CorrectionTransformService(jobs).queue(command)
    store = MemoryStore(source)
    worker = CorrectionTransformWorker(jobs, store, **worker_options)
    return jobs, queue, store, worker


def test_command_round_trips_as_canonical_immutable_json() -> None:
    command = _command()

    restored = CorrectionTransformCommand.from_dict(json.loads(command.serialized))

    assert restored == command
    assert restored.fingerprint == command.fingerprint
    assert restored.key == RasterArtifactKey("book-1", "source-image")
    assert restored.as_dict()["adjustment"]["threshold"] == command.adjustment.threshold
    with pytest.raises(FrozenInstanceError):
        command.operation_id = "different"


def test_adjustment_and_ocr_are_optional_command_fields() -> None:
    source = _source()

    command = CorrectionTransformCommand(
        item_id="book-1",
        artifact_id="source-image",
        artifact_revision="artifact-r1",
        source_revision="bytes-r1",
        source_sha256=source.source_sha256,
        quad=FULL_FRAME,
        operation_id="defaults-op",
    )

    assert command.adjustment is None
    assert command.rerun_ocr is False


def test_command_rejects_bad_checksum_quad_boolean_and_tampered_recipe() -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        _command(source_sha256="bad")
    with pytest.raises(ValidationError) as quad:
        _command(quad=((0, 0), (1, 1), (1, 0), (0, 1)))
    assert quad.value.code == "invalid_correction_quad"
    with pytest.raises(ValidationError, match="boolean"):
        _command(rerun_ocr=1)

    payload = _command().as_dict()
    payload["adjustment"]["threshold"] += 1
    with pytest.raises(ValidationError, match="threshold"):
        CorrectionTransformCommand.from_dict(payload)
    payload = _command().as_dict()
    payload["adjustment"]["schema"] = "legacy-colour-normalization"
    with pytest.raises(ValidationError, match="unsupported manual binary"):
        CorrectionTransformCommand.from_dict(payload)
    payload = _command().as_dict()
    payload["adjustment"]["algorithm"] = "different-threshold-semantics"
    with pytest.raises(ValidationError, match="canonical recipe") as algorithm:
        CorrectionTransformCommand.from_dict(payload)
    assert algorithm.value.details["mismatched"] == ["algorithm"]
    payload = _command(adjustment=ManualBinaryAdjustRecipe(brightness=99)).as_dict()
    assert payload["adjustment"]["threshold"] == 1
    payload["adjustment"]["threshold"] = True
    with pytest.raises(ValidationError, match="canonical recipe") as threshold_type:
        CorrectionTransformCommand.from_dict(payload)
    assert threshold_type.value.details["mismatched"] == ["threshold"]


def test_command_codec_rejects_unknown_fields_and_boolean_versions() -> None:
    payload = _command().as_dict()
    payload["future"] = "silently changing the fingerprint is unsafe"
    with pytest.raises(ValidationError) as unknown:
        CorrectionTransformCommand.from_dict(payload)
    assert unknown.value.details["unknown"] == ["future"]

    payload = _command().as_dict()
    payload["version"] = True
    with pytest.raises(ValidationError, match="unsupported correction"):
        CorrectionTransformCommand.from_dict(payload)

    payload = _command().as_dict()
    payload["adjustment"]["future"] = 1
    with pytest.raises(ValidationError) as nested:
        CorrectionTransformCommand.from_dict(payload)
    assert nested.value.details["unknown"] == ["future"]


def test_queue_is_idempotent_and_operation_reuse_conflicts() -> None:
    source = _source()
    command = _command(source)
    jobs = JobManager()
    service = CorrectionTransformService(jobs)

    first = service.queue(command)
    replay = service.queue(CorrectionTransformCommand.from_dict(command.as_dict()))

    assert first.created is True
    assert replay.created is False
    assert replay.job_id == first.job_id
    assert len(jobs.list()) == 1
    assert first.job.subject.item_id == "book-1"
    assert first.job.subject.source_id == "source-image"
    with pytest.raises(ConflictError) as conflict:
        service.queue(_command(source, adjustment=None))
    assert conflict.value.code == "correction_operation_conflict"


def test_service_executes_a_queued_command_through_its_injected_runner() -> None:
    source = _source()
    command = _command(source)
    jobs = JobManager(checkpoint_interval=0)
    store = MemoryStore(source)
    worker = CorrectionTransformWorker(jobs, store)
    service = CorrectionTransformService(jobs, executor=worker.run)
    queued = service.queue(command)

    result = service.execute_queued(command)

    assert service.executable is True
    assert result.job_id == queued.job_id
    assert jobs.view(queued.job_id).state.value == "done"
    assert len(store.commits) == 1


def test_queue_only_service_reports_that_no_executor_was_composed() -> None:
    service = CorrectionTransformService(JobManager())
    command = _command()
    service.queue(command)

    assert service.executable is False
    with pytest.raises(RuntimeError, match="executor is unavailable"):
        service.execute_queued(command)


def test_queue_rejects_an_unrelated_job_at_the_deterministic_identity() -> None:
    command = _command()
    jobs = JobManager()
    service = CorrectionTransformService(jobs)
    jobs.track(
        {
            "id": service.job_id_for(command.operation_id),
            "kind": "different.job",
            "status": "queued",
            "subject": {
                "item_id": command.item_id,
                "source_id": command.artifact_id,
            },
            "input_revisions": {
                "operation_id": command.operation_id,
                "command_sha256": command.fingerprint,
            },
        },
        "different.job",
    )

    with pytest.raises(ConflictError) as conflict:
        service.queue(command)

    assert conflict.value.code == "correction_operation_conflict"


def test_concurrent_queue_calls_create_one_job() -> None:
    command = _command()
    jobs = JobManager()
    service = CorrectionTransformService(jobs)
    barrier = threading.Barrier(8)
    results = []

    def queue() -> None:
        barrier.wait()
        results.append(service.queue(command))

    workers = [threading.Thread(target=queue) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert sum(value.created for value in results) == 1
    assert {value.job_id for value in results} == {results[0].job_id}
    assert len(jobs.list()) == 1


def test_terminal_job_cannot_be_executed_twice() -> None:
    source = _source()
    command = _command(source)
    _, _, store, worker = _queued(source, command)

    worker.run(command)
    with pytest.raises(ConflictError) as conflict:
        worker.run(command)

    assert conflict.value.code == "correction_job_already_claimed"
    assert len(store.commits) == 1


def test_worker_builds_four_immutable_outputs_and_maps_clipped_polygons() -> None:
    source = _source(
        annotations=(
            _annotation(),
            _annotation(
                "outside-region",
                points=((0.0, 0.0), (0.1, 0.0), (0.1, 0.1), (0.0, 0.1)),
            ),
        )
    )
    command = _command(
        source,
        quad=((0.25, 0.25), (0.75, 0.25), (0.75, 0.75), (0.25, 0.75)),
    )
    source_before = source.content
    jobs, queue, store, worker = _queued(source, command)

    result = worker.run(command)

    assert result.job_id == queue.job_id
    assert result.ocr_followup.state is OcrFollowupState.NOT_REQUESTED
    assert source.content == source_before
    assert len(store.commits) == 1
    draft = store.commits[0]
    assert tuple(output.kind for output in draft.outputs) == CORRECTION_OUTPUT_KINDS
    assert all(isinstance(output.content, bytes) for output in draft.outputs)
    assert draft.output("corrected-display").content_sha256 == (
        draft.output("ocr-ready").content_sha256
    )
    assert max(
        draft.output("thumbnail").dimensions.width,
        draft.output("thumbnail").dimensions.height,
    ) <= 512
    assert draft.dropped_annotation_ids == ("outside-region",)
    mapped = draft.mapped_annotations[0]
    assert mapped.annotation_id == "region-1"
    assert min(point.x for point in mapped.points) == 0
    assert min(point.y for point in mapped.points) == 0
    assert max(point.x for point in mapped.points) == pytest.approx(0.3)
    assert max(point.y for point in mapped.points) == pytest.approx(0.3)
    manifest = json.loads(draft.output("transform-manifest").content)
    assert manifest["annotation_mapping"]["mapped"] == 1
    assert manifest["ocr_publication_policy"] == "machine-proposal-only"
    assert manifest["dependent_revision_pins"] == source.dependent_revision_pins
    job = jobs.get(queue.job_id)
    assert job["state"] == "done"
    assert len(job["outputs"]) == 4
    assert (
        job["input_revisions"]["dependent_assertions"]
        == source.dependent_revision_pins
    )


def test_projective_mapping_uses_the_published_normalized_homography() -> None:
    quad = ((0.2, 0.1), (0.8, 0.2), (0.9, 0.85), (0.1, 0.75))
    source = _source(
        annotations=(
            _annotation("skewed-page", points=quad),
        )
    )
    command = _command(source, quad=quad)
    _, _, store, worker = _queued(source, command)

    worker.run(command)

    mapped = store.commits[0].mapped_annotations[0]
    assert mapped.annotation_id == "skewed-page"
    assert len(mapped.points) == 4
    for point, expected in zip(
        mapped.points,
        ((0, 0), (1, 0), (1, 1), (0, 1)),
        strict=True,
    ):
        assert point.x == pytest.approx(expected[0], abs=1e-10)
        assert point.y == pytest.approx(expected[1], abs=1e-10)


def test_commit_containers_defensively_freeze_outputs_and_revision_pins() -> None:
    source = _source()
    command = _command(source)
    _, _, store, worker = _queued(source, command)

    run_result = worker.run(command)
    draft = store.commits[0]
    rebuilt_draft = CorrectionTransformCommitDraft(
        draft.command,
        draft.source,
        list(draft.outputs),
        list(draft.mapped_annotations),
        list(draft.dropped_annotation_ids),
        draft.human_assertions,
    )
    rebuilt_commit = CorrectionTransformCommitResult(
        run_result.image_commit.operation_id,
        list(run_result.image_commit.outputs),
    )
    pins = source.dependent_revision_pins
    pins["spatial_annotations"].clear()

    assert isinstance(rebuilt_draft.outputs, tuple)
    assert isinstance(rebuilt_draft.mapped_annotations, tuple)
    assert isinstance(rebuilt_draft.dropped_annotation_ids, tuple)
    assert isinstance(rebuilt_commit.outputs, tuple)
    assert source.dependent_revision_pins["spatial_annotations"] == [
        {"annotation_id": "region-1", "revision": "region-1-r1"}
    ]


def test_worker_rejects_a_store_that_reuses_the_source_artifact_identity() -> None:
    source = _source()
    command = _command(source)
    jobs = JobManager()
    queue = CorrectionTransformService(jobs).queue(command)
    store = InvalidOutputIdentityStore(source)
    worker = CorrectionTransformWorker(jobs, store)

    with pytest.raises(ConflictError) as conflict:
        worker.run(command)

    assert conflict.value.code == "correction_commit_mismatch"
    assert jobs.view(queue.job_id).state.value == "failed"
    assert jobs.view(queue.job_id).outputs == ()


def test_human_roles_captions_categories_and_text_are_carried_separately() -> None:
    source = _source()
    command = _command(source)
    _, _, store, worker = _queued(source, command)

    worker.run(command)

    human = store.commits[0].human_assertions
    assert [value.category for value in human.artifact_categories] == ["title_page"]
    assert [value.text for value in human.artifact_captions] == ["Human caption"]
    assert [value.role for value in human.spatial[0].roles] == ["marginalia"]
    assert [value.text for value in human.spatial[0].captions] == [
        "Human region caption"
    ]
    assert [value.text for value in human.text] == ["Verified transcription"]
    # Mapped geometry can carry machine evidence too, but it is not allowed
    # into the separately protected human assertions.
    mapped = store.commits[0].mapped_annotations[0]
    assert {value.origin.value for value in mapped.role_assignments} == {
        "manual",
        "machine",
    }


@pytest.mark.parametrize("stale_point", ("reload", "commit"))
def test_stale_source_conflicts_before_atomic_publication(stale_point: str) -> None:
    source = _source()
    command = _command(source)
    jobs, queue, store, worker = _queued(source, command)
    store.stale_on_second_load = stale_point == "reload"
    store.stale_during_commit = stale_point == "commit"

    with pytest.raises(ConflictError) as conflict:
        worker.run(command)

    assert conflict.value.code == "correction_source_stale"
    assert store.commits == []
    job = jobs.view(queue.job_id)
    assert job.state.value == "failed"
    assert job.error.code == "correction_source_stale"
    assert job.outputs == ()


def test_initial_stale_source_pin_fails_before_transform_or_commit() -> None:
    source = _source()
    command = _command(source, artifact_revision="artifact-older")
    jobs, queue, store, worker = _queued(source, command)

    with pytest.raises(ConflictError) as conflict:
        worker.run(command)

    assert conflict.value.code == "correction_source_stale"
    assert store.loads == 1
    assert store.commits == []
    assert jobs.view(queue.job_id).error.code == "correction_source_stale"


def test_concurrent_assertion_revision_conflicts_before_publication() -> None:
    source = _source()
    command = _command(source)
    jobs, queue, store, worker = _queued(source, command)
    store.assertions_stale_on_second_load = True

    with pytest.raises(ConflictError) as conflict:
        worker.run(command)

    assert conflict.value.code == "correction_assertions_stale"
    assert store.commits == []
    job = jobs.view(queue.job_id)
    assert job.state.value == "failed"
    assert job.error.code == "correction_assertions_stale"
    assert job.outputs == ()


def test_prequeued_cancellation_reads_and_commits_nothing() -> None:
    source = _source()
    command = _command(source)
    jobs, queue, store, worker = _queued(source, command)
    jobs.request_cancel(queue.job_id)

    result = worker.run(command)

    assert result.cancelled_before_commit is True
    assert result.image_commit is None
    assert store.loads == 0
    assert store.commits == []
    assert jobs.view(queue.job_id).state.value == "cancelled"


def test_progress_hook_can_cancel_after_render_without_publication() -> None:
    source = _source()
    command = _command(source)
    jobs, queue, store, worker = _queued(source, command)
    hooks = ObservingHooks(cancel_at="transforming")

    result = worker.run(command, hooks=hooks)

    assert result.cancelled_before_commit is True
    assert store.loads == 1
    assert store.commits == []
    assert [value.phase for value in hooks.progress][:2] == [
        "validating-source",
        "transforming",
    ]
    assert jobs.view(queue.job_id).state.value == "cancelled"


def test_successful_ocr_is_an_exact_rendition_pinned_machine_proposal() -> None:
    source = _source()
    command = _command(source, rerun_ocr=True)
    ocr = SuccessfulOcr()
    recorder = OutcomeRecorder()
    jobs, queue, store, worker = _queued(
        source,
        command,
        ocr=ocr,
        ocr_outcomes=recorder,
    )

    result = worker.run(command)

    assert result.ocr_followup.state is OcrFollowupState.SUCCEEDED
    assert result.ocr_followup.proposal_ref == "ocr-proposal-1"
    assert ocr.requests[0].source == result.image_commit.output("ocr-ready")
    assert ocr.requests[0].as_dict()["publication_policy"] == "machine-proposal-only"
    assert "human_assertions" not in ocr.requests[0].as_dict()
    assert recorder.values == [(command.operation_id, result.ocr_followup)]
    job = jobs.get(queue.job_id)
    assert job["state"] == "done"
    assert job["outputs"][-1] == {
        "kind": "ocr-proposal",
        "ref": "ocr-proposal-1",
        "partial": False,
    }


def test_ocr_failure_is_observable_and_does_not_roll_back_image_commit() -> None:
    source = _source()
    command = _command(source, rerun_ocr=True)
    recorder = OutcomeRecorder()
    jobs, queue, store, worker = _queued(
        source,
        command,
        ocr=FailingOcr(),
        ocr_outcomes=recorder,
    )

    result = worker.run(command)

    assert len(store.commits) == 1
    assert result.image_commit is not None
    assert result.ocr_followup.state is OcrFollowupState.FAILED
    assert result.ocr_followup.failure.code == "ocr_followup_failed"
    assert recorder.values[0][1] == result.ocr_followup
    job = jobs.get(queue.job_id)
    assert job["state"] == "done"
    assert job["errors"] == 1
    assert len(job["outputs"]) == 4
    assert store.commits[0].human_assertions.text[0].text == "Verified transcription"


def test_ports_remain_runtime_checkable_and_framework_neutral() -> None:
    source = _source()
    assert isinstance(MemoryStore(source), CorrectionTransformStorePort)
    assert isinstance(OutcomeRecorder(), OcrFollowupOutcomePort)
    assert isinstance(SuccessfulOcr(), OcrFollowupPort)
    assert isinstance(ObservingHooks(), CorrectionTransformHooksPort)
