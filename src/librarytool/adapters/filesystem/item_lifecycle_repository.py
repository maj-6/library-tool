"""Recoverable filesystem persistence for item lifecycle commands.

This adapter composes the catalogue repository's lock and raw-record seams so
one lifecycle command observes a stable catalogue snapshot from receipt lookup
through commit.  The public engine tombstone deliberately contains no storage
paths or catalogue data; those details live in a private, versioned envelope.

Only an item's engine-owned entry directory is moved.  Representation files
reached through catalogue locators are not inspected and can therefore never
become lifecycle transaction targets.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.errors import EngineError, RepositoryError
from ...engine.item_lifecycle import (
    ItemLifecycleReceipt,
    ItemLifecycleState,
    ItemTombstoneSnapshot,
    LifecycleItemSnapshot,
    ManagedTreeSnapshot,
    StagedItemRestoration,
)
from .item_command_repository import (
    FilesystemItemCommandRepository,
    FilesystemItemCommandUnitOfWork,
    _json_bytes,
    _read_json,
    _safe_cause,
    _strict_plain,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    WriteSetError,
    _fingerprint_tree,
    _is_redirecting_path,
)


EntryDirectoryResolver: TypeAlias = Callable[[str], Path]
RestoredRecordAdvancer: TypeAlias = Callable[
    [str, Mapping[str, Any]], Mapping[str, Any]
]
LifecycleLockFactory: TypeAlias = Callable[[], ContextManager[Any]]
ItemDeletionGuardFactory: TypeAlias = Callable[[str], ContextManager[Any]]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FILE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/item-lifecycle-v1")
_ENVELOPE_ROOT = PurePosixPath(
    ".engine/lifecycle/item-tombstones-v1/envelopes"
)
_TREE_ROOT = PurePosixPath(".engine/lifecycle/item-tombstones-v1/trees")
_ENVELOPE_SCHEMA = "librarytool.item-lifecycle-tombstone/1"
_PHYSICAL_TREE_PREFIX = "managed-tree-v1-"
EMPTY_MANAGED_TREE_REVISION = (
    "managed-tree-empty-v1-"
    + hashlib.sha256(b"librarytool-managed-empty-tree-v1").hexdigest()
)


class FilesystemItemLifecycleRepository:
    """Open lifecycle units inside the catalogue repository's lock domain.

    ``advance_restored_record`` receives a detached copy of the exact private
    catalogue record captured at deletion.  It must preserve all storage-only
    fields while advancing the host's item revision token.  The shared item
    codec validates the returned record before it can be staged.

    ``deletion_guard_for`` is an optional host policy port.  Its context is
    entered after the broad lifecycle locks and remains held through commit or
    rollback.  A host can therefore check for active jobs while holding the
    same job-registry gate that job registration uses, eliminating the race
    between a snapshot-only check and tree publication.
    Omission is appropriate only for a host with no background-job subsystem;
    a host that installs jobs must bind the shared job manager's guard.

    ``lock_context_for`` is the broad host mutation gate and must include the
    catalogue lock expected by ``item_repository.open_locked_unit``.  Lock
    order is workspace lease, broad item/page gate, then deletion/job guard.
    It must not be the item repository's ordinary ``unit_of_work`` context,
    which would acquire the non-reentrant catalogue lock a second time.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_repository: FilesystemItemCommandRepository,
        entry_directory_for: EntryDirectoryResolver,
        advance_restored_record: RestoredRecordAdvancer,
        lock_context_for: LifecycleLockFactory,
        deletion_guard_for: ItemDeletionGuardFactory | None = None,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not isinstance(item_repository, FilesystemItemCommandRepository):
            raise TypeError(
                "item_repository must be a FilesystemItemCommandRepository"
            )
        for callback, name in (
            (entry_directory_for, "entry_directory_for"),
            (advance_restored_record, "advance_restored_record"),
            (lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if deletion_guard_for is not None and not callable(
            deletion_guard_for
        ):
            raise TypeError("deletion_guard_for must be callable")
        workspace_probe = item_repository.target_path(
            ".engine/lifecycle/workspace-identity",
            artifact="workspace_identity",
        )
        expected_probe = (
            write_set.root
            / ".engine"
            / "lifecycle"
            / "workspace-identity"
        )
        if workspace_probe != expected_probe:
            raise ValueError(
                "the item repository belongs to another workspace"
            )

        self._write_set = write_set
        self._items = item_repository
        self._entry_directory_for = entry_directory_for
        self._advance_restored_record = advance_restored_record
        self._lock_context_for = lock_context_for
        self._deletion_guard_for = deletion_guard_for

    def inspect(self, item_id: str) -> ItemLifecycleState | None:
        """Read live item and tree revisions under lifecycle isolation.

        An orphan entry tree is intentionally not reported as live state.  It
        remains visible to restore collision checks performed by a unit of
        work, but callers cannot mistake it for a catalogue item.
        """

        if not isinstance(item_id, str):
            raise RepositoryError(
                "item_id must be a string",
                code="invalid_item_lifecycle_identity",
                details={"field": "item_id"},
            )
        operation_id = "inspect-" + hashlib.sha256(
            item_id.encode("utf-8")
        ).hexdigest()[:32]
        with self._locked_item_unit(operation_id=operation_id) as item_unit:
            unit = self._new_unit(
                operation_id=operation_id, item_unit=item_unit
            )
            try:
                item = unit.get_item(item_id)
                if item is None:
                    return None
                tree = unit.get_managed_tree(item_id)
                if tree is None:
                    raise RepositoryError(
                        "the live item has no logical managed tree",
                        code="invalid_item_lifecycle_state",
                    )
                return ItemLifecycleState(item, tree)
            finally:
                unit.close()

    def get_tombstone(
        self, tombstone_id: str
    ) -> ItemTombstoneSnapshot | None:
        """Read one validated public tombstone under lifecycle isolation."""

        if not isinstance(tombstone_id, str):
            raise RepositoryError(
                "tombstone_id must be a string",
                code="invalid_item_lifecycle_identity",
                details={"field": "tombstone_id"},
            )
        operation_id = "read-tombstone-" + hashlib.sha256(
            tombstone_id.encode("utf-8")
        ).hexdigest()[:32]
        with self._locked_item_unit(operation_id=operation_id) as item_unit:
            unit = self._new_unit(
                operation_id=operation_id, item_unit=item_unit
            )
            try:
                return unit.get_tombstone(tombstone_id)
            finally:
                unit.close()

    def list_tombstones(self) -> tuple[ItemTombstoneSnapshot, ...]:
        """Read all public tombstones under one lifecycle isolation scope."""

        operation_id = "read-tombstone-index"
        with self._locked_item_unit(operation_id=operation_id) as item_unit:
            unit = self._new_unit(
                operation_id=operation_id, item_unit=item_unit
            )
            try:
                return unit.list_tombstones()
            finally:
                unit.close()

    @contextmanager
    def tombstone_read_guard(
        self,
    ) -> Iterator[tuple[ItemTombstoneSnapshot, ...]]:
        """Yield one stable public index while lifecycle isolation is held.

        Bulk writers use this scope to apply a tombstone policy without
        re-entering the non-reentrant host catalogue lock for every item.
        """

        operation_id = "guard-tombstone-index"
        with self._locked_item_unit(operation_id=operation_id) as item_unit:
            unit = self._new_unit(
                operation_id=operation_id, item_unit=item_unit
            )
            try:
                tombstones = unit.list_tombstones()
                yield tombstones
            finally:
                unit.close()

    @contextmanager
    def unit_of_work(
        self, *, operation_id: str
    ) -> Iterator["FilesystemItemLifecycleUnitOfWork"]:
        with self._locked_item_unit(operation_id=operation_id) as item_unit:
            unit = self._new_unit(
                operation_id=operation_id, item_unit=item_unit
            )
            try:
                yield unit
            finally:
                unit.close()

    @contextmanager
    def _locked_item_unit(
        self, *, operation_id: str
    ) -> Iterator[FilesystemItemCommandUnitOfWork]:
        """Take workspace, broad host, then item snapshot locks exactly once."""

        stack = ExitStack()
        unit: FilesystemItemCommandUnitOfWork | None = None
        try:
            try:
                stack.enter_context(self._write_set.workspace_lease())
                stack.enter_context(self._lock_context_for())
                unit = self._items.open_locked_unit(
                    operation_id=operation_id
                )
            except WriteSetError as exc:
                raise _safe_cause(
                    exc,
                    code=exc.code,
                    message="the item lifecycle workspace is unavailable",
                ) from exc
            except EngineError:
                raise
            except Exception as exc:
                raise _safe_cause(
                    exc,
                    code="item_lifecycle_isolation_failed",
                    message="the item lifecycle isolation scope failed",
                ) from exc
            yield unit
        finally:
            try:
                if unit is not None:
                    unit.close()
            finally:
                stack.close()

    def _new_unit(
        self,
        *,
        operation_id: str,
        item_unit: FilesystemItemCommandUnitOfWork,
    ) -> "FilesystemItemLifecycleUnitOfWork":
        return FilesystemItemLifecycleUnitOfWork(
            self._write_set,
            operation_id=operation_id,
            item_unit=item_unit,
            safe_target=self._items.target_path,
            catalogue_relative=self._items.catalogue_relative,
            entry_directory_for=self._entry_directory_for,
            advance_restored_record=self._advance_restored_record,
            deletion_guard_for=self._deletion_guard_for,
        )


class FilesystemItemLifecycleUnitOfWork:
    """One locked item/tree/tombstone snapshot and staged lifecycle change."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        item_unit: FilesystemItemCommandUnitOfWork,
        safe_target: Callable[..., Path],
        catalogue_relative: str,
        entry_directory_for: EntryDirectoryResolver,
        advance_restored_record: RestoredRecordAdvancer,
        deletion_guard_for: ItemDeletionGuardFactory | None,
    ) -> None:
        self._write_set = write_set
        self._operation_id = operation_id
        self._item_unit = item_unit
        self._safe_target = safe_target
        self._catalogue_relative = catalogue_relative
        self._entry_directory_for = entry_directory_for
        self._advance_restored_record_callback = advance_restored_record
        self._deletion_guard_for_callback = deletion_guard_for
        self._held_guards = ExitStack()
        self._loaded_items: dict[str, LifecycleItemSnapshot | None] = {}
        self._loaded_trees: dict[str, ManagedTreeSnapshot | None] = {}
        self._loaded_envelopes: dict[str, dict[str, Any]] = {}
        self._envelope_ids: dict[str, str] | None = None
        self._staged_action = ""
        self._staged_item: LifecycleItemSnapshot | None = None
        self._staged_tree: ManagedTreeSnapshot | None = None
        self._staged_tombstone_before: ItemTombstoneSnapshot | None = None
        self._staged_tombstone_after: ItemTombstoneSnapshot | None = None
        self._staged_envelope: dict[str, Any] | None = None
        self._staged_tree_present = False
        self._committed = False
        self._closed = False

    def receipt(self, operation_id: str) -> ItemLifecycleReceipt | None:
        self._ensure_open()
        if operation_id != self._operation_id:
            raise RepositoryError(
                "the receipt request is outside this operation",
                code="receipt_scope_mismatch",
            )
        path = self._receipt_path(operation_id)
        if not path.exists():
            return None
        raw = _read_json(path, None, artifact="item_lifecycle_receipt")
        try:
            receipt = ItemLifecycleReceipt.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "an item lifecycle receipt is invalid",
                code="invalid_item_lifecycle_receipt",
                details={"cause_type": type(exc).__name__},
            ) from exc
        if receipt.operation_id != operation_id:
            raise RepositoryError(
                "the stored receipt belongs to another operation",
                code="receipt_scope_mismatch",
            )
        return receipt

    def get_item(self, item_id: str) -> LifecycleItemSnapshot | None:
        self._ensure_open()
        if item_id in self._loaded_items:
            return self._loaded_items[item_id]
        record = self._item_unit.get(item_id)
        snapshot = (
            None
            if record is None
            else LifecycleItemSnapshot(record.item_id, record.revision)
        )
        self._loaded_items[item_id] = snapshot
        return snapshot

    def get_managed_tree(self, item_id: str) -> ManagedTreeSnapshot | None:
        self._ensure_open()
        if item_id in self._loaded_trees:
            return self._loaded_trees[item_id]
        entry = self._entry_relative(item_id)
        path = self._safe_target(entry, artifact="item_entry")
        if os.path.lexists(path):
            revision = self._physical_tree_revision(path)
            snapshot: ManagedTreeSnapshot | None = ManagedTreeSnapshot(
                item_id, revision
            )
        elif self.get_item(item_id) is not None:
            snapshot = ManagedTreeSnapshot(
                item_id, EMPTY_MANAGED_TREE_REVISION
            )
        else:
            snapshot = None
        self._loaded_trees[item_id] = snapshot
        return snapshot

    def get_tombstone(
        self, tombstone_id: str
    ) -> ItemTombstoneSnapshot | None:
        self._ensure_open()
        self._file_identifier(tombstone_id, field="tombstone_id")
        if tombstone_id in self._loaded_envelopes:
            return ItemTombstoneSnapshot.from_dict(
                self._loaded_envelopes[tombstone_id]["tombstone"]
            )
        stored_id = self._envelope_id_index().get(tombstone_id.casefold())
        if stored_id is None:
            return None
        if stored_id != tombstone_id:
            raise RepositoryError(
                "the tombstone identity aliases a stored envelope",
                code="item_lifecycle_tombstone_alias",
                details={"tombstone_id": tombstone_id},
            )
        path = self._envelope_path(stored_id)
        raw = _read_json(path, None, artifact="item_lifecycle_tombstone")
        envelope = self._validate_envelope(raw, tombstone_id=stored_id)
        self._loaded_envelopes[stored_id] = envelope
        return ItemTombstoneSnapshot.from_dict(envelope["tombstone"])

    def list_tombstones(self) -> tuple[ItemTombstoneSnapshot, ...]:
        """Return strict public snapshots in deterministic identity order."""

        self._ensure_open()
        snapshots: list[ItemTombstoneSnapshot] = []
        for tombstone_id in self._envelope_id_index().values():
            tombstone = self.get_tombstone(tombstone_id)
            if tombstone is None:
                raise RepositoryError(
                    "a lifecycle envelope disappeared from the locked index",
                    code="invalid_item_lifecycle_store",
                    retryable=True,
                )
            snapshots.append(tombstone)
        return tuple(
            sorted(
                snapshots,
                key=lambda value: (
                    value.tombstone_id.casefold(),
                    value.tombstone_id,
                ),
            )
        )

    def stage_delete(
        self,
        item: LifecycleItemSnapshot,
        managed_tree: ManagedTreeSnapshot,
    ) -> ItemTombstoneSnapshot:
        self._ensure_stageable()
        if not isinstance(item, LifecycleItemSnapshot) or not isinstance(
            managed_tree, ManagedTreeSnapshot
        ):
            raise RepositoryError(
                "the item lifecycle deletion input is invalid",
                code="invalid_item_lifecycle_repository_command",
            )
        if self.get_item(item.item_id) != item or (
            self.get_managed_tree(item.item_id) != managed_tree
        ):
            raise RepositoryError(
                "the deletion is outside the locked lifecycle snapshot",
                code="item_lifecycle_repository_scope_mismatch",
            )
        self._enter_deletion_guard(item.item_id)
        raw = self._item_unit.raw_record(item.item_id)
        record = self._item_unit.get(item.item_id)
        if raw is None or record is None:
            raise RepositoryError(
                "the managed item disappeared from the locked snapshot",
                code="item_lifecycle_repository_scope_mismatch",
            )
        tombstone_id = self._new_tombstone_id()
        tombstone = ItemTombstoneSnapshot(
            tombstone_id=tombstone_id,
            revision=self._new_tombstone_revision(),
            state="deleted",
            item_id=item.item_id,
            deleted_item_revision=item.revision,
            managed_tree_revision=managed_tree.revision,
        )
        entry_relative = self._entry_relative(item.item_id)
        entry_path = self._safe_target(entry_relative, artifact="item_entry")
        present = os.path.lexists(entry_path)
        expected_present = managed_tree.revision != EMPTY_MANAGED_TREE_REVISION
        if present != expected_present:
            raise RepositoryError(
                "the managed tree changed after it was read",
                code="managed_tree_snapshot_changed",
                retryable=True,
            )
        if present and self._physical_tree_revision(entry_path) != (
            managed_tree.revision
        ):
            raise RepositoryError(
                "the managed tree changed after it was read",
                code="managed_tree_snapshot_changed",
                retryable=True,
            )
        tree_relative = self._tombstone_tree_relative(tombstone_id)
        envelope = {
            "schema": _ENVELOPE_SCHEMA,
            "tombstone": tombstone.as_dict(),
            "delete_operation_id": self._operation_id,
            "restore_operation_id": "",
            "record": _strict_plain(raw),
            "managed_tree": {
                "present": present,
                "revision": managed_tree.revision,
                "live_relative": entry_relative,
                "tombstone_relative": tree_relative,
            },
        }
        self._item_unit.stage_delete(record)
        self._staged_action = "delete"
        self._staged_item = item
        self._staged_tree = managed_tree
        self._staged_tombstone_after = tombstone
        self._staged_envelope = envelope
        self._staged_tree_present = present
        return tombstone

    def stage_restore(
        self,
        tombstone: ItemTombstoneSnapshot,
    ) -> StagedItemRestoration:
        self._ensure_stageable()
        if not isinstance(tombstone, ItemTombstoneSnapshot):
            raise RepositoryError(
                "the item lifecycle restoration input is invalid",
                code="invalid_item_lifecycle_repository_command",
            )
        loaded = self.get_tombstone(tombstone.tombstone_id)
        if loaded != tombstone:
            raise RepositoryError(
                "the restoration is outside the locked lifecycle snapshot",
                code="item_lifecycle_repository_scope_mismatch",
            )
        if self.get_item(tombstone.item_id) is not None or (
            self.get_managed_tree(tombstone.item_id) is not None
        ):
            raise RepositoryError(
                "the restore target is no longer absent",
                code="item_restore_collision",
            )
        envelope = self._loaded_envelopes[tombstone.tombstone_id]
        tree_metadata = envelope["managed_tree"]
        present = bool(tree_metadata["present"])
        live_relative = self._entry_relative(tombstone.item_id)
        tree_relative = self._tombstone_tree_relative(
            tombstone.tombstone_id
        )
        live_path = self._safe_target(live_relative, artifact="item_entry")
        tree_path = self._safe_target(
            tree_relative, artifact="item_lifecycle_tree"
        )
        if os.path.lexists(live_path):
            raise RepositoryError(
                "a managed tree already occupies the restore identity",
                code="managed_tree_restore_collision",
            )
        if present:
            if not os.path.lexists(tree_path) or (
                self._physical_tree_revision(tree_path)
                != tombstone.managed_tree_revision
            ):
                raise RepositoryError(
                    "the tombstoned managed tree changed elsewhere",
                    code="tombstone_tree_revision_conflict",
                    retryable=True,
                )
        elif os.path.lexists(tree_path):
            raise RepositoryError(
                "an unexpected tree occupies empty lifecycle storage",
                code="tombstone_tree_collision",
            )

        raw = self._advance_record(tombstone.item_id, envelope["record"])
        restored_record = self._item_unit.stage_restored_record(
            tombstone.item_id, raw
        )
        if restored_record.revision == tombstone.deleted_item_revision:
            raise RepositoryError(
                "the restored item revision was not advanced",
                code="restored_item_revision_not_advanced",
            )
        item = LifecycleItemSnapshot(
            tombstone.item_id, restored_record.revision
        )
        tree = ManagedTreeSnapshot(
            tombstone.item_id, tombstone.managed_tree_revision
        )
        restored_tombstone = ItemTombstoneSnapshot(
            tombstone_id=tombstone.tombstone_id,
            revision=self._new_tombstone_revision(),
            state="restored",
            item_id=tombstone.item_id,
            deleted_item_revision=tombstone.deleted_item_revision,
            managed_tree_revision=tombstone.managed_tree_revision,
            restored_item_revision=item.revision,
        )
        restored_envelope = _strict_plain(envelope)
        restored_envelope["tombstone"] = restored_tombstone.as_dict()
        restored_envelope["restore_operation_id"] = self._operation_id
        self._staged_action = "restore"
        self._staged_item = item
        self._staged_tree = tree
        self._staged_tombstone_before = tombstone
        self._staged_tombstone_after = restored_tombstone
        self._staged_envelope = restored_envelope
        self._staged_tree_present = present
        return StagedItemRestoration(item, tree, restored_tombstone)

    def commit(self, receipt: ItemLifecycleReceipt) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the item lifecycle unit is already committed",
                code="item_lifecycle_unit_committed",
            )
        self._validate_receipt(receipt)
        if self.receipt(self._operation_id) is not None:
            raise RepositoryError(
                "an item lifecycle receipt already exists",
                code="item_lifecycle_receipt_exists",
            )
        assert self._staged_tombstone_after is not None
        assert self._staged_envelope is not None
        assert self._staged_item is not None
        tombstone_id = self._staged_tombstone_after.tombstone_id
        try:
            transaction = self._write_set.begin(
                operation_id=self._operation_id,
                scope="item-lifecycle",
                metadata={
                    "action": self._staged_action,
                    "item_id": self._staged_item.item_id,
                    "tombstone_id": tombstone_id,
                },
            )
            if self._staged_tree_present:
                live = self._entry_relative(self._staged_item.item_id)
                archived = self._tombstone_tree_relative(tombstone_id)
                source, destination = (
                    (live, archived)
                    if self._staged_action == "delete"
                    else (archived, live)
                )
                transaction.stage_tree_move(source, destination)
            transaction.stage_write(
                self._envelope_relative(tombstone_id),
                _json_bytes(
                    self._staged_envelope,
                    artifact="item_lifecycle_tombstone",
                ),
            )
            transaction.stage_write(
                self._receipt_relative(self._operation_id),
                _json_bytes(
                    receipt.as_dict(), artifact="item_lifecycle_receipt"
                ),
            )
            # The catalogue projection is always the final file publication.
            # RWS v2 reserves the earlier publication slots for tree moves.
            self._item_unit.stage_catalogue_publication(transaction)
            transaction.commit(receipt=receipt.as_dict())
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the item lifecycle transaction failed",
            ) from exc
        self._committed = True

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._held_guards.close()

    def _enter_deletion_guard(self, item_id: str) -> None:
        if self._deletion_guard_for_callback is None:
            return
        try:
            guard = self._deletion_guard_for_callback(item_id)
            self._held_guards.enter_context(guard)
        except EngineError:
            raise
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="item_deletion_guard_failed",
                message="the item deletion guard failed",
            ) from exc

    def _advance_record(
        self, item_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            value = self._advance_restored_record_callback(
                item_id, _strict_plain(raw)
            )
            detached = _strict_plain(value)
        except EngineError:
            raise
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="item_restore_record_codec_failed",
                message="the item restore record could not be advanced",
            ) from exc
        if not isinstance(detached, dict):
            raise RepositoryError(
                "the item restore record advancer returned a non-object",
                code="item_restore_record_codec_failed",
            )
        return detached

    def _validate_receipt(self, receipt: ItemLifecycleReceipt) -> None:
        after = self._staged_tombstone_after
        item = self._staged_item
        tree = self._staged_tree
        if (
            not isinstance(receipt, ItemLifecycleReceipt)
            or after is None
            or item is None
            or tree is None
            or receipt.operation_id != self._operation_id
            or receipt.action != self._staged_action
            or receipt.item_id != item.item_id
            or receipt.managed_tree_revision != tree.revision
            or receipt.tombstone != after
        ):
            raise RepositoryError(
                "the lifecycle receipt is outside the staged mutation",
                code="receipt_scope_mismatch",
            )
        if self._staged_action == "delete":
            valid = (
                receipt.deleted_item_revision == item.revision
                and not receipt.restored_item_revision
                and not receipt.tombstone_before_revision
                and self._staged_tombstone_before is None
            )
        else:
            before = self._staged_tombstone_before
            valid = (
                before is not None
                and receipt.deleted_item_revision
                == before.deleted_item_revision
                and receipt.restored_item_revision == item.revision
                and receipt.tombstone_before_revision == before.revision
            )
        if not valid:
            raise RepositoryError(
                "the lifecycle receipt is outside the staged mutation",
                code="receipt_scope_mismatch",
            )

    def _validate_envelope(
        self, value: Any, *, tombstone_id: str
    ) -> dict[str, Any]:
        try:
            envelope = _strict_plain(value)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "an item lifecycle tombstone is invalid",
                code="invalid_item_lifecycle_tombstone",
            ) from exc
        fields = {
            "schema",
            "tombstone",
            "delete_operation_id",
            "restore_operation_id",
            "record",
            "managed_tree",
        }
        if not isinstance(envelope, dict) or set(envelope) != fields:
            raise RepositoryError(
                "an item lifecycle tombstone has the wrong schema",
                code="invalid_item_lifecycle_tombstone",
            )
        try:
            tombstone = ItemTombstoneSnapshot.from_dict(
                envelope["tombstone"]
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "an item lifecycle tombstone is invalid",
                code="invalid_item_lifecycle_tombstone",
            ) from exc
        tree = envelope["managed_tree"]
        tree_fields = {
            "present",
            "revision",
            "live_relative",
            "tombstone_relative",
        }
        expected_live = self._entry_relative(tombstone.item_id)
        expected_tree = self._tombstone_tree_relative(tombstone_id)
        delete_operation = envelope["delete_operation_id"]
        restore_operation = envelope["restore_operation_id"]
        valid_operations = (
            isinstance(delete_operation, str)
            and bool(_IDENTIFIER_RE.fullmatch(delete_operation))
            and isinstance(restore_operation, str)
            and (
                not restore_operation
                or bool(_IDENTIFIER_RE.fullmatch(restore_operation))
            )
        )
        valid_tree = (
            isinstance(tree, dict)
            and set(tree) == tree_fields
            and isinstance(tree.get("present"), bool)
            and tree.get("revision") == tombstone.managed_tree_revision
            and tree.get("live_relative") == expected_live
            and tree.get("tombstone_relative") == expected_tree
            and (
                (
                    tree.get("present") is True
                    and tombstone.managed_tree_revision.startswith(
                        _PHYSICAL_TREE_PREFIX
                    )
                )
                or (
                    tree.get("present") is False
                    and tombstone.managed_tree_revision
                    == EMPTY_MANAGED_TREE_REVISION
                )
            )
        )
        valid_state = (
            (tombstone.state == "deleted" and not restore_operation)
            or (tombstone.state == "restored" and bool(restore_operation))
        )
        if (
            envelope["schema"] != _ENVELOPE_SCHEMA
            or tombstone.tombstone_id != tombstone_id
            or not isinstance(envelope["record"], dict)
            or not valid_operations
            or not valid_tree
            or not valid_state
        ):
            raise RepositoryError(
                "an item lifecycle tombstone is inconsistent",
                code="invalid_item_lifecycle_tombstone",
            )
        return envelope

    def _entry_relative(self, item_id: str) -> str:
        try:
            configured = Path(self._entry_directory_for(item_id))
            candidate = (
                configured
                if configured.is_absolute()
                else self._write_set.root / configured
            )
            candidate = Path(os.path.abspath(candidate))
            relative = candidate.relative_to(self._write_set.root)
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the item entry directory is invalid",
                code="invalid_item_lifecycle_path",
                details={"item_id": item_id, "cause_type": type(exc).__name__},
            ) from exc
        pure = PurePosixPath(relative.as_posix())
        catalogue = PurePosixPath(self._catalogue_relative)
        if (
            not pure.parts
            or pure.parts[0].casefold() in {".engine", ".transactions"}
            or _paths_overlap(pure, catalogue)
        ):
            raise RepositoryError(
                "the item entry directory is invalid",
                code="invalid_item_lifecycle_path",
                details={"item_id": item_id},
            )
        # Reuse the item repository's component-by-component redirect checks.
        self._safe_target(pure.as_posix(), artifact="item_entry")
        return pure.as_posix()

    def _physical_tree_revision(self, path: Path) -> str:
        try:
            fingerprint = _fingerprint_tree(path)
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the managed item tree could not be inspected",
            ) from exc
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="managed_tree_inspection_failed",
                message="the managed item tree could not be inspected",
            ) from exc
        return _PHYSICAL_TREE_PREFIX + str(fingerprint["sha256"])

    def _new_tombstone_id(self) -> str:
        existing = self._stored_tombstone_ids()
        while True:
            candidate = "ilt-" + uuid.uuid4().hex
            if candidate.casefold() not in existing:
                return candidate

    def _new_tombstone_revision(self) -> str:
        existing = self._stored_tombstone_revisions()
        while True:
            candidate = "ltr-" + uuid.uuid4().hex
            if candidate not in existing:
                return candidate

    def _stored_tombstone_ids(self) -> set[str]:
        aliases = dict(self._envelope_id_index())
        root = self._safe_target(
            _TREE_ROOT.as_posix(), artifact="item_lifecycle_store"
        )
        if not root.exists():
            return set(aliases)
        if not root.is_dir() or _is_redirecting_path(root):
            raise RepositoryError(
                "the item lifecycle store is invalid",
                code="invalid_item_lifecycle_store",
            )
        for path in root.iterdir():
            if _is_redirecting_path(path) or not path.is_dir():
                raise RepositoryError(
                    "the lifecycle tree store contains an invalid entry",
                    code="invalid_item_lifecycle_store",
                )
            identity = self._file_identifier(
                path.name, field="stored_tombstone_id"
            )
            folded = identity.casefold()
            if folded in aliases and aliases[folded] != identity:
                raise RepositoryError(
                    "the lifecycle store contains aliased identities",
                    code="invalid_item_lifecycle_store",
                )
            aliases[folded] = identity
        return set(aliases)

    def _stored_tombstone_revisions(self) -> set[str]:
        revisions: set[str] = set()
        for identity in self._envelope_id_index().values():
            tombstone = self.get_tombstone(identity)
            assert tombstone is not None
            revisions.add(tombstone.revision)
        return revisions

    def _envelope_id_index(self) -> dict[str, str]:
        """Return case-folded envelope identities or fail on any ambiguity."""

        if self._envelope_ids is not None:
            return dict(self._envelope_ids)
        root = self._safe_target(
            _ENVELOPE_ROOT.as_posix(), artifact="item_lifecycle_store"
        )
        if not root.exists():
            self._envelope_ids = {}
            return {}
        if not root.is_dir() or _is_redirecting_path(root):
            raise RepositoryError(
                "the item lifecycle store is invalid",
                code="invalid_item_lifecycle_store",
            )
        aliases: dict[str, str] = {}
        for path in root.iterdir():
            if (
                _is_redirecting_path(path)
                or not path.is_file()
                or path.suffix != ".json"
            ):
                raise RepositoryError(
                    "the lifecycle envelope store contains an invalid entry",
                    code="invalid_item_lifecycle_store",
                )
            identity = self._file_identifier(
                path.stem, field="stored_tombstone_id"
            )
            if path.name != f"{identity}.json":
                raise RepositoryError(
                    "the lifecycle envelope name is not canonical",
                    code="invalid_item_lifecycle_store",
                )
            folded = identity.casefold()
            if folded in aliases:
                raise RepositoryError(
                    "the lifecycle envelope store contains aliased identities",
                    code="invalid_item_lifecycle_store",
                )
            aliases[folded] = identity
        self._envelope_ids = dict(aliases)
        return aliases

    def _receipt_path(self, operation_id: str) -> Path:
        return self._safe_target(
            self._receipt_relative(operation_id),
            artifact="item_lifecycle_receipt",
        )

    def _envelope_path(self, tombstone_id: str) -> Path:
        return self._safe_target(
            self._envelope_relative(tombstone_id),
            artifact="item_lifecycle_tombstone",
        )

    @staticmethod
    def _receipt_relative(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return (_RECEIPT_ROOT / f"{digest}.json").as_posix()

    @staticmethod
    def _envelope_relative(tombstone_id: str) -> str:
        return (_ENVELOPE_ROOT / f"{tombstone_id}.json").as_posix()

    @staticmethod
    def _tombstone_tree_relative(tombstone_id: str) -> str:
        return (_TREE_ROOT / tombstone_id).as_posix()

    @staticmethod
    def _file_identifier(value: Any, *, field: str) -> str:
        if (
            not isinstance(value, str)
            or not _FILE_IDENTIFIER_RE.fullmatch(value)
            or value.endswith(".")
            or value.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
        ):
            raise RepositoryError(
                f"{field} is not a portable filesystem identifier",
                code="invalid_item_lifecycle_identity",
                details={"field": field},
            )
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryError(
                "the item lifecycle unit is closed",
                code="item_lifecycle_unit_closed",
            )

    def _ensure_stageable(self) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the item lifecycle unit is already committed",
                code="item_lifecycle_unit_committed",
            )
        if self._staged_action:
            raise RepositoryError(
                "an item lifecycle mutation is already staged",
                code="item_lifecycle_mutation_already_staged",
            )


def _paths_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    left_parts = tuple(part.casefold() for part in left.parts)
    right_parts = tuple(part.casefold() for part in right.parts)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


__all__ = [
    "EMPTY_MANAGED_TREE_REVISION",
    "EntryDirectoryResolver",
    "FilesystemItemLifecycleRepository",
    "ItemDeletionGuardFactory",
    "LifecycleLockFactory",
    "RestoredRecordAdvancer",
]
