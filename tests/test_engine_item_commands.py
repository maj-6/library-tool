"""Framework-neutral item command contracts and lifecycle invariants."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from librarytool.engine.errors import (
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.item_commands import (
    CreateItemCommand,
    DeleteItemCommand,
    ItemCommandService,
    ItemDeletionSnapshot,
    ItemDraft,
    ItemMutationReceipt,
    ItemPatch,
    ItemRecordSnapshot,
    RepresentationDraft,
    UpdateItemCommand,
)


_DEFAULT = object()


def _representation(identifier: str = "primary", **metadata):
    return RepresentationDraft(
        identifier,
        role="primary" if identifier == "primary" else "alternate",
        media_type="application/pdf",
        locator=f"urn:test:{identifier}",
        label=identifier.title(),
        metadata=metadata,
    )


def _draft(title: str = "A Herbal", **metadata) -> ItemDraft:
    return ItemDraft(
        kind="book",
        title=title,
        metadata=metadata,
        representations=(_representation(),),
    )


def _snapshot(
    item_id: str = "book-1",
    revision: str = "rev-1",
    draft: ItemDraft | None = None,
) -> ItemRecordSnapshot:
    value = draft or _draft()
    return ItemRecordSnapshot(
        item_id=item_id,
        revision=revision,
        kind=value.kind,
        title=value.title,
        metadata=value.metadata,
        representations=value.representations,
    )


class _MemoryRepository:
    """Explicit-staging fake; exiting without commit changes nothing."""

    def __init__(self, records=()):
        self.records = {record.item_id: record for record in records}
        self.receipts = {}
        self.units = []
        self.next_item_id = "item-created"
        self.revision_number = 1
        self.receipt_override = _DEFAULT
        self.get_override = _DEFAULT
        self.stage_snapshot = _DEFAULT
        self.stage_deletion = _DEFAULT
        self.fail_commit = None
        self.commits = 0
        self.allocations = 0
        self.stages = 0
        self.events = []

    @contextmanager
    def unit_of_work(self, *, operation_id):
        unit = _MemoryUnit(self, operation_id)
        self.units.append(unit)
        yield unit


class _MemoryUnit:
    def __init__(self, repository, operation_id):
        self.repository = repository
        self.operation_id = operation_id
        self.pending = None

    def receipt(self, operation_id):
        self.repository.events.append("receipt")
        override = self.repository.receipt_override
        if override is not _DEFAULT:
            return override
        return self.repository.receipts.get(operation_id)

    def get(self, item_id):
        self.repository.events.append("get")
        override = self.repository.get_override
        if override is not _DEFAULT:
            return override
        return self.repository.records.get(item_id)

    def allocate_item_id(self):
        self.repository.events.append("allocate")
        self.repository.allocations += 1
        return self.repository.next_item_id

    def _next_revision(self):
        self.repository.revision_number += 1
        return f"rev-{self.repository.revision_number}"

    def stage_create(self, item_id, draft):
        self.repository.events.append("stage:create")
        self.repository.stages += 1
        override = self.repository.stage_snapshot
        if override is not _DEFAULT:
            return override
        record = _snapshot(item_id, self._next_revision(), draft)
        self.pending = ("save", record)
        return record

    def stage_replace(self, current, draft):
        self.repository.events.append("stage:update")
        self.repository.stages += 1
        override = self.repository.stage_snapshot
        if override is not _DEFAULT:
            return override
        record = _snapshot(current.item_id, self._next_revision(), draft)
        self.pending = ("save", record)
        return record

    def stage_delete(self, current):
        self.repository.events.append("stage:delete")
        self.repository.stages += 1
        override = self.repository.stage_deletion
        if override is not _DEFAULT:
            return override
        deletion = ItemDeletionSnapshot(
            current.item_id,
            current.revision,
            f"tombstone-{current.item_id}",
        )
        self.pending = ("delete", deletion)
        return deletion

    def commit(self, receipt):
        self.repository.events.append("commit")
        if self.repository.fail_commit is not None:
            raise self.repository.fail_commit
        action, value = self.pending
        if action == "save":
            self.repository.records[value.item_id] = value
        else:
            self.repository.records.pop(value.item_id, None)
        self.repository.receipts[receipt.operation_id] = receipt
        self.repository.commits += 1


class _RecordingPolicy:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.create_candidates = []
        self.update_candidates = []
        self.failure = None
        self.result = None

    def validate_create(self, candidate):
        self.events.append("policy:create")
        self.create_candidates.append(candidate)
        if self.failure is not None:
            raise self.failure
        return self.result

    def validate_update(self, current, patch, candidate):
        self.events.append("policy:update")
        self.update_candidates.append((current, patch, candidate))
        if self.failure is not None:
            raise self.failure
        return self.result


def test_item_dtos_detach_freeze_sort_and_round_trip_strict_json():
    raw = {"nested": {"values": [1, "two"]}}
    draft = ItemDraft(
        title="Portable",
        metadata=raw,
        representations=(_representation("scan-b"), _representation()),
    )
    raw["nested"]["values"].append(3)

    assert draft.metadata["nested"]["values"] == (1, "two")
    assert [row.representation_id for row in draft.representations] == [
        "primary",
        "scan-b",
    ]
    with pytest.raises(TypeError):
        draft.metadata["new"] = True
    assert ItemDraft.from_dict(draft.as_dict()) == draft
    json.dumps(draft.as_dict(), allow_nan=False, ensure_ascii=False)


def test_dtos_reject_hostile_json_and_ambiguous_patch_shapes():
    cycle = {}
    cycle["self"] = cycle
    hostile = (
        {"bad": float("nan")},
        {"bad": object()},
        {1: "non-string key"},
        {"bad": "nul\x00byte"},
        {"bad": "surrogate\ud800"},
        cycle,
    )
    for metadata in hostile:
        with pytest.raises((TypeError, ValueError)):
            ItemDraft(metadata=metadata)

    with pytest.raises(ValueError, match="duplicate representation"):
        ItemDraft(
            representations=(
                _representation("Scan"),
                _representation("scan"),
            )
        )
    with pytest.raises(ValueError, match="overlap"):
        ItemPatch(metadata_set={"year": "1700"}, metadata_remove=("year",))
    with pytest.raises(ValueError, match="portable"):
        RepresentationDraft("../scan")


def test_receipts_are_strict_round_trippable_action_specific_contracts():
    item = _snapshot(revision="rev-2")
    receipt = ItemMutationReceipt(
        action="update",
        operation_id="update-1",
        command_sha256="a" * 64,
        item_id=item.item_id,
        before_revision="rev-1",
        after_revision="rev-2",
        item=item,
    )

    assert ItemMutationReceipt.from_dict(receipt.as_dict()) == receipt
    json.dumps(receipt.as_dict(), allow_nan=False)
    with pytest.raises(ValueError, match="does not match"):
        ItemMutationReceipt(
            action="delete",
            operation_id="delete-1",
            command_sha256="b" * 64,
            item_id="book-1",
            before_revision="rev-1",
            item=item,
        )
    malformed = receipt.as_dict()
    malformed["extra"] = True
    with pytest.raises(ValueError, match="schema"):
        ItemMutationReceipt.from_dict(malformed)


def test_create_stages_one_atomic_outcome_with_canonical_command_identity():
    first = _MemoryRepository()
    second = _MemoryRepository()
    left = ItemDraft(title="Herbal", metadata={"a": 1, "b": 2})
    right = ItemDraft(title="Herbal", metadata={"b": 2, "a": 1})

    result = ItemCommandService(first).create(
        CreateItemCommand(left, "create-left")
    )
    other = ItemCommandService(second).create(
        CreateItemCommand(right, "create-right")
    )

    assert result.replayed is False
    assert result.receipt.item_id == "item-created"
    assert result.receipt.item.as_draft() == left
    assert first.records["item-created"] == result.receipt.item
    assert first.receipts["create-left"] == result.receipt
    assert first.commits == first.allocations == first.stages == 1
    assert result.receipt.command_sha256 == other.receipt.command_sha256


def test_create_replays_without_allocating_and_rejects_key_reuse():
    repository = _MemoryRepository()
    service = ItemCommandService(repository)
    command = CreateItemCommand(_draft(), "create-retry")

    original = service.create(command)
    replay = service.create(command)

    assert replay.replayed is True
    assert replay.receipt == original.receipt
    assert repository.commits == repository.allocations == 1
    with pytest.raises(ConflictError) as caught:
        service.create(
            CreateItemCommand(_draft(title="Different"), "create-retry")
        )
    assert caught.value.code == "operation_id_conflict"
    assert repository.commits == 1


def test_create_policy_runs_after_replay_lookup_and_before_allocation():
    repository = _MemoryRepository()
    policy = _RecordingPolicy(repository.events)
    draft = _draft()

    ItemCommandService(repository, policy=policy).create(
        CreateItemCommand(draft, "create-policy-order")
    )

    assert repository.events == [
        "receipt",
        "policy:create",
        "allocate",
        "get",
        "stage:create",
        "commit",
    ]
    assert policy.create_candidates == [draft]


def test_exact_create_replay_bypasses_policy_validation():
    repository = _MemoryRepository()
    policy = _RecordingPolicy(repository.events)
    service = ItemCommandService(repository, policy=policy)
    command = CreateItemCommand(_draft(), "create-policy-replay")
    original = service.create(command)
    policy.failure = ValidationError("policy now rejects", code="policy_reject")
    repository.events.clear()

    replay = service.create(command)

    assert replay.replayed is True
    assert replay.receipt == original.receipt
    assert repository.events == ["receipt"]
    assert len(policy.create_candidates) == 1


def test_policy_rejection_does_not_allocate_stage_commit_or_change_records():
    rejection = ValidationError("profile rejected item", code="profile_reject")
    create_repository = _MemoryRepository()
    create_policy = _RecordingPolicy(create_repository.events)
    create_policy.failure = rejection

    with pytest.raises(ValidationError) as create_error:
        ItemCommandService(create_repository, policy=create_policy).create(
            CreateItemCommand(_draft(), "create-policy-reject")
        )

    assert create_error.value is rejection
    assert create_repository.records == {}
    assert create_repository.allocations == 0
    assert create_repository.stages == create_repository.commits == 0

    current = _snapshot()
    update_repository = _MemoryRepository((current,))
    update_policy = _RecordingPolicy(update_repository.events)
    update_policy.failure = ValidationError(
        "profile rejected update",
        code="profile_reject",
    )
    with pytest.raises(ValidationError):
        ItemCommandService(update_repository, policy=update_policy).update(
            UpdateItemCommand(
                current.item_id,
                current.revision,
                ItemPatch(title="Changed"),
                "update-policy-reject",
            )
        )

    assert update_repository.records == {current.item_id: current}
    assert update_repository.stages == update_repository.commits == 0


def test_update_applies_explicit_patch_and_replays_after_revision_changes():
    current = _snapshot(
        draft=ItemDraft(
            title="Old",
            metadata={"keep": 1, "remove": 2},
            representations=(_representation(),),
        )
    )
    repository = _MemoryRepository((current,))
    service = ItemCommandService(repository)
    command = UpdateItemCommand(
        item_id="book-1",
        expected_revision="rev-1",
        patch=ItemPatch(
            title="New",
            metadata_set={"added": None},
            metadata_remove=("remove",),
            representations=(),
        ),
        operation_id="update-1",
    )

    result = service.update(command)
    saved = result.receipt.item

    assert saved.title == "New"
    assert dict(saved.metadata) == {"keep": 1, "added": None}
    assert saved.representations == ()
    assert result.receipt.before_revision == "rev-1"
    assert result.receipt.after_revision != "rev-1"
    assert service.update(command).replayed is True
    assert repository.commits == 1


def test_update_policy_receives_fully_applied_candidate_before_staging():
    current = _snapshot(
        draft=ItemDraft(
            title="Old",
            metadata={"keep": 1, "remove": 2},
            representations=(_representation(),),
        )
    )
    repository = _MemoryRepository((current,))
    policy = _RecordingPolicy(repository.events)
    patch = ItemPatch(
        title="New",
        metadata_set={"added": None},
        metadata_remove=("remove",),
    )

    result = ItemCommandService(repository, policy=policy).update(
        UpdateItemCommand(
            current.item_id,
            current.revision,
            patch,
            "update-policy-candidate",
        )
    )

    assert repository.events == [
        "receipt",
        "get",
        "policy:update",
        "stage:update",
        "commit",
    ]
    policy_current, policy_patch, candidate = policy.update_candidates[0]
    assert policy_current == current
    assert policy_patch == patch
    assert candidate.title == "New"
    assert dict(candidate.metadata) == {"keep": 1, "added": None}
    assert candidate.representations == current.representations
    assert result.receipt.item.as_draft() == candidate

    policy.failure = ValidationError("policy now rejects", code="policy_reject")
    repository.events.clear()
    replay = ItemCommandService(repository, policy=policy).update(
        UpdateItemCommand(
            current.item_id,
            current.revision,
            patch,
            "update-policy-candidate",
        )
    )
    assert replay.replayed is True
    assert replay.receipt == result.receipt
    assert repository.events == ["receipt"]
    assert len(policy.update_candidates) == 1


def test_update_requires_a_match_and_never_stages_missing_or_stale_items():
    current = _snapshot()

    empty_repository = _MemoryRepository()
    with pytest.raises(NotFoundError) as missing:
        ItemCommandService(empty_repository).update(
            UpdateItemCommand(
                "book-1",
                "rev-1",
                ItemPatch(title="new"),
                "update-missing",
            )
        )
    assert missing.value.code == "item_not_found"
    assert empty_repository.stages == empty_repository.commits == 0

    stale_repository = _MemoryRepository((current,))
    with pytest.raises(ConflictError) as stale:
        ItemCommandService(stale_repository).update(
            UpdateItemCommand(
                "book-1",
                "rev-old",
                ItemPatch(title="new"),
                "update-stale",
            )
        )
    assert stale.value.code == "item_revision_conflict"
    assert stale.value.details["current_revision"] == "rev-1"
    assert stale_repository.stages == stale_repository.commits == 0


def test_update_rejects_missing_revision_and_empty_patch_before_opening_uow():
    repository = _MemoryRepository((_snapshot(),))
    service = ItemCommandService(repository)

    with pytest.raises(PreconditionRequiredError) as missing:
        service.update(
            UpdateItemCommand(
                "book-1",
                "",
                ItemPatch(title="new"),
                "update-no-revision",
            )
        )
    assert missing.value.code == "item_revision_required"
    with pytest.raises(ValidationError) as empty:
        service.update(
            UpdateItemCommand(
                "book-1",
                "rev-1",
                ItemPatch(),
                "update-empty",
            )
        )
    assert empty.value.code == "empty_item_patch"
    assert repository.units == []


def test_delete_stages_a_server_tombstone_and_replays_without_live_record():
    repository = _MemoryRepository((_snapshot(),))
    service = ItemCommandService(repository)
    command = DeleteItemCommand("book-1", "rev-1", "delete-1")

    result = service.delete(command)

    assert result.receipt.deletion == ItemDeletionSnapshot(
        "book-1",
        "rev-1",
        "tombstone-book-1",
    )
    assert "book-1" not in repository.records
    assert service.delete(command).replayed is True
    assert repository.commits == repository.stages == 1


def test_legacy_delete_authority_defaults_to_backward_compatible():
    repository = _MemoryRepository((_snapshot(),))

    result = ItemCommandService(repository).delete(
        DeleteItemCommand("book-1", "rev-1", "delete-default")
    )

    assert result.receipt.action == "delete"
    assert repository.commits == repository.stages == 1


def test_disabled_legacy_delete_refuses_before_repository_or_receipt_replay():
    repository = _MemoryRepository((_snapshot(),))
    command = DeleteItemCommand("book-1", "rev-1", "delete-disabled")
    legacy_result = ItemCommandService(repository).delete(command)
    units_before = tuple(repository.units)

    def fail_repository_access(**_kwargs):
        pytest.fail("disabled legacy delete accessed its repository")

    repository.unit_of_work = fail_repository_access
    with pytest.raises(ConflictError) as caught:
        ItemCommandService(
            repository,
            allow_legacy_delete=False,
        ).delete(command)

    assert caught.value.code == "item_lifecycle_command_required"
    assert caught.value.retryable is False
    assert tuple(repository.units) == units_before
    assert repository.receipts[command.operation_id] == legacy_result.receipt
    assert repository.commits == repository.stages == 1


def test_disabled_legacy_delete_does_not_disable_create_or_update():
    repository = _MemoryRepository()
    service = ItemCommandService(repository, allow_legacy_delete=False)

    created = service.create(CreateItemCommand(_draft(), "create-enabled"))
    updated = service.update(
        UpdateItemCommand(
            created.receipt.item_id,
            created.receipt.after_revision,
            ItemPatch(title="Updated"),
            "update-enabled",
        )
    )

    assert created.receipt.action == "create"
    assert updated.receipt.action == "update"
    assert updated.receipt.item.title == "Updated"
    assert repository.commits == repository.stages == 2


def test_legacy_delete_authority_requires_an_explicit_boolean():
    with pytest.raises(TypeError, match="allow_legacy_delete must be a boolean"):
        ItemCommandService(_MemoryRepository(), allow_legacy_delete=0)


def test_delete_requires_revision_and_current_item_before_staging():
    repository = _MemoryRepository((_snapshot(),))
    service = ItemCommandService(repository)

    with pytest.raises(PreconditionRequiredError):
        service.delete(DeleteItemCommand("book-1", "", "delete-no-revision"))
    with pytest.raises(ConflictError):
        service.delete(DeleteItemCommand("book-1", "rev-old", "delete-stale"))
    missing_repository = _MemoryRepository()
    with pytest.raises(NotFoundError):
        ItemCommandService(missing_repository).delete(
            DeleteItemCommand("book-1", "rev-1", "delete-missing")
        )
    assert repository.stages == repository.commits == 0
    assert missing_repository.stages == missing_repository.commits == 0


def test_command_identity_validation_happens_before_repository_entry():
    repository = _MemoryRepository()
    service = ItemCommandService(repository)

    with pytest.raises(PreconditionRequiredError) as missing:
        service.create(CreateItemCommand(_draft(), ""))
    assert missing.value.code == "operation_id_required"
    with pytest.raises(ValidationError) as bad_item:
        service.delete(DeleteItemCommand("../item", "rev-1", "delete-bad"))
    assert bad_item.value.code == "invalid_item_id"
    with pytest.raises(ValidationError) as wrong_type:
        service.create(object())
    assert wrong_type.value.code == "invalid_item_command"
    assert repository.units == []


def test_repository_create_outputs_are_scope_and_content_checked():
    invalid_id = _MemoryRepository()
    invalid_id.next_item_id = "../escape"
    with pytest.raises(RepositoryError) as allocated:
        ItemCommandService(invalid_id).create(
            CreateItemCommand(_draft(), "bad-allocation")
        )
    assert allocated.value.code == "invalid_allocated_item_id"

    collision = _MemoryRepository((_snapshot("item-created"),))
    with pytest.raises(RepositoryError) as collided:
        ItemCommandService(collision).create(
            CreateItemCommand(_draft(), "collision")
        )
    assert collided.value.code == "allocated_item_id_collision"

    wrong_content = _MemoryRepository()
    wrong_content.stage_snapshot = _snapshot(
        "item-created",
        "rev-2",
        _draft(title="repository changed it"),
    )
    with pytest.raises(RepositoryError) as changed:
        ItemCommandService(wrong_content).create(
            CreateItemCommand(_draft(), "wrong-content")
        )
    assert changed.value.code == "item_repository_content_mismatch"
    assert invalid_id.commits == collision.commits == wrong_content.commits == 0


def test_repository_update_and_delete_outputs_must_match_locked_scope():
    current = _snapshot()
    same_revision = _MemoryRepository((current,))
    same_revision.stage_snapshot = _snapshot(
        "book-1",
        "rev-1",
        _draft(title="new"),
    )
    with pytest.raises(RepositoryError) as unchanged:
        ItemCommandService(same_revision).update(
            UpdateItemCommand(
                "book-1",
                "rev-1",
                ItemPatch(title="new"),
                "same-revision",
            )
        )
    assert unchanged.value.code == "item_revision_not_advanced"

    wrong_deletion = _MemoryRepository((current,))
    wrong_deletion.stage_deletion = ItemDeletionSnapshot(
        "other-item",
        "rev-1",
        "tombstone-other",
    )
    with pytest.raises(RepositoryError) as wrong_scope:
        ItemCommandService(wrong_deletion).delete(
            DeleteItemCommand("book-1", "rev-1", "wrong-deletion")
        )
    assert wrong_scope.value.code == "item_repository_scope_mismatch"
    assert same_revision.commits == wrong_deletion.commits == 0


def test_invalid_repository_receipts_and_failures_never_report_success():
    invalid_receipt = _MemoryRepository()
    invalid_receipt.receipt_override = {"not": "a receipt"}
    with pytest.raises(RepositoryError) as invalid:
        ItemCommandService(invalid_receipt).create(
            CreateItemCommand(_draft(), "invalid-receipt")
        )
    assert invalid.value.code == "invalid_item_mutation_receipt"

    failing = _MemoryRepository()
    failing.fail_commit = RuntimeError("disk unavailable")
    with pytest.raises(RepositoryError) as failure:
        ItemCommandService(failing).create(
            CreateItemCommand(_draft(), "commit-failure")
        )
    assert failure.value.code == "item_repository_unavailable"
    assert failure.value.retryable is True
    assert failure.value.details == {"cause_type": "RuntimeError"}
    assert "disk unavailable" not in str(failure.value.as_dict())
    assert failing.records == {}
    assert failing.receipts == {}
    assert failing.commits == 0
