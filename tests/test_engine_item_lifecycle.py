"""Pure contract tests for recoverable item lifecycle commands."""

from __future__ import annotations

import hashlib
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
from librarytool.engine.item_lifecycle import (
    DeleteItemCommand,
    ItemLifecycleDeletionIndex,
    ItemLifecycleReceipt,
    ItemLifecycleResult,
    ItemLifecycleService,
    ItemLifecycleState,
    ItemTombstoneSnapshot,
    LifecycleItemSnapshot,
    ManagedTreeSnapshot,
    RestoreItemCommand,
    StagedItemRestoration,
)


_DEFAULT = object()


def _item(
    item_id: str = "book-1", revision: str = "item-r1"
) -> LifecycleItemSnapshot:
    return LifecycleItemSnapshot(item_id=item_id, revision=revision)


def _tree(
    item_id: str = "book-1", revision: str = "tree-r1"
) -> ManagedTreeSnapshot:
    return ManagedTreeSnapshot(item_id=item_id, revision=revision)


def _tombstone(
    *,
    tombstone_id: str = "deleted-1",
    revision: str = "tomb-r1",
    state: str = "deleted",
    item_id: str = "book-1",
    deleted_item_revision: str = "item-r1",
    managed_tree_revision: str = "tree-r1",
    restored_item_revision: str = "",
) -> ItemTombstoneSnapshot:
    return ItemTombstoneSnapshot(
        tombstone_id=tombstone_id,
        revision=revision,
        state=state,
        item_id=item_id,
        deleted_item_revision=deleted_item_revision,
        managed_tree_revision=managed_tree_revision,
        restored_item_revision=restored_item_revision,
    )


def _restoration(
    before: ItemTombstoneSnapshot | None = None,
    *,
    item_revision: str = "item-r2",
    tombstone_revision: str = "tomb-r2",
) -> StagedItemRestoration:
    before = before or _tombstone()
    after = _tombstone(
        tombstone_id=before.tombstone_id,
        revision=tombstone_revision,
        state="restored",
        item_id=before.item_id,
        deleted_item_revision=before.deleted_item_revision,
        managed_tree_revision=before.managed_tree_revision,
        restored_item_revision=item_revision,
    )
    return StagedItemRestoration(
        item=_item(before.item_id, item_revision),
        managed_tree=_tree(before.item_id, before.managed_tree_revision),
        tombstone=after,
    )


class FakeLifecycleUnit:
    def __init__(
        self,
        *,
        item=_DEFAULT,
        tree=_DEFAULT,
        tombstone=_DEFAULT,
    ) -> None:
        self.item = _item() if item is _DEFAULT else item
        self.tree = _tree() if tree is _DEFAULT else tree
        self.tombstone = None if tombstone is _DEFAULT else tombstone
        self.receipts: dict[str, ItemLifecycleReceipt] = {}
        self.receipt_value = _DEFAULT
        self.delete_outcome = _DEFAULT
        self.restore_outcome = _DEFAULT
        self.events: list[object] = []
        self.last_restoration: StagedItemRestoration | None = None
        self.commit_error: Exception | None = None

    def receipt(self, operation_id: str):
        self.events.append(("receipt", operation_id))
        if self.receipt_value is not _DEFAULT:
            return self.receipt_value
        return self.receipts.get(operation_id)

    def get_item(self, item_id: str):
        self.events.append(("get_item", item_id))
        return self.item

    def get_managed_tree(self, item_id: str):
        self.events.append(("get_managed_tree", item_id))
        return self.tree

    def get_tombstone(self, tombstone_id: str):
        self.events.append(("get_tombstone", tombstone_id))
        return self.tombstone

    def stage_delete(self, item, managed_tree):
        self.events.append(("stage_delete", item, managed_tree))
        if self.delete_outcome is not _DEFAULT:
            return self.delete_outcome
        return _tombstone(
            item_id=item.item_id,
            deleted_item_revision=item.revision,
            managed_tree_revision=managed_tree.revision,
        )

    def stage_restore(self, tombstone):
        self.events.append(("stage_restore", tombstone))
        if self.restore_outcome is not _DEFAULT:
            outcome = self.restore_outcome
        else:
            outcome = _restoration(tombstone)
        if isinstance(outcome, StagedItemRestoration):
            self.last_restoration = outcome
        return outcome

    def commit(self, receipt: ItemLifecycleReceipt) -> None:
        self.events.append(("commit", receipt))
        if self.commit_error is not None:
            raise self.commit_error
        self.receipts[receipt.operation_id] = receipt
        self.tombstone = receipt.tombstone
        if receipt.action == "delete":
            self.item = None
            self.tree = None
        else:
            assert self.last_restoration is not None
            self.item = self.last_restoration.item
            self.tree = self.last_restoration.managed_tree


class FakeLifecycleRepository:
    def __init__(self, unit: FakeLifecycleUnit | None = None) -> None:
        self.unit = unit or FakeLifecycleUnit()
        self.operations: list[str] = []
        self.inspections: list[str] = []
        self.inspect_value = _DEFAULT
        self.tombstone_reads: list[str] = []
        self.tombstone_value = _DEFAULT
        self.tombstone_list_reads = 0
        self.tombstone_list_value = _DEFAULT
        self.tombstone_guard_depth = 0
        self.error: Exception | None = None

    def inspect(self, item_id: str):
        self.inspections.append(item_id)
        if self.error is not None:
            raise self.error
        if self.inspect_value is not _DEFAULT:
            return self.inspect_value
        if self.unit.item is None:
            return None
        return ItemLifecycleState(
            item=self.unit.item,
            managed_tree=self.unit.tree,
        )

    def get_tombstone(self, tombstone_id: str):
        self.tombstone_reads.append(tombstone_id)
        if self.error is not None:
            raise self.error
        if self.tombstone_value is not _DEFAULT:
            return self.tombstone_value
        tombstone = self.unit.tombstone
        if tombstone is not None and tombstone.tombstone_id == tombstone_id:
            return tombstone
        return None

    def list_tombstones(self):
        self.tombstone_list_reads += 1
        if self.error is not None:
            raise self.error
        if self.tombstone_list_value is not _DEFAULT:
            return self.tombstone_list_value
        return () if self.unit.tombstone is None else (self.unit.tombstone,)

    @contextmanager
    def tombstone_read_guard(self):
        self.tombstone_guard_depth += 1
        try:
            yield self.list_tombstones()
        finally:
            self.tombstone_guard_depth -= 1

    @contextmanager
    def unit_of_work(self, *, operation_id: str):
        self.operations.append(operation_id)
        if self.error is not None:
            raise self.error
        yield self.unit


def _delete_command(**changes) -> DeleteItemCommand:
    values = {
        "item_id": "book-1",
        "expected_item_revision": "item-r1",
        "expected_managed_tree_revision": "tree-r1",
        "operation_id": "delete-op-1",
    }
    values.update(changes)
    return DeleteItemCommand(**values)


def _restore_command(**changes) -> RestoreItemCommand:
    values = {
        "tombstone_id": "deleted-1",
        "expected_tombstone_revision": "tomb-r1",
        "operation_id": "restore-op-1",
    }
    values.update(changes)
    return RestoreItemCommand(**values)


def _delete_receipt(
    *, operation_id: str = "delete-op-1", command_sha256: str | None = None
) -> ItemLifecycleReceipt:
    command_sha256 = command_sha256 or hashlib.sha256(b"delete").hexdigest()
    tombstone = _tombstone()
    return ItemLifecycleReceipt(
        action="delete",
        operation_id=operation_id,
        command_sha256=command_sha256,
        item_id="book-1",
        deleted_item_revision="item-r1",
        restored_item_revision="",
        managed_tree_revision="tree-r1",
        tombstone_before_revision="",
        tombstone=tombstone,
    )


def _restore_receipt(
    *, operation_id: str = "restore-op-1", command_sha256: str | None = None
) -> ItemLifecycleReceipt:
    command_sha256 = command_sha256 or hashlib.sha256(b"restore").hexdigest()
    restored = _restoration()
    return ItemLifecycleReceipt(
        action="restore",
        operation_id=operation_id,
        command_sha256=command_sha256,
        item_id="book-1",
        deleted_item_revision="item-r1",
        restored_item_revision="item-r2",
        managed_tree_revision="tree-r1",
        tombstone_before_revision="tomb-r1",
        tombstone=restored.tombstone,
    )


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (_delete_command, "item_id"),
        (_delete_command, "expected_item_revision"),
        (_delete_command, "expected_managed_tree_revision"),
        (_delete_command, "operation_id"),
        (_restore_command, "tombstone_id"),
        (_restore_command, "expected_tombstone_revision"),
        (_restore_command, "operation_id"),
    ],
)
def test_command_dtos_require_string_fields(factory, field) -> None:
    with pytest.raises(TypeError, match=field):
        factory(**{field: None})


@pytest.mark.parametrize("snapshot_factory", [_item, _tree])
def test_item_and_managed_tree_snapshots_round_trip(snapshot_factory) -> None:
    snapshot = snapshot_factory()

    assert type(snapshot).from_dict(snapshot.as_dict()) == snapshot
    with pytest.raises(ValueError, match="fields"):
        type(snapshot).from_dict({**snapshot.as_dict(), "extra": True})
    with pytest.raises(ValueError, match="portable identifier"):
        snapshot_factory(item_id="../escape")
    with pytest.raises(ValueError, match="revision"):
        snapshot_factory(revision="bad revision")


def test_lifecycle_state_round_trips_and_requires_one_identity() -> None:
    state = ItemLifecycleState(item=_item(), managed_tree=_tree())

    assert ItemLifecycleState.from_dict(state.as_dict()) == state
    with pytest.raises(ValueError, match="fields"):
        ItemLifecycleState.from_dict({**state.as_dict(), "extra": True})
    with pytest.raises(ValueError, match="identities"):
        ItemLifecycleState(item=_item(), managed_tree=_tree("other"))


def test_inspect_returns_coherent_delete_preconditions() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)

    state = service.inspect("book-1")

    assert state == ItemLifecycleState(item=_item(), managed_tree=_tree())
    assert repository.inspections == ["book-1"]
    assert repository.operations == []


def test_inspect_validates_identity_absence_and_repository_outcome() -> None:
    repository = FakeLifecycleRepository(FakeLifecycleUnit(item=None, tree=None))
    service = ItemLifecycleService(repository)

    with pytest.raises(NotFoundError) as missing:
        service.inspect("book-1")
    assert missing.value.code == "item_not_found"

    repository.inspect_value = object()
    with pytest.raises(RepositoryError) as malformed:
        service.inspect("book-1")
    assert malformed.value.code == "invalid_item_lifecycle_state"

    repository.inspect_value = ItemLifecycleState(
        item=_item("other"),
        managed_tree=_tree("other"),
    )
    with pytest.raises(RepositoryError) as wrong_item:
        service.inspect("book-1")
    assert wrong_item.value.code == "invalid_item_lifecycle_state"


def test_inspect_validates_input_and_wraps_unexpected_repository_failure() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)

    with pytest.raises(ValidationError) as invalid:
        service.inspect("../escape")
    assert invalid.value.code == "invalid_item_id"
    assert repository.inspections == []

    repository.error = OSError("offline")
    with pytest.raises(RepositoryError) as unavailable:
        service.inspect("book-1")
    assert unavailable.value.code == "item_lifecycle_repository_unavailable"
    assert unavailable.value.retryable is True


def test_get_tombstone_returns_only_the_requested_public_snapshot() -> None:
    tombstone = _tombstone()
    repository = FakeLifecycleRepository(
        FakeLifecycleUnit(tombstone=tombstone)
    )

    result = ItemLifecycleService(repository).get_tombstone("deleted-1")

    assert result == tombstone
    assert result.as_dict() == tombstone.as_dict()
    assert repository.tombstone_reads == ["deleted-1"]
    assert repository.operations == []


def test_get_tombstone_validates_input_absence_and_repository_result() -> None:
    repository = FakeLifecycleRepository(FakeLifecycleUnit(tombstone=None))
    service = ItemLifecycleService(repository)

    with pytest.raises(ValidationError) as invalid:
        service.get_tombstone("../escape")
    assert invalid.value.code == "invalid_tombstone_id"
    assert repository.tombstone_reads == []

    with pytest.raises(NotFoundError) as missing:
        service.get_tombstone("deleted-1")
    assert missing.value.code == "item_tombstone_not_found"

    repository.tombstone_value = object()
    with pytest.raises(RepositoryError) as malformed:
        service.get_tombstone("deleted-1")
    assert malformed.value.code == "invalid_item_tombstone"

    repository.tombstone_value = _tombstone(tombstone_id="deleted-other")
    with pytest.raises(RepositoryError) as wrong:
        service.get_tombstone("deleted-1")
    assert wrong.value.code == "invalid_item_tombstone"


def test_get_tombstone_wraps_unexpected_repository_failure() -> None:
    repository = FakeLifecycleRepository()
    repository.error = OSError("C:/private/tombstones unavailable")

    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(repository).get_tombstone("deleted-1")

    assert caught.value.code == "item_lifecycle_repository_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {"cause_type": "OSError"}
    assert "private" not in str(caught.value.as_dict())


def test_list_tombstones_is_public_deterministic_and_tracks_active_ids() -> None:
    restored = _restoration(
        _tombstone(
            tombstone_id="deleted-z",
            item_id="book-z",
        ),
        item_revision="item-z-r2",
        tombstone_revision="tomb-z-r2",
    ).tombstone
    deleted_b = _tombstone(
        tombstone_id="deleted-b",
        item_id="book-b",
        deleted_item_revision="item-b-r1",
    )
    deleted_a = _tombstone(
        tombstone_id="deleted-a",
        item_id="Book-A",
        deleted_item_revision="item-a-r1",
    )
    repository = FakeLifecycleRepository()
    repository.tombstone_list_value = (restored, deleted_b, deleted_a)
    service = ItemLifecycleService(repository)

    listed = service.list_tombstones()

    assert listed == (deleted_a, deleted_b, restored)
    assert all(isinstance(value, ItemTombstoneSnapshot) for value in listed)
    assert service.active_deleted_item_ids() == ("Book-A", "book-b")
    assert repository.tombstone_list_reads == 2
    assert repository.operations == []


def test_deletion_index_guard_is_lock_safe_and_denies_case_aliases() -> None:
    deleted = _tombstone(item_id="Book-One")
    restored = _restoration(
        _tombstone(tombstone_id="deleted-two", item_id="book-two")
    ).tombstone
    repository = FakeLifecycleRepository()
    repository.tombstone_list_value = (restored, deleted)
    service = ItemLifecycleService(repository)

    with service.deletion_index_guard() as index:
        assert isinstance(index, ItemLifecycleDeletionIndex)
        assert repository.tombstone_guard_depth == 1
        assert index.active_item_ids == ("Book-One",)
        assert index.allows("book-one") is False
        assert index.allows("BOOK-ONE") is False
        assert index.allows("book-two") is True
        with pytest.raises(ValueError, match="portable identifier"):
            index.allows("../book-one")

    assert repository.tombstone_guard_depth == 0
    assert repository.tombstone_list_reads == 1


def test_deletion_index_guard_preserves_caller_failures_and_wraps_setup() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)

    with pytest.raises(RuntimeError, match="sync failed"):
        with service.deletion_index_guard():
            raise RuntimeError("sync failed")
    assert repository.tombstone_guard_depth == 0

    repository.error = OSError("C:/private/tombstone index unavailable")
    with pytest.raises(RepositoryError) as caught:
        with service.deletion_index_guard():
            pytest.fail("an unavailable index must not be yielded")
    assert caught.value.code == "item_lifecycle_repository_unavailable"
    assert caught.value.details == {"cause_type": "OSError"}
    assert "private" not in str(caught.value.as_dict())


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not-an-index",
        (_tombstone(), object()),
        (_tombstone(), _tombstone()),
        (
            _tombstone(tombstone_id="Deleted-1"),
            _tombstone(tombstone_id="deleted-1"),
        ),
        (
            _tombstone(tombstone_id="deleted-a", item_id="Book-1"),
            _tombstone(tombstone_id="deleted-b", item_id="book-1"),
        ),
    ],
)
def test_list_tombstones_rejects_invalid_duplicates_and_active_aliases(
    value,
) -> None:
    repository = FakeLifecycleRepository()
    repository.tombstone_list_value = value

    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(repository).list_tombstones()

    assert caught.value.code == "invalid_item_tombstone_index"


def test_list_tombstones_wraps_iteration_and_repository_failures() -> None:
    repository = FakeLifecycleRepository()

    def broken_index():
        yield _tombstone()
        raise OSError("C:/private/index")

    repository.tombstone_list_value = broken_index()
    with pytest.raises(RepositoryError) as iteration:
        ItemLifecycleService(repository).list_tombstones()
    assert iteration.value.code == "item_lifecycle_repository_unavailable"
    assert "private" not in str(iteration.value.as_dict())

    repository.tombstone_list_value = _DEFAULT
    repository.error = OSError("offline")
    with pytest.raises(RepositoryError) as unavailable:
        ItemLifecycleService(repository).active_deleted_item_ids()
    assert unavailable.value.code == "item_lifecycle_repository_unavailable"


def test_tombstone_round_trip_and_state_invariants() -> None:
    deleted = _tombstone()
    restored = _restoration(deleted).tombstone

    assert ItemTombstoneSnapshot.from_dict(deleted.as_dict()) == deleted
    assert ItemTombstoneSnapshot.from_dict(restored.as_dict()) == restored
    with pytest.raises(ValueError, match="cannot have"):
        _tombstone(restored_item_revision="item-r2")
    with pytest.raises(ValueError, match="requires a new"):
        _tombstone(state="restored")
    with pytest.raises(ValueError, match="requires a new"):
        _tombstone(state="restored", restored_item_revision="item-r1")
    with pytest.raises(ValueError, match="state"):
        _tombstone(state="forgotten")


def test_staged_restoration_requires_one_consistent_aggregate() -> None:
    valid = _restoration()

    assert valid.item.revision == valid.tombstone.restored_item_revision
    with pytest.raises(ValueError, match="identities"):
        StagedItemRestoration(
            item=_item("other", "item-r2"),
            managed_tree=valid.managed_tree,
            tombstone=valid.tombstone,
        )
    with pytest.raises(ValueError, match="restored tombstone"):
        StagedItemRestoration(
            item=_item("book-1", "item-r2"),
            managed_tree=_tree(),
            tombstone=_tombstone(),
        )


@pytest.mark.parametrize("receipt", [_delete_receipt(), _restore_receipt()])
def test_receipts_round_trip_but_public_results_hide_command_hash(receipt) -> None:
    stored = receipt.as_dict()
    public = receipt.as_public_dict()
    result = ItemLifecycleResult(receipt).as_dict()

    assert ItemLifecycleReceipt.from_dict(stored) == receipt
    assert stored["command_sha256"] == receipt.command_sha256
    assert "command_sha256" not in public
    assert "command_sha256" not in result["receipt"]
    assert result["replayed"] is False
    with pytest.raises(ValueError, match="fields"):
        ItemLifecycleReceipt.from_dict({**stored, "private_path": "C:/secret"})


@pytest.mark.parametrize(
    "changes",
    [
        {"command_sha256": "ABC"},
        {"restored_item_revision": "item-r2"},
        {"tombstone_before_revision": "tomb-r0"},
        {"tombstone": _restoration().tombstone},
    ],
)
def test_delete_receipt_rejects_inconsistent_state(changes) -> None:
    values = _delete_receipt().as_dict()
    values.update(changes)
    if "tombstone" not in changes:
        values["tombstone"] = _delete_receipt().tombstone
    with pytest.raises(ValueError):
        ItemLifecycleReceipt(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"restored_item_revision": ""},
        {"restored_item_revision": "item-r1"},
        {"tombstone_before_revision": ""},
        {"tombstone_before_revision": "tomb-r2"},
        {"tombstone": _tombstone()},
    ],
)
def test_restore_receipt_rejects_inconsistent_state(changes) -> None:
    values = _restore_receipt().as_dict()
    values.update(changes)
    if "tombstone" not in changes:
        values["tombstone"] = _restore_receipt().tombstone
    with pytest.raises(ValueError):
        ItemLifecycleReceipt(**values)


def test_delete_stages_item_tree_tombstone_and_receipt_once() -> None:
    repository = FakeLifecycleRepository()
    result = ItemLifecycleService(repository).delete(_delete_command())

    assert result.replayed is False
    assert result.receipt.action == "delete"
    assert result.receipt.item_id == "book-1"
    assert result.receipt.deleted_item_revision == "item-r1"
    assert result.receipt.managed_tree_revision == "tree-r1"
    assert result.receipt.tombstone.state == "deleted"
    assert repository.operations == ["delete-op-1"]
    assert [event[0] for event in repository.unit.events] == [
        "receipt",
        "get_item",
        "get_managed_tree",
        "stage_delete",
        "commit",
    ]
    assert repository.unit.item is None
    assert repository.unit.tree is None


def test_delete_accepts_a_logical_empty_tree_without_a_physical_directory() -> None:
    """The adapter maps an absent directory to the live item's empty set."""

    unit = FakeLifecycleUnit(tree=_tree(revision="tree-empty-v1"))
    repository = FakeLifecycleRepository(unit)

    result = ItemLifecycleService(repository).delete(
        _delete_command(expected_managed_tree_revision="tree-empty-v1")
    )

    assert result.receipt.managed_tree_revision == "tree-empty-v1"
    assert result.receipt.tombstone.managed_tree_revision == "tree-empty-v1"
    assert unit.item is None
    assert unit.tree is None


def test_delete_exact_retry_replays_before_reading_changed_state() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)
    first = service.delete(_delete_command())
    repository.unit.events.clear()

    second = service.delete(_delete_command())

    assert second == ItemLifecycleResult(first.receipt, replayed=True)
    assert repository.unit.events == [("receipt", "delete-op-1")]


def test_delete_operation_id_cannot_be_reused_for_different_command() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)
    service.delete(_delete_command())

    with pytest.raises(ConflictError) as caught:
        service.delete(
            _delete_command(expected_managed_tree_revision="tree-other")
        )

    assert caught.value.code == "operation_id_conflict"


@pytest.mark.parametrize(
    ("changes", "error_type", "code"),
    [
        ({"item_id": ""}, ValidationError, "item_id_required"),
        ({"item_id": "../bad"}, ValidationError, "invalid_item_id"),
        (
            {"expected_item_revision": ""},
            PreconditionRequiredError,
            "item_revision_required",
        ),
        (
            {"expected_item_revision": "bad revision"},
            ValidationError,
            "invalid_item_revision",
        ),
        (
            {"expected_managed_tree_revision": ""},
            PreconditionRequiredError,
            "managed_tree_revision_required",
        ),
        (
            {"expected_managed_tree_revision": '"bad"'},
            ValidationError,
            "invalid_managed_tree_revision",
        ),
        (
            {"operation_id": ""},
            PreconditionRequiredError,
            "operation_id_required",
        ),
        ({"operation_id": "not portable"}, ValidationError, "invalid_operation_id"),
    ],
)
def test_delete_validates_transport_preconditions(changes, error_type, code) -> None:
    with pytest.raises(error_type) as caught:
        ItemLifecycleService(FakeLifecycleRepository()).delete(
            _delete_command(**changes)
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("unit", "code"),
    [
        (FakeLifecycleUnit(item=None), "item_not_found"),
        (FakeLifecycleUnit(item=_item("other")), "lifecycle_repository_scope_mismatch"),
        (FakeLifecycleUnit(item=_item(revision="item-r2")), "item_revision_conflict"),
        (FakeLifecycleUnit(tree=None), "managed_tree_not_found"),
        (FakeLifecycleUnit(tree=_tree("other")), "lifecycle_repository_scope_mismatch"),
        (
            FakeLifecycleUnit(tree=_tree(revision="tree-r2")),
            "managed_tree_revision_conflict",
        ),
    ],
)
def test_delete_requires_matching_item_and_managed_tree_cas(unit, code) -> None:
    with pytest.raises((ConflictError, NotFoundError, RepositoryError)) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).delete(
            _delete_command()
        )
    assert caught.value.code == code
    assert not any(event[0] == "commit" for event in unit.events)


@pytest.mark.parametrize(
    "outcome",
    [
        object(),
        _tombstone(state="deleted", item_id="other"),
        _tombstone(state="deleted", deleted_item_revision="item-other"),
        _tombstone(state="deleted", managed_tree_revision="tree-other"),
        _restoration().tombstone,
    ],
)
def test_delete_rejects_invalid_repository_outcome(outcome) -> None:
    unit = FakeLifecycleUnit()
    unit.delete_outcome = outcome

    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).delete(
            _delete_command()
        )

    assert caught.value.code in {
        "invalid_item_tombstone",
        "lifecycle_repository_content_mismatch",
    }
    assert not any(event[0] == "commit" for event in unit.events)


def test_delete_rejects_invalid_or_inconsistent_replay_receipt() -> None:
    unit = FakeLifecycleUnit()
    unit.receipt_value = object()
    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).delete(
            _delete_command()
        )
    assert caught.value.code == "invalid_item_lifecycle_receipt"

    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)
    receipt = service.delete(_delete_command()).receipt
    malformed_tombstone = _tombstone(
        deleted_item_revision="item-other",
        managed_tree_revision="tree-r1",
    )
    unit = FakeLifecycleUnit()
    unit.receipt_value = ItemLifecycleReceipt(
        action="delete",
        operation_id=receipt.operation_id,
        command_sha256=receipt.command_sha256,
        item_id="book-1",
        deleted_item_revision="item-other",
        restored_item_revision="",
        managed_tree_revision="tree-r1",
        tombstone_before_revision="",
        tombstone=malformed_tombstone,
    )
    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).delete(
            _delete_command()
        )
    assert caught.value.code == "invalid_item_lifecycle_receipt"


def test_restore_stages_new_item_revision_tree_and_tombstone_transition() -> None:
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=_tombstone())
    repository = FakeLifecycleRepository(unit)

    result = ItemLifecycleService(repository).restore(_restore_command())

    assert result.replayed is False
    assert result.receipt.action == "restore"
    assert result.receipt.deleted_item_revision == "item-r1"
    assert result.receipt.restored_item_revision == "item-r2"
    assert result.receipt.managed_tree_revision == "tree-r1"
    assert result.receipt.tombstone_before_revision == "tomb-r1"
    assert result.receipt.tombstone.state == "restored"
    assert [event[0] for event in unit.events] == [
        "receipt",
        "get_tombstone",
        "get_item",
        "get_managed_tree",
        "stage_restore",
        "commit",
    ]
    assert unit.item == _item(revision="item-r2")
    assert unit.tree == _tree()


def test_restore_accepts_a_logical_empty_tree_without_materializing_a_directory() -> None:
    """A staged empty owned-asset set uses the ordinary snapshot schema."""

    tombstone = _tombstone(managed_tree_revision="tree-empty-v1")
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=tombstone)
    repository = FakeLifecycleRepository(unit)

    result = ItemLifecycleService(repository).restore(_restore_command())

    assert result.receipt.managed_tree_revision == "tree-empty-v1"
    assert unit.tree == _tree(revision="tree-empty-v1")


def test_delete_then_restore_is_a_complete_recoverable_lifecycle() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)
    deletion = service.delete(_delete_command())

    restoration = service.restore(
        _restore_command(
            tombstone_id=deletion.receipt.tombstone.tombstone_id,
            expected_tombstone_revision=deletion.receipt.tombstone.revision,
        )
    )

    assert restoration.receipt.item_id == deletion.receipt.item_id
    assert restoration.receipt.managed_tree_revision == "tree-r1"
    assert repository.unit.item == _item(revision="item-r2")
    assert repository.unit.tree == _tree()


def test_restore_exact_retry_replays_before_inspecting_restored_state() -> None:
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=_tombstone())
    repository = FakeLifecycleRepository(unit)
    service = ItemLifecycleService(repository)
    first = service.restore(_restore_command())
    unit.events.clear()

    second = service.restore(_restore_command())

    assert second == ItemLifecycleResult(first.receipt, replayed=True)
    assert unit.events == [("receipt", "restore-op-1")]


def test_restore_operation_id_cannot_be_reused_for_another_tombstone_revision() -> None:
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=_tombstone())
    service = ItemLifecycleService(FakeLifecycleRepository(unit))
    service.restore(_restore_command())

    with pytest.raises(ConflictError) as caught:
        service.restore(_restore_command(expected_tombstone_revision="tomb-r0"))

    assert caught.value.code == "operation_id_conflict"


@pytest.mark.parametrize(
    ("changes", "error_type", "code"),
    [
        ({"tombstone_id": ""}, ValidationError, "tombstone_id_required"),
        ({"tombstone_id": "../bad"}, ValidationError, "invalid_tombstone_id"),
        (
            {"expected_tombstone_revision": ""},
            PreconditionRequiredError,
            "tombstone_revision_required",
        ),
        (
            {"expected_tombstone_revision": "bad revision"},
            ValidationError,
            "invalid_tombstone_revision",
        ),
        (
            {"operation_id": ""},
            PreconditionRequiredError,
            "operation_id_required",
        ),
        ({"operation_id": "bad operation"}, ValidationError, "invalid_operation_id"),
    ],
)
def test_restore_validates_transport_preconditions(changes, error_type, code) -> None:
    with pytest.raises(error_type) as caught:
        ItemLifecycleService(FakeLifecycleRepository()).restore(
            _restore_command(**changes)
        )
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("unit", "code"),
    [
        (
            FakeLifecycleUnit(item=None, tree=None, tombstone=None),
            "item_tombstone_not_found",
        ),
        (
            FakeLifecycleUnit(
                item=None,
                tree=None,
                tombstone=_tombstone(tombstone_id="other"),
            ),
            "lifecycle_repository_scope_mismatch",
        ),
        (
            FakeLifecycleUnit(
                item=None,
                tree=None,
                tombstone=_tombstone(revision="tomb-r2"),
            ),
            "tombstone_revision_conflict",
        ),
        (
            FakeLifecycleUnit(
                item=None,
                tree=None,
                tombstone=_restoration().tombstone,
            ),
            "tombstone_state_conflict",
        ),
        (
            FakeLifecycleUnit(tree=None, tombstone=_tombstone()),
            "item_restore_collision",
        ),
        (
            FakeLifecycleUnit(item=None, tombstone=_tombstone()),
            "managed_tree_restore_collision",
        ),
    ],
)
def test_restore_requires_live_deleted_tombstone_and_empty_target(unit, code) -> None:
    command = _restore_command()
    if code == "tombstone_state_conflict":
        command = _restore_command(expected_tombstone_revision="tomb-r2")
    with pytest.raises((ConflictError, NotFoundError, RepositoryError)) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).restore(command)
    assert caught.value.code == code
    assert not any(event[0] == "commit" for event in unit.events)


@pytest.mark.parametrize(
    "outcome",
    [
        object(),
        _restoration(
            _tombstone(deleted_item_revision="item-old"),
            item_revision="item-r1",
        ),
        _restoration(tombstone_revision="tomb-r1"),
        _restoration(
            _tombstone(
                item_id="other",
                tombstone_id="deleted-other",
            )
        ),
    ],
)
def test_restore_rejects_invalid_repository_outcome(outcome) -> None:
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=_tombstone())
    unit.restore_outcome = outcome

    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).restore(
            _restore_command()
        )

    assert caught.value.code in {
        "invalid_item_restoration",
        "lifecycle_repository_content_mismatch",
    }
    assert not any(event[0] == "commit" for event in unit.events)


def test_restore_rejects_inconsistent_replay_receipt() -> None:
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=_tombstone())
    repository = FakeLifecycleRepository(unit)
    receipt = ItemLifecycleService(repository).restore(_restore_command()).receipt
    other = _tombstone(
        tombstone_id="deleted-other",
        revision="tomb-r2",
        state="restored",
        restored_item_revision="item-r2",
    )
    unit = FakeLifecycleUnit(item=None, tree=None, tombstone=_tombstone())
    unit.receipt_value = ItemLifecycleReceipt(
        action="restore",
        operation_id=receipt.operation_id,
        command_sha256=receipt.command_sha256,
        item_id="book-1",
        deleted_item_revision="item-r1",
        restored_item_revision="item-r2",
        managed_tree_revision="tree-r1",
        tombstone_before_revision="tomb-r1",
        tombstone=other,
    )

    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).restore(
            _restore_command()
        )

    assert caught.value.code == "invalid_item_lifecycle_receipt"


def test_repository_exceptions_are_sanitized_and_marked_retryable() -> None:
    repository = FakeLifecycleRepository()
    repository.error = OSError("C:/private/library is unavailable")

    with pytest.raises(RepositoryError) as caught:
        ItemLifecycleService(repository).delete(_delete_command())

    assert caught.value.code == "item_lifecycle_repository_unavailable"
    assert caught.value.retryable is True
    assert caught.value.details == {"cause_type": "OSError"}
    assert "private" not in caught.value.message


def test_engine_errors_from_commit_are_preserved() -> None:
    unit = FakeLifecycleUnit()
    unit.commit_error = ConflictError("locked", code="adapter_conflict")

    with pytest.raises(ConflictError) as caught:
        ItemLifecycleService(FakeLifecycleRepository(unit)).delete(
            _delete_command()
        )

    assert caught.value.code == "adapter_conflict"


def test_command_fingerprints_are_canonical_and_bind_every_cas_precondition() -> None:
    repository = FakeLifecycleRepository()
    receipt = ItemLifecycleService(repository).delete(_delete_command()).receipt
    expected_payload = {
        "action": "delete",
        "item_id": "book-1",
        "expected_item_revision": "item-r1",
        "expected_managed_tree_revision": "tree-r1",
    }
    expected = hashlib.sha256(
        json.dumps(
            expected_payload, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()

    assert receipt.command_sha256 == expected


def test_service_rejects_wrong_command_types_before_opening_repository() -> None:
    repository = FakeLifecycleRepository()
    service = ItemLifecycleService(repository)

    with pytest.raises(ValidationError) as delete_error:
        service.delete(_restore_command())
    with pytest.raises(ValidationError) as restore_error:
        service.restore(_delete_command())

    assert delete_error.value.code == "invalid_item_lifecycle_command"
    assert restore_error.value.code == "invalid_item_lifecycle_command"
    assert repository.operations == []
