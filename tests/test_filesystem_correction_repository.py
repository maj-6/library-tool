"""Durable filesystem boundary for revisioned correction commands."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.correction_repository import (
    FilesystemCorrectionRepository,
)
from librarytool.adapters.filesystem.recoverable_write_set import (
    RecoverableWriteSet,
)
from librarytool.engine.corrections import (
    AnnotationCorrectionSnapshot,
    ArtifactCorrectionSnapshot,
    ArtifactMetadataAssertion,
    AssertArtifactMetadataCommand,
    AssignImageCategoryCommand,
    AssignRegionRoleCommand,
    ClearImageCategoryCommand,
    ClearManualCaptionCommand,
    ClearRegionRoleCommand,
    CorrectionAggregateSnapshot,
    CorrectionReviewSnapshot,
    CorrectionService,
    MarkAttentionCommand,
    MetadataAssertionOrigin,
    ReopenCorrectionsCommand,
    ResolveCorrectionsCommand,
    SetManualCaptionCommand,
)
from librarytool.engine.errors import ConflictError, RepositoryError
from librarytool.engine.raster_artifacts import (
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    RasterArtifactKey,
)
from librarytool.engine.spatial_annotations import (
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialRoleAssignment,
)


class _Revisions:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, kind: str, target_id: str) -> str:
        self.count += 1
        return f"{kind}-{target_id}-r{self.count}"


def _provenance(origin: str = "machine") -> ArtifactProvenance:
    return ArtifactProvenance(
        origin=origin,
        provider_id="test-provider" if origin == "machine" else "",
        model="test-model" if origin == "machine" else "",
    )


def _aggregate() -> CorrectionAggregateSnapshot:
    source = ArtifactCorrectionSnapshot(
        key=RasterArtifactKey("book-1", "source-1"),
        revision="source-r1",
        category_assignments=(
            CategoryAssignment(
                "cover",
                AssignmentOrigin.SUGGESTED,
                "source-category-r1",
                confidence=0.8,
                provenance=_provenance(),
            ),
        ),
    )
    crop = ArtifactCorrectionSnapshot(
        key=RasterArtifactKey("book-1", "crop-1"),
        revision="crop-r1",
        source_artifact_id="source-1",
        caption_assertions=(
            CaptionAssertion(
                "Machine caption",
                CaptionOrigin.MACHINE,
                "caption-machine-r1",
                provenance=_provenance(),
            ),
        ),
        role_assignments=(
            SpatialRoleAssignment(
                "figure",
                RoleAssignmentOrigin.MACHINE,
                "artifact-role-machine-r1",
                provenance=_provenance(),
            ),
        ),
        metadata_assertions=(
            ArtifactMetadataAssertion(
                "caption_source",
                "ocr",
                MetadataAssertionOrigin.MACHINE,
                "metadata-machine-r1",
                _provenance(),
            ),
        ),
    )
    annotation = AnnotationCorrectionSnapshot(
        key=SpatialAnnotationKey("book-1", "region-1"),
        revision="region-r1",
        linked_artifact_id="crop-1",
        role_assignments=(
            SpatialRoleAssignment(
                "figure",
                RoleAssignmentOrigin.MACHINE,
                "region-role-machine-r1",
                provenance=_provenance(),
            ),
        ),
    )
    return CorrectionAggregateSnapshot(
        item_id="book-1",
        revision="aggregate-r1",
        artifacts=(source, crop),
        annotations=(annotation,),
        review=CorrectionReviewSnapshot("review-r1"),
    )


def _repository(
    root: Path,
    *,
    aggregate: CorrectionAggregateSnapshot | None = None,
    write_set: RecoverableWriteSet | None = None,
    hook=None,
    recover: bool = True,
    revisions: _Revisions | None = None,
    loader=None,
    reconciler=None,
) -> FilesystemCorrectionRepository:
    store = write_set or RecoverableWriteSet(root, publish_hook=hook)
    initial = aggregate or _aggregate()
    return FilesystemCorrectionRepository(
        store,
        load_aggregate=(
            loader
            if loader is not None
            else lambda item_id: initial if item_id == initial.item_id else None
        ),
        reconcile_aggregate=reconciler,
        revision_factory=revisions or _Revisions(),
        clock=lambda: "2026-07-23T12:00:00Z",
        recover=recover,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aggregate_path(root: Path, item_id: str = "book-1") -> Path:
    return (
        root
        / ".engine"
        / "corrections"
        / "aggregates"
        / f"{_digest(item_id)}.json"
    )


def _receipt_path(root: Path, operation_id: str) -> Path:
    return (
        root
        / ".engine"
        / "receipts"
        / "corrections"
        / f"{_digest(operation_id)}.json"
    )


def _state(
    repository: FilesystemCorrectionRepository,
    operation_id: str = "read-state",
) -> CorrectionAggregateSnapshot:
    with repository.unit_of_work(operation_id=operation_id) as unit:
        aggregate = unit.get("book-1")
    assert aggregate is not None
    return aggregate


def _repository_files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).parts[0] != ".transactions"
    }


def test_category_mutation_and_exact_replay_survive_repository_restart(tmp_path):
    root = tmp_path / "library"
    first_repository = _repository(root)
    command = AssignImageCategoryCommand(
        "book-1",
        "source-1",
        "source-r1",
        "title_page",
        "category-op",
    )

    first = CorrectionService(first_repository).assign_category(command)

    assert first.replayed is False
    assert _aggregate_path(root).is_file()
    assert _receipt_path(root, "category-op").is_file()
    assert "book-1" not in _aggregate_path(root).name
    assert "category-op" not in _receipt_path(root, "category-op").name

    def fail_if_loaded(_item_id):
        raise AssertionError("durable correction state should win after restart")

    restarted = _repository(root, loader=fail_if_loaded)
    replay = CorrectionService(restarted).assign_category(command)
    state = _state(restarted)
    source = state.artifact("source-1")

    assert replay.replayed is True
    assert replay.receipt == first.receipt
    assert source.category(AssignmentOrigin.MANUAL).category == "title_page"
    assert source.category(AssignmentOrigin.SUGGESTED).category == "cover"
    assert state.effective_category("crop-1").category == "title_page"
    assert replay.receipt.inverse.action == "category.clear"


def test_explicit_reconciler_refreshes_durable_machine_evidence(tmp_path):
    root = tmp_path / "library"
    original = _aggregate()
    repository = _repository(root, aggregate=original)
    CorrectionService(repository).assign_category(
        AssignImageCategoryCommand(
            "book-1",
            "source-1",
            "source-r1",
            "title_page",
            "category-op",
        )
    )
    live = replace(
        original,
        revision="aggregate-live-r2",
        artifacts=tuple(
            replace(value, revision="source-live-r2")
            if value.key.artifact_id == "source-1"
            else value
            for value in original.artifacts
        ),
    )
    calls = []

    def reconcile(current, durable):
        calls.append((current, durable))
        return replace(
            durable,
            revision="aggregate-reconciled-r3",
            artifacts=tuple(
                replace(value, revision="source-reconciled-r3")
                if value.key.artifact_id == "source-1"
                else value
                for value in durable.artifacts
            ),
        )

    restarted = _repository(
        root,
        loader=lambda item_id: live if item_id == "book-1" else None,
        reconciler=reconcile,
    )

    state = _state(restarted)

    assert calls and calls[0][0] == live
    assert calls[0][1].revision != live.revision
    assert state.revision == "aggregate-reconciled-r3"
    assert state.artifact("source-1").revision == "source-reconciled-r3"
    assert (
        state.artifact("source-1").category(AssignmentOrigin.MANUAL).category
        == "title_page"
    )


def test_linked_region_and_artifact_publish_in_one_recoverable_transaction(tmp_path):
    root = tmp_path / "library"
    published: list[Path] = []

    def trace(_index: int, target: Path) -> None:
        published.append(target)

    repository = _repository(root, hook=trace)
    result = CorrectionService(repository).assign_region_role(
        AssignRegionRoleCommand(
            "book-1",
            "region-1",
            "region-r1",
            "MAR",
            "role-op",
            linked_artifact_id="crop-1",
            expected_linked_artifact_revision="crop-r1",
        )
    )
    state = _state(repository)
    annotation = state.annotation("region-1")
    artifact = state.artifact("crop-1")

    assert annotation.role(RoleAssignmentOrigin.MANUAL).role == "marginalia"
    assert artifact.role(RoleAssignmentOrigin.MANUAL).role == "marginalia"
    assert annotation.role(RoleAssignmentOrigin.MACHINE).role == "figure"
    assert artifact.role(RoleAssignmentOrigin.MACHINE).role == "figure"
    assert {
        (target.kind.value, target.target_id)
        for target in result.receipt.targets
    } == {("annotation", "region-1"), ("artifact", "crop-1")}
    assert published == [
        _receipt_path(root, "role-op"),
        _aggregate_path(root),
    ]


class _SimulatedCrash(BaseException):
    pass


def test_restart_recovers_partial_receipt_and_allows_exact_retry(tmp_path):
    root = tmp_path / "library"

    def crash_before_aggregate(index: int, _target: Path) -> None:
        if index == 1:
            raise _SimulatedCrash("process stopped")

    command = AssignImageCategoryCommand(
        "book-1",
        "source-1",
        "source-r1",
        "spine",
        "crash-op",
    )
    crashing = _repository(root, hook=crash_before_aggregate)
    with pytest.raises(_SimulatedCrash):
        CorrectionService(crashing).assign_category(command)

    assert _receipt_path(root, "crash-op").is_file()
    assert not _aggregate_path(root).exists()

    restarted = _repository(root)
    assert not _receipt_path(root, "crash-op").exists()
    assert not _aggregate_path(root).exists()

    result = CorrectionService(restarted).assign_category(command)
    assert result.replayed is False
    assert _state(restarted).artifact("source-1").category(
        AssignmentOrigin.MANUAL
    ).category == "spine"


def test_stale_revision_conflict_performs_no_repository_write(tmp_path):
    root = tmp_path / "library"
    revisions = _Revisions()
    repository = _repository(root, revisions=revisions)
    before = _repository_files(root)

    with pytest.raises(ConflictError) as caught:
        CorrectionService(repository).assign_category(
            AssignImageCategoryCommand(
                "book-1",
                "source-1",
                "stale-r1",
                "cover",
                "stale-op",
            )
        )

    assert caught.value.code == "artifact_revision_conflict"
    assert _repository_files(root) == before
    assert revisions.count == 0
    assert not _receipt_path(root, "stale-op").exists()
    assert not _aggregate_path(root).exists()


def test_caption_and_metadata_preserve_machine_assertions_after_restart(tmp_path):
    root = tmp_path / "library"
    repository = _repository(root)
    service = CorrectionService(repository)
    service.set_manual_caption(
        SetManualCaptionCommand(
            "book-1",
            "crop-1",
            "crop-r1",
            "Corrected caption",
            "caption-op",
            language="en",
        )
    )
    state = _state(repository)
    service.assert_artifact_metadata(
        AssertArtifactMetadataCommand(
            "book-1",
            "crop-1",
            state.artifact("crop-1").revision,
            "metadata-op",
            assertions={"caption_source": "manual", "plate_number": 7},
        )
    )

    restarted = _repository(
        root,
        loader=lambda _item_id: (_ for _ in ()).throw(
            AssertionError("durable aggregate should be loaded")
        ),
    )
    artifact = _state(restarted).artifact("crop-1")

    assert artifact.caption(CaptionOrigin.MACHINE).text == "Machine caption"
    assert artifact.caption(CaptionOrigin.MANUAL).text == "Corrected caption"
    assert artifact.metadata(
        "caption_source",
        MetadataAssertionOrigin.MACHINE,
    ).value == "ocr"
    assert artifact.effective_metadata() == {
        "caption_source": "manual",
        "plate_number": 7,
    }


def test_attention_history_and_inverse_data_are_immutable_and_durable(tmp_path):
    root = tmp_path / "library"
    repository = _repository(root)
    service = CorrectionService(repository)
    marked = service.mark_attention(
        MarkAttentionCommand(
            "book-1",
            "review-r1",
            "caption is uncertain",
            "reviewer-1",
            "attention-op",
            comment="Compare against the scan.",
        )
    )
    state = _state(repository)
    resolved = service.resolve(
        ResolveCorrectionsCommand(
            "book-1",
            state.review.revision,
            "reviewer-1",
            "resolve-op",
            comment="Verified.",
        )
    )

    restarted = _repository(root)
    state = _state(restarted)
    with restarted.unit_of_work(operation_id="attention-op") as unit:
        durable_mark = unit.receipt("attention-op")
    with restarted.unit_of_work(operation_id="resolve-op") as unit:
        durable_resolve = unit.receipt("resolve-op")

    assert state.review.state.value == "resolved"
    assert state.review.reason == "caption is uncertain"
    assert [event.action for event in state.review.history] == [
        "attention.mark",
        "attention.resolve",
    ]
    assert [event.comment for event in state.review.history] == [
        "Compare against the scan.",
        "Verified.",
    ]
    assert durable_mark == marked.receipt
    assert durable_mark.inverse.action == "attention.clear"
    assert durable_resolve == resolved.receipt
    assert durable_resolve.inverse.action == "attention.reopen"


def test_clear_and_reopen_commands_round_trip_without_erasing_machine_data(
    tmp_path,
):
    root = tmp_path / "library"
    repository = _repository(root)
    service = CorrectionService(repository)

    service.assign_category(
        AssignImageCategoryCommand(
            "book-1",
            "source-1",
            "source-r1",
            "spine",
            "category-set",
        )
    )
    state = _state(repository)
    service.clear_category(
        ClearImageCategoryCommand(
            "book-1",
            "source-1",
            state.artifact("source-1").revision,
            "category-clear",
        )
    )

    state = _state(repository)
    service.set_manual_caption(
        SetManualCaptionCommand(
            "book-1",
            "crop-1",
            state.artifact("crop-1").revision,
            "Manual caption",
            "caption-set",
        )
    )
    state = _state(repository)
    service.clear_manual_caption(
        ClearManualCaptionCommand(
            "book-1",
            "crop-1",
            state.artifact("crop-1").revision,
            "caption-clear",
        )
    )

    state = _state(repository)
    service.assign_region_role(
        AssignRegionRoleCommand(
            "book-1",
            "region-1",
            state.annotation("region-1").revision,
            "MAR",
            "role-set",
            linked_artifact_id="crop-1",
            expected_linked_artifact_revision=state.artifact("crop-1").revision,
        )
    )
    state = _state(repository)
    service.clear_region_role(
        ClearRegionRoleCommand(
            "book-1",
            "region-1",
            state.annotation("region-1").revision,
            "role-clear",
            linked_artifact_id="crop-1",
            expected_linked_artifact_revision=state.artifact("crop-1").revision,
        )
    )

    state = _state(repository)
    service.assert_artifact_metadata(
        AssertArtifactMetadataCommand(
            "book-1",
            "crop-1",
            state.artifact("crop-1").revision,
            "metadata-set",
            assertions={"plate_number": 7},
        )
    )
    state = _state(repository)
    service.assert_artifact_metadata(
        AssertArtifactMetadataCommand(
            "book-1",
            "crop-1",
            state.artifact("crop-1").revision,
            "metadata-clear",
            clear_names=("plate_number",),
        )
    )

    state = _state(repository)
    service.mark_attention(
        MarkAttentionCommand(
            "book-1",
            state.review.revision,
            "check the crop",
            "reviewer-1",
            "review-mark",
        )
    )
    state = _state(repository)
    service.resolve(
        ResolveCorrectionsCommand(
            "book-1",
            state.review.revision,
            "reviewer-1",
            "review-resolve",
        )
    )
    state = _state(repository)
    service.reopen(
        ReopenCorrectionsCommand(
            "book-1",
            state.review.revision,
            "reviewer-1",
            "review-reopen",
        )
    )

    restarted = _repository(root)
    state = _state(restarted)
    source = state.artifact("source-1")
    crop = state.artifact("crop-1")
    annotation = state.annotation("region-1")
    assert source.category(AssignmentOrigin.MANUAL) is None
    assert source.category(AssignmentOrigin.SUGGESTED).category == "cover"
    assert crop.caption(CaptionOrigin.MANUAL) is None
    assert crop.caption(CaptionOrigin.MACHINE).text == "Machine caption"
    assert crop.role(RoleAssignmentOrigin.MANUAL) is None
    assert crop.role(RoleAssignmentOrigin.MACHINE).role == "figure"
    assert annotation.role(RoleAssignmentOrigin.MANUAL) is None
    assert annotation.role(RoleAssignmentOrigin.MACHINE).role == "figure"
    assert crop.metadata("plate_number", MetadataAssertionOrigin.MANUAL) is None
    assert state.review.state.value == "needs_attention"
    assert [event.action for event in state.review.history] == [
        "attention.mark",
        "attention.resolve",
        "attention.reopen",
    ]


@pytest.mark.parametrize("failure_index", [0, 1])
def test_ordinary_publication_failure_rolls_back_aggregate_and_receipt(
    tmp_path,
    failure_index,
):
    root = tmp_path / "library"

    def fail(index: int, _target: Path) -> None:
        if index == failure_index:
            raise RuntimeError("injected publication failure")

    repository = _repository(root, hook=fail)
    with pytest.raises(RepositoryError):
        CorrectionService(repository).assign_category(
            AssignImageCategoryCommand(
                "book-1",
                "source-1",
                "source-r1",
                "cover",
                "rollback-op",
            )
        )

    assert not _receipt_path(root, "rollback-op").exists()
    assert not _aggregate_path(root).exists()


def test_identifiers_are_validated_and_never_interpreted_as_paths(tmp_path):
    root = tmp_path / "library"
    repository = _repository(root)

    with pytest.raises(RepositoryError) as operation_error:
        with repository.unit_of_work(operation_id="../escape"):
            pass
    assert operation_error.value.code == "invalid_correction_repository_identity"

    with repository.unit_of_work(operation_id="safe-op") as unit:
        with pytest.raises(RepositoryError) as item_error:
            unit.get("../book")
    assert item_error.value.code == "invalid_correction_repository_identity"
    assert not (tmp_path / "escape").exists()
