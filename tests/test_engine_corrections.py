from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace

import pytest

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
    CorrectionAuditEvent,
    CorrectionMutationReceipt,
    CorrectionReviewSnapshot,
    CorrectionService,
    MarkAttentionCommand,
    MetadataAssertionOrigin,
    ReopenCorrectionsCommand,
    ResolveCorrectionsCommand,
    SetManualCaptionCommand,
)
from librarytool.engine.errors import (
    ConflictError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
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


def _provenance(origin="human") -> ArtifactProvenance:
    return ArtifactProvenance(origin=origin, provider_id="test-provider")


def _artifact(
    artifact_id,
    *,
    revision=None,
    source_artifact_id="",
    categories=(),
    captions=(),
    roles=(),
    metadata=(),
):
    return ArtifactCorrectionSnapshot(
        key=RasterArtifactKey("book-1", artifact_id),
        revision=revision or f"{artifact_id}-r1",
        source_artifact_id=source_artifact_id,
        category_assignments=categories,
        caption_assertions=captions,
        role_assignments=roles,
        metadata_assertions=metadata,
    )


def _annotation(*, revision="region-r1", roles=()):
    return AnnotationCorrectionSnapshot(
        key=SpatialAnnotationKey("book-1", "region-1"),
        revision=revision,
        linked_artifact_id="crop-1",
        role_assignments=roles,
    )


def _aggregate() -> CorrectionAggregateSnapshot:
    source = _artifact(
        "source-1",
        categories=(
            CategoryAssignment(
                "cover",
                AssignmentOrigin.SUGGESTED,
                "source-category-r1",
                confidence=0.8,
                provenance=_provenance("machine"),
            ),
        ),
    )
    crop = _artifact(
        "crop-1",
        source_artifact_id="source-1",
        categories=(
            CategoryAssignment(
                "spine",
                AssignmentOrigin.SUGGESTED,
                "crop-category-r1",
                provenance=_provenance("machine"),
            ),
        ),
        captions=(
            CaptionAssertion(
                "Machine caption",
                CaptionOrigin.MACHINE,
                "caption-machine-r1",
                provenance=_provenance("machine"),
            ),
        ),
        roles=(
            SpatialRoleAssignment(
                "figure",
                RoleAssignmentOrigin.MACHINE,
                "artifact-role-machine-r1",
                provenance=_provenance("machine"),
            ),
        ),
        metadata=(
            ArtifactMetadataAssertion(
                "caption_source",
                "ocr",
                MetadataAssertionOrigin.MACHINE,
                "metadata-machine-r1",
                _provenance("machine"),
            ),
        ),
    )
    sibling = _artifact("sibling-1")
    annotation = _annotation(
        roles=(
            SpatialRoleAssignment(
                "figure",
                RoleAssignmentOrigin.MACHINE,
                "region-role-machine-r1",
                provenance=_provenance("machine"),
            ),
        )
    )
    return CorrectionAggregateSnapshot(
        "book-1",
        "aggregate-r1",
        (source, crop, sibling),
        (annotation,),
        CorrectionReviewSnapshot("review-r1"),
    )


def _replace_origin(values, origin, replacement):
    retained = tuple(value for value in values if value.origin is not origin)
    return retained if replacement is None else (*retained, replacement)


class _MemoryRepository:
    def __init__(self, aggregate=None):
        self.aggregate = aggregate or _aggregate()
        self.receipts: dict[str, CorrectionMutationReceipt] = {}
        self.revisions = 1
        self.stages = 0
        self.commits = 0
        self.pending = None
        self.stage_override = None

    def revision(self, prefix):
        self.revisions += 1
        return f"{prefix}-r{self.revisions}"

    @contextmanager
    def unit_of_work(self, *, operation_id):
        yield _MemoryUnit(self)


class _MemoryUnit:
    def __init__(self, repository):
        self.repository = repository

    def receipt(self, operation_id):
        return self.repository.receipts.get(operation_id)

    def get(self, item_id):
        return self.repository.aggregate if item_id == "book-1" else None

    def stage(self, current, command):
        self.repository.stages += 1
        if self.repository.stage_override is not None:
            staged = self.repository.stage_override(current, command)
            self.repository.pending = staged
            return staged

        artifacts = {value.key.artifact_id: value for value in current.artifacts}
        annotations = {
            value.key.annotation_id: value for value in current.annotations
        }
        review = current.review
        if isinstance(command, AssignImageCategoryCommand):
            before = artifacts[command.artifact_id]
            assignment = CategoryAssignment(
                command.category,
                AssignmentOrigin.MANUAL,
                self.repository.revision("category"),
                provenance=command.provenance,
            )
            artifacts[command.artifact_id] = replace(
                before,
                revision=self.repository.revision(command.artifact_id),
                category_assignments=_replace_origin(
                    before.category_assignments,
                    AssignmentOrigin.MANUAL,
                    assignment,
                ),
            )
        elif isinstance(command, ClearImageCategoryCommand):
            before = artifacts[command.artifact_id]
            artifacts[command.artifact_id] = replace(
                before,
                revision=self.repository.revision(command.artifact_id),
                category_assignments=_replace_origin(
                    before.category_assignments,
                    AssignmentOrigin.MANUAL,
                    None,
                ),
            )
        elif isinstance(command, SetManualCaptionCommand):
            before = artifacts[command.artifact_id]
            assertion = CaptionAssertion(
                command.text,
                CaptionOrigin.MANUAL,
                self.repository.revision("caption"),
                language=command.language,
                provenance=command.provenance,
            )
            artifacts[command.artifact_id] = replace(
                before,
                revision=self.repository.revision(command.artifact_id),
                caption_assertions=_replace_origin(
                    before.caption_assertions,
                    CaptionOrigin.MANUAL,
                    assertion,
                ),
            )
        elif isinstance(command, ClearManualCaptionCommand):
            before = artifacts[command.artifact_id]
            artifacts[command.artifact_id] = replace(
                before,
                revision=self.repository.revision(command.artifact_id),
                caption_assertions=_replace_origin(
                    before.caption_assertions,
                    CaptionOrigin.MANUAL,
                    None,
                ),
            )
        elif isinstance(command, AssertArtifactMetadataCommand):
            before = artifacts[command.artifact_id]
            changed = set(command.assertions) | set(command.clear_names)
            assertions = tuple(
                value
                for value in before.metadata_assertions
                if not (
                    value.origin is MetadataAssertionOrigin.MANUAL
                    and value.name in changed
                )
            )
            assertions += tuple(
                ArtifactMetadataAssertion(
                    name,
                    value,
                    MetadataAssertionOrigin.MANUAL,
                    self.repository.revision("metadata"),
                    command.provenance,
                )
                for name, value in command.assertions.items()
            )
            artifacts[command.artifact_id] = replace(
                before,
                revision=self.repository.revision(command.artifact_id),
                metadata_assertions=assertions,
            )
        elif isinstance(command, (AssignRegionRoleCommand, ClearRegionRoleCommand)):
            before = annotations[command.annotation_id]
            assignment = None
            if isinstance(command, AssignRegionRoleCommand):
                assignment = SpatialRoleAssignment(
                    command.role,
                    RoleAssignmentOrigin.MANUAL,
                    self.repository.revision("role"),
                    provenance=command.provenance,
                )
            annotations[command.annotation_id] = replace(
                before,
                revision=self.repository.revision(command.annotation_id),
                linked_artifact_id=(
                    before.linked_artifact_id or command.linked_artifact_id
                ),
                role_assignments=_replace_origin(
                    before.role_assignments,
                    RoleAssignmentOrigin.MANUAL,
                    assignment,
                ),
            )
            if command.linked_artifact_id:
                artifact = artifacts[command.linked_artifact_id]
                artifact_assignment = None
                if isinstance(command, AssignRegionRoleCommand):
                    artifact_assignment = SpatialRoleAssignment(
                        command.role,
                        RoleAssignmentOrigin.MANUAL,
                        self.repository.revision("artifact-role"),
                        provenance=command.provenance,
                    )
                artifacts[command.linked_artifact_id] = replace(
                    artifact,
                    revision=self.repository.revision(command.linked_artifact_id),
                    role_assignments=_replace_origin(
                        artifact.role_assignments,
                        RoleAssignmentOrigin.MANUAL,
                        artifact_assignment,
                    ),
                )
        else:
            if isinstance(command, MarkAttentionCommand):
                action = "attention.mark"
                after_state = "needs_attention"
                reason = command.reason
                event_reason = command.reason
            elif isinstance(command, ResolveCorrectionsCommand):
                action = "attention.resolve"
                after_state = "resolved"
                reason = review.reason
                event_reason = ""
            else:
                assert isinstance(command, ReopenCorrectionsCommand)
                action = "attention.reopen"
                after_state = "needs_attention"
                reason = review.reason
                event_reason = ""
            event = CorrectionAuditEvent(
                operation_id=command.operation_id,
                action=action,
                actor_id=command.actor_id,
                occurred_at="2026-07-22T12:00:00Z",
                before_state=review.state,
                after_state=after_state,
                reason=event_reason,
                comment=command.comment,
            )
            review = CorrectionReviewSnapshot(
                revision=self.repository.revision("review"),
                state=after_state,
                reason=reason,
                history=(*review.history, event),
            )

        staged = CorrectionAggregateSnapshot(
            item_id=current.item_id,
            revision=self.repository.revision("aggregate"),
            artifacts=tuple(artifacts.values()),
            annotations=tuple(annotations.values()),
            review=review,
        )
        self.repository.pending = staged
        return staged

    def commit(self, receipt):
        assert self.repository.pending is not None
        self.repository.aggregate = self.repository.pending
        self.repository.receipts[receipt.operation_id] = receipt
        self.repository.commits += 1


def test_source_category_inheritance_changes_without_child_fanout():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    child_before = repository.aggregate.artifact("crop-1")
    assert repository.aggregate.effective_category("crop-1").as_dict() == {
        "category": "cover",
        "origin": "inherited",
        "assignment_revision": "source-category-r1",
        "inherited_from_artifact_id": "source-1",
    }

    assigned = service.assign_category(
        AssignImageCategoryCommand(
            "book-1",
            "source-1",
            "source-1-r1",
            "title_page",
            "category-op-1",
        )
    )

    assert repository.aggregate.effective_category("crop-1").category == "title_page"
    assert repository.aggregate.artifact("crop-1") is child_before
    assert [(target.kind.value, target.target_id) for target in assigned.receipt.targets] == [
        ("artifact", "source-1")
    ]
    assert assigned.receipt.inverse.action == "category.clear"

    source_revision = repository.aggregate.artifact("source-1").revision
    cleared = service.clear_category(
        ClearImageCategoryCommand(
            "book-1",
            "source-1",
            source_revision,
            "category-op-2",
        )
    )
    assert repository.aggregate.effective_category("crop-1").category == "cover"
    assert cleared.receipt.inverse.action == "category.assign"
    assert cleared.receipt.inverse.payload["assignment"]["category"] == "title_page"


def test_source_recategorization_never_overrides_explicit_child_category():
    aggregate = _aggregate()
    child = aggregate.artifact("crop-1")
    manual_child_category = CategoryAssignment(
        "content_specimen",
        AssignmentOrigin.MANUAL,
        "crop-category-manual-r1",
        provenance=_provenance(),
    )
    child = replace(
        child,
        category_assignments=(
            *child.category_assignments,
            manual_child_category,
        ),
    )
    repository = _MemoryRepository(
        replace(
            aggregate,
            artifacts=tuple(
                child if artifact.key.artifact_id == "crop-1" else artifact
                for artifact in aggregate.artifacts
            ),
        )
    )

    CorrectionService(repository).assign_category(
        AssignImageCategoryCommand(
            "book-1",
            "source-1",
            "source-1-r1",
            "title_page",
            "category-parent-op",
        )
    )

    assert repository.aggregate.artifact("crop-1") is child
    effective = repository.aggregate.effective_category("crop-1")
    assert effective.category == "content_specimen"
    assert effective.origin.value == "manual"
    assert effective.assignment_revision == "crop-category-manual-r1"


def test_exact_replay_is_idempotent_and_operation_id_reuse_conflicts():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    command = AssignImageCategoryCommand(
        "book-1",
        "source-1",
        "source-1-r1",
        "cover",
        "replay-op",
    )
    first = service.assign_category(command)
    replay = service.assign_category(command)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.receipt is first.receipt
    assert repository.stages == 1
    assert repository.commits == 1

    with pytest.raises(ConflictError) as caught:
        service.assign_category(replace(command, category="spine"))
    assert caught.value.code == "operation_id_conflict"


def test_stale_target_revision_and_missing_operation_id_are_typed():
    service = CorrectionService(_MemoryRepository())
    with pytest.raises(ConflictError) as stale:
        service.assign_category(
            AssignImageCategoryCommand(
                "book-1",
                "source-1",
                "old-r1",
                "cover",
                "stale-op",
            )
        )
    assert stale.value.code == "artifact_revision_conflict"

    with pytest.raises(PreconditionRequiredError) as missing:
        service.assign_category(
            AssignImageCategoryCommand(
                "book-1",
                "source-1",
                "source-1-r1",
                "cover",
                "",
            )
        )
    assert missing.value.code == "operation_id_required"


def test_manual_caption_set_and_clear_preserve_machine_assertion():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    set_result = service.set_manual_caption(
        SetManualCaptionCommand(
            "book-1",
            "crop-1",
            "crop-1-r1",
            "Corrected caption",
            "caption-op-1",
            language="en",
        )
    )
    artifact = repository.aggregate.artifact("crop-1")
    assert artifact.caption(CaptionOrigin.MACHINE).text == "Machine caption"
    assert artifact.caption(CaptionOrigin.MANUAL).text == "Corrected caption"
    assert set_result.receipt.inverse.action == "caption.clear"

    cleared = service.clear_manual_caption(
        ClearManualCaptionCommand(
            "book-1",
            "crop-1",
            artifact.revision,
            "caption-op-2",
        )
    )
    artifact = repository.aggregate.artifact("crop-1")
    assert artifact.caption(CaptionOrigin.MANUAL) is None
    assert artifact.caption(CaptionOrigin.MACHINE).text == "Machine caption"
    assert cleared.receipt.inverse.action == "caption.set"
    assert cleared.receipt.inverse.payload["assertion"]["text"] == (
        "Corrected caption"
    )


def test_metadata_assertions_are_bounded_layered_and_reversible():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    result = service.assert_artifact_metadata(
        AssertArtifactMetadataCommand(
            "book-1",
            "crop-1",
            "crop-1-r1",
            "metadata-op-1",
            assertions={"caption_source": "manual", "plate_number": 7},
        )
    )
    artifact = repository.aggregate.artifact("crop-1")
    assert artifact.effective_metadata() == {
        "caption_source": "manual",
        "plate_number": 7,
    }
    assert artifact.metadata(
        "caption_source",
        MetadataAssertionOrigin.MACHINE,
    ).value == "ocr"
    assert result.receipt.inverse.payload["clear_names"] == (
        "caption_source",
        "plate_number",
    )
    assert result.receipt.inverse.payload["restore_assertions"] == ()

    with pytest.raises(ValidationError) as private:
        AssertArtifactMetadataCommand(
            "book-1",
            "crop-1",
            artifact.revision,
            "metadata-op-private",
            assertions={"future": {"local_path": "C:/private.jpg"}},
        )
    assert private.value.code == "private_artifact_extension"


def test_linked_region_and_extracted_artifact_update_in_one_transaction():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    result = service.assign_region_role(
        AssignRegionRoleCommand(
            "book-1",
            "region-1",
            "region-r1",
            "MAR",
            "role-op-1",
            linked_artifact_id="crop-1",
            expected_linked_artifact_revision="crop-1-r1",
        )
    )

    annotation = repository.aggregate.annotation("region-1")
    artifact = repository.aggregate.artifact("crop-1")
    assert annotation.role(RoleAssignmentOrigin.MANUAL).role == "marginalia"
    assert artifact.role(RoleAssignmentOrigin.MANUAL).role == "marginalia"
    assert annotation.role(RoleAssignmentOrigin.MACHINE).role == "figure"
    assert artifact.role(RoleAssignmentOrigin.MACHINE).role == "figure"
    assert repository.stages == repository.commits == 1
    assert {(target.kind.value, target.target_id) for target in result.receipt.targets} == {
        ("annotation", "region-1"),
        ("artifact", "crop-1"),
    }

    cleared = service.clear_region_role(
        ClearRegionRoleCommand(
            "book-1",
            "region-1",
            annotation.revision,
            "role-op-2",
            linked_artifact_id="crop-1",
            expected_linked_artifact_revision=artifact.revision,
        )
    )
    assert repository.aggregate.annotation("region-1").role(
        RoleAssignmentOrigin.MANUAL
    ) is None
    assert repository.aggregate.artifact("crop-1").role(
        RoleAssignmentOrigin.MANUAL
    ) is None
    assert cleared.receipt.inverse.action == "role.assign"


def test_linked_role_requires_both_revision_pins():
    service = CorrectionService(_MemoryRepository())
    with pytest.raises(PreconditionRequiredError) as missing:
        service.assign_region_role(
            AssignRegionRoleCommand(
                "book-1",
                "region-1",
                "region-r1",
                "ILL",
                "role-missing-link",
            )
        )
    assert missing.value.code == "linked_artifact_revision_required"

    with pytest.raises(ConflictError) as stale:
        service.assign_region_role(
            AssignRegionRoleCommand(
                "book-1",
                "region-1",
                "region-r1",
                "ILL",
                "role-stale-link",
                linked_artifact_id="crop-1",
                expected_linked_artifact_revision="old-r1",
            )
        )
    assert stale.value.code == "artifact_revision_conflict"


def test_linked_role_inverse_restores_asymmetric_manual_artifact_state():
    aggregate = _aggregate()
    prior = SpatialRoleAssignment(
        "figure",
        RoleAssignmentOrigin.MANUAL,
        "artifact-role-manual-r1",
        provenance=_provenance(),
    )
    artifacts = tuple(
        replace(
            artifact,
            role_assignments=(*artifact.role_assignments, prior),
        )
        if artifact.key.artifact_id == "crop-1"
        else artifact
        for artifact in aggregate.artifacts
    )
    repository = _MemoryRepository(replace(aggregate, artifacts=artifacts))
    result = CorrectionService(repository).assign_region_role(
        AssignRegionRoleCommand(
            "book-1",
            "region-1",
            "region-r1",
            "MAR",
            "role-asymmetric-op",
            linked_artifact_id="crop-1",
            expected_linked_artifact_revision="crop-1-r1",
        )
    )

    inverse = result.receipt.inverse
    assert inverse.action == "role.clear"
    assert inverse.payload["linked_assignment"]["role"] == "figure"
    assert inverse.payload["linked_assignment"]["origin"] == "manual"


def test_attention_resolve_reopen_append_audit_and_keep_inverse_transitions():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    marked = service.mark_attention(
        MarkAttentionCommand(
            "book-1",
            "review-r1",
            "Caption needs checking",
            "curator-1",
            "review-op-1",
            "Found during QA",
        )
    )
    assert repository.aggregate.review.state.value == "needs_attention"
    assert marked.receipt.inverse.action == "attention.clear"
    assert marked.receipt.inverse.payload["reason"] == "Caption needs checking"
    assert marked.receipt.inverse.payload["append_audit"] is True

    resolved = service.resolve(
        ResolveCorrectionsCommand(
            "book-1",
            repository.aggregate.review.revision,
            "curator-1",
            "review-op-2",
            "Caption corrected",
        )
    )
    reopened = service.reopen(
        ReopenCorrectionsCommand(
            "book-1",
            repository.aggregate.review.revision,
            "curator-2",
            "review-op-3",
            "Second review requested",
        )
    )

    review = repository.aggregate.review
    assert review.state.value == "needs_attention"
    assert review.reason == "Caption needs checking"
    assert [event.action for event in review.history] == [
        "attention.mark",
        "attention.resolve",
        "attention.reopen",
    ]
    assert resolved.receipt.inverse.action == "attention.reopen"
    assert reopened.receipt.inverse.action == "attention.resolve"


def test_attention_clear_inverse_can_append_a_continuous_audit_event():
    marked = CorrectionAuditEvent(
        operation_id="review-clear-op-1",
        action="attention.mark",
        actor_id="curator-1",
        occurred_at="2026-07-22T12:00:00Z",
        before_state="clear",
        after_state="needs_attention",
        reason="Caption needs checking",
    )
    cleared = CorrectionAuditEvent(
        operation_id="review-clear-op-2",
        action="attention.clear",
        actor_id="curator-1",
        occurred_at="2026-07-22T12:01:00Z",
        before_state="needs_attention",
        after_state="clear",
        reason="Caption needs checking",
        comment="Undo attention mark",
    )

    review = CorrectionReviewSnapshot(
        revision="review-clear-r1",
        state="clear",
        history=(marked, cleared),
    )
    assert review.history[-1].action == "attention.clear"
    assert review.state.value == "clear"


def test_review_snapshot_rejects_discontinuous_audit_history():
    marked = CorrectionAuditEvent(
        operation_id="review-gap-op-1",
        action="attention.mark",
        actor_id="curator-1",
        occurred_at="2026-07-22T12:00:00Z",
        before_state="clear",
        after_state="needs_attention",
        reason="Caption needs checking",
    )
    reopened_without_resolve = CorrectionAuditEvent(
        operation_id="review-gap-op-2",
        action="attention.reopen",
        actor_id="curator-2",
        occurred_at="2026-07-22T12:01:00Z",
        before_state="resolved",
        after_state="needs_attention",
    )

    with pytest.raises(ValidationError) as caught:
        CorrectionReviewSnapshot(
            revision="review-gap-r2",
            state="needs_attention",
            reason="Caption needs checking",
            history=(marked, reopened_without_resolve),
        )
    assert caught.value.code == "invalid_correction_audit"


def test_service_rejects_adapter_that_reuses_changed_assertion_revision():
    repository = _MemoryRepository()
    service = CorrectionService(repository)
    service.assign_category(
        AssignImageCategoryCommand(
            "book-1",
            "source-1",
            "source-1-r1",
            "title_page",
            "category-revision-op-1",
        )
    )
    artifact = repository.aggregate.artifact("source-1")
    prior_manual = artifact.category(AssignmentOrigin.MANUAL)
    assert prior_manual is not None

    def corrupt(current, command):
        before = current.artifact("source-1")
        changed = replace(
            before,
            revision="source-corrupt-r3",
            category_assignments=_replace_origin(
                before.category_assignments,
                AssignmentOrigin.MANUAL,
                CategoryAssignment(
                    command.category,
                    AssignmentOrigin.MANUAL,
                    prior_manual.revision,
                    provenance=command.provenance,
                ),
            ),
        )
        return replace(
            current,
            revision="aggregate-corrupt-r3",
            artifacts=tuple(
                changed if value.key.artifact_id == "source-1" else value
                for value in current.artifacts
            ),
        )

    repository.stage_override = corrupt
    with pytest.raises(RepositoryError) as caught:
        service.assign_category(
            AssignImageCategoryCommand(
                "book-1",
                "source-1",
                artifact.revision,
                "spine",
                "category-revision-op-2",
            )
        )
    assert caught.value.code == "correction_repository_content_mismatch"
    assert repository.commits == 1


def test_service_rejects_adapter_that_drops_machine_caption_assertions():
    repository = _MemoryRepository()

    def corrupt(current, command):
        artifact = current.artifact("crop-1")
        changed = replace(
            artifact,
            revision="crop-corrupt-r2",
            caption_assertions=(
                CaptionAssertion(
                    command.text,
                    CaptionOrigin.MANUAL,
                    "caption-human-r2",
                    provenance=command.provenance,
                ),
            ),
        )
        return replace(
            current,
            revision="aggregate-corrupt-r2",
            artifacts=tuple(
                changed if value.key.artifact_id == "crop-1" else value
                for value in current.artifacts
            ),
        )

    repository.stage_override = corrupt
    with pytest.raises(RepositoryError) as caught:
        CorrectionService(repository).set_manual_caption(
            SetManualCaptionCommand(
                "book-1",
                "crop-1",
                "crop-1-r1",
                "Human caption",
                "corrupt-op",
            )
        )
    assert caught.value.code == "correction_repository_content_mismatch"
    assert repository.commits == 0


def test_public_receipt_contains_inverse_but_not_replay_fingerprint():
    repository = _MemoryRepository()
    result = CorrectionService(repository).assign_category(
        AssignImageCategoryCommand(
            "book-1",
            "source-1",
            "source-1-r1",
            "content_specimen",
            "public-receipt-op",
        )
    )
    public = result.as_dict()
    assert "command_sha256" not in public["receipt"]
    assert public["receipt"]["inverse"]["expected_targets"] == [
        result.receipt.targets[0].as_dict()
    ]
