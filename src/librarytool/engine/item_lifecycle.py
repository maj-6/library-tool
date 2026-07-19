"""Recoverable, framework-neutral item deletion and restoration commands.

The ordinary item command service owns catalogue mutations.  This boundary
owns the larger lifecycle transaction in which a catalogue record and the
engine-managed entry tree must disappear or reappear together.  A repository
adapter supplies the persistence and move mechanics behind one isolated unit
of work; the engine supplies validation, compare-and-swap checks, durable
idempotency, and outcome validation.

``ManagedTreeSnapshot`` deliberately describes the logical set of files owned
by the library.  An existing catalogue item therefore still has a deterministic
empty-tree revision when no physical entry directory has been materialized.
Bytes reached through externally referenced representations are not members of
that set, do not contribute to its revision, and must never be moved by a
lifecycle repository.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ContextManager, Literal, Protocol, TypeAlias

from .errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)


JsonMapping: TypeAlias = Mapping[str, Any]
ItemLifecycleAction: TypeAlias = Literal["delete", "restore"]
TombstoneState: TypeAlias = Literal["deleted", "restored"]

_ACTIONS = frozenset({"delete", "restore"})
_TOMBSTONE_STATES = frozenset({"deleted", "restored"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


def _revision(value: Any, field_name: str, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if optional and not value:
        return value
    if (
        not value
        or len(value) > 512
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError(f"{field_name} is not a valid revision token")
    return value


def _canonical(value: JsonMapping) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def _fields(value: Any, expected: set[str], subject: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{subject} must be an object")
    if set(value) != expected:
        raise ValueError(f"{subject} fields do not match the schema")
    return value


@dataclass(frozen=True, slots=True)
class DeleteItemCommand:
    """Delete one item only if both owned aggregate revisions still match."""

    item_id: str
    expected_item_revision: str
    expected_managed_tree_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "item_id",
            "expected_item_revision",
            "expected_managed_tree_revision",
            "operation_id",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")


@dataclass(frozen=True, slots=True)
class RestoreItemCommand:
    """Restore the deleted aggregate identified by a tombstone revision."""

    tombstone_id: str
    expected_tombstone_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "tombstone_id",
            "expected_tombstone_revision",
            "operation_id",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")


@dataclass(frozen=True, slots=True)
class LifecycleItemSnapshot:
    """Minimal catalogue identity needed by the lifecycle transaction."""

    item_id: str
    revision: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _revision(self.revision, "revision")

    @classmethod
    def from_dict(cls, value: Any) -> LifecycleItemSnapshot:
        value = _fields(value, {"item_id", "revision"}, "item snapshot")
        return cls(item_id=value["item_id"], revision=value["revision"])

    def as_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class ManagedTreeSnapshot:
    """Identity of one item's logical set of engine-owned files.

    External reference targets are intentionally outside this snapshot.  A
    repository must neither include them in ``revision`` nor move/delete them
    while staging a lifecycle command.  If a live catalogue item has no
    physical entry directory, the repository must return a snapshot with its
    deterministic empty-tree revision rather than ``None``.  This keeps empty
    items in the same CAS model without requiring placeholder directories.
    """

    item_id: str
    revision: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _revision(self.revision, "revision")

    @classmethod
    def from_dict(cls, value: Any) -> ManagedTreeSnapshot:
        value = _fields(
            value, {"item_id", "revision"}, "managed tree snapshot"
        )
        return cls(item_id=value["item_id"], revision=value["revision"])

    def as_dict(self) -> dict[str, str]:
        return {"item_id": self.item_id, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class ItemTombstoneSnapshot:
    """Durable lifecycle state for one deleted item and its managed tree."""

    tombstone_id: str
    revision: str
    state: TombstoneState
    item_id: str
    deleted_item_revision: str
    managed_tree_revision: str
    restored_item_revision: str = ""

    def __post_init__(self) -> None:
        _identifier(self.tombstone_id, "tombstone_id")
        _revision(self.revision, "revision")
        if self.state not in _TOMBSTONE_STATES:
            raise ValueError("tombstone state is invalid")
        _identifier(self.item_id, "item_id")
        _revision(self.deleted_item_revision, "deleted_item_revision")
        _revision(self.managed_tree_revision, "managed_tree_revision")
        _revision(
            self.restored_item_revision,
            "restored_item_revision",
            optional=True,
        )
        if self.state == "deleted" and self.restored_item_revision:
            raise ValueError("a deleted tombstone cannot have a restore revision")
        if self.state == "restored" and (
            not self.restored_item_revision
            or self.restored_item_revision == self.deleted_item_revision
        ):
            raise ValueError(
                "a restored tombstone requires a new item revision"
            )

    @classmethod
    def from_dict(cls, value: Any) -> ItemTombstoneSnapshot:
        value = _fields(
            value,
            {
                "tombstone_id",
                "revision",
                "state",
                "item_id",
                "deleted_item_revision",
                "managed_tree_revision",
                "restored_item_revision",
            },
            "item tombstone",
        )
        return cls(
            tombstone_id=value["tombstone_id"],
            revision=value["revision"],
            state=value["state"],
            item_id=value["item_id"],
            deleted_item_revision=value["deleted_item_revision"],
            managed_tree_revision=value["managed_tree_revision"],
            restored_item_revision=value["restored_item_revision"],
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "tombstone_id": self.tombstone_id,
            "revision": self.revision,
            "state": self.state,
            "item_id": self.item_id,
            "deleted_item_revision": self.deleted_item_revision,
            "managed_tree_revision": self.managed_tree_revision,
            "restored_item_revision": self.restored_item_revision,
        }


@dataclass(frozen=True, slots=True)
class StagedItemRestoration:
    """Repository-produced state staged by one restore operation."""

    item: LifecycleItemSnapshot
    managed_tree: ManagedTreeSnapshot
    tombstone: ItemTombstoneSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.item, LifecycleItemSnapshot):
            raise TypeError("item must be a LifecycleItemSnapshot")
        if not isinstance(self.managed_tree, ManagedTreeSnapshot):
            raise TypeError("managed_tree must be a ManagedTreeSnapshot")
        if not isinstance(self.tombstone, ItemTombstoneSnapshot):
            raise TypeError("tombstone must be an ItemTombstoneSnapshot")
        if not (
            self.item.item_id
            == self.managed_tree.item_id
            == self.tombstone.item_id
        ):
            raise ValueError("restored state has inconsistent item identities")
        if self.tombstone.state != "restored":
            raise ValueError("restored state requires a restored tombstone")
        if self.item.revision != self.tombstone.restored_item_revision:
            raise ValueError("restored item revision is inconsistent")
        if self.managed_tree.revision != self.tombstone.managed_tree_revision:
            raise ValueError("restored managed tree revision is inconsistent")


@dataclass(frozen=True, slots=True)
class ItemLifecycleReceipt:
    """Durable, private replay record for a lifecycle operation."""

    action: ItemLifecycleAction
    operation_id: str
    command_sha256: str
    item_id: str
    deleted_item_revision: str
    restored_item_revision: str
    managed_tree_revision: str
    tombstone_before_revision: str
    tombstone: ItemTombstoneSnapshot

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        _identifier(self.operation_id, "operation_id")
        if not isinstance(self.command_sha256, str) or not _SHA256_RE.fullmatch(
            self.command_sha256
        ):
            raise ValueError("command_sha256 is invalid")
        _identifier(self.item_id, "item_id")
        _revision(self.deleted_item_revision, "deleted_item_revision")
        _revision(
            self.restored_item_revision,
            "restored_item_revision",
            optional=True,
        )
        _revision(self.managed_tree_revision, "managed_tree_revision")
        _revision(
            self.tombstone_before_revision,
            "tombstone_before_revision",
            optional=True,
        )
        if not isinstance(self.tombstone, ItemTombstoneSnapshot):
            raise TypeError("tombstone must be an ItemTombstoneSnapshot")
        if (
            self.tombstone.item_id != self.item_id
            or self.tombstone.deleted_item_revision
            != self.deleted_item_revision
            or self.tombstone.managed_tree_revision
            != self.managed_tree_revision
        ):
            raise ValueError("receipt tombstone does not match its aggregate")
        if self.action == "delete":
            valid = (
                not self.restored_item_revision
                and not self.tombstone_before_revision
                and self.tombstone.state == "deleted"
            )
        else:
            valid = (
                bool(self.restored_item_revision)
                and self.restored_item_revision
                != self.deleted_item_revision
                and bool(self.tombstone_before_revision)
                and self.tombstone_before_revision != self.tombstone.revision
                and self.tombstone.state == "restored"
                and self.tombstone.restored_item_revision
                == self.restored_item_revision
            )
        if not valid:
            raise ValueError("receipt state does not match its action")

    @classmethod
    def from_dict(cls, value: Any) -> ItemLifecycleReceipt:
        value = _fields(
            value,
            {
                "action",
                "operation_id",
                "command_sha256",
                "item_id",
                "deleted_item_revision",
                "restored_item_revision",
                "managed_tree_revision",
                "tombstone_before_revision",
                "tombstone",
            },
            "item lifecycle receipt",
        )
        return cls(
            action=value["action"],
            operation_id=value["operation_id"],
            command_sha256=value["command_sha256"],
            item_id=value["item_id"],
            deleted_item_revision=value["deleted_item_revision"],
            restored_item_revision=value["restored_item_revision"],
            managed_tree_revision=value["managed_tree_revision"],
            tombstone_before_revision=value["tombstone_before_revision"],
            tombstone=ItemTombstoneSnapshot.from_dict(value["tombstone"]),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the durable repository form, including replay fingerprint."""

        return {
            "action": self.action,
            "operation_id": self.operation_id,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "deleted_item_revision": self.deleted_item_revision,
            "restored_item_revision": self.restored_item_revision,
            "managed_tree_revision": self.managed_tree_revision,
            "tombstone_before_revision": self.tombstone_before_revision,
            "tombstone": self.tombstone.as_dict(),
        }

    def as_public_dict(self) -> dict[str, Any]:
        """Return a transport-safe receipt without its command fingerprint."""

        value = self.as_dict()
        value.pop("command_sha256")
        return value


@dataclass(frozen=True, slots=True)
class ItemLifecycleResult:
    receipt: ItemLifecycleReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ItemLifecycleReceipt):
            raise TypeError("receipt must be an ItemLifecycleReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_public_dict(),
        }


class ItemLifecycleUnitOfWorkPort(Protocol):
    """One isolated, recoverable lifecycle transaction boundary.

    The unit must hold one stable isolation/lock scope from the first read
    through ``commit``.  ``stage_delete`` stages catalogue removal, tombstone
    creation, and a move of the managed tree into lifecycle storage.
    ``stage_restore`` stages the inverse move, catalogue restoration with a
    new revision, and the tombstone's transition to ``restored``.  Neither
    method may publish live state.  ``commit`` must provide recoverable
    all-or-rollback semantics: under the shared isolation scope, publish tree
    moves first and the catalogue projection last, after the tombstone and
    receipt are durable.  Readers and every competing writer of these assets
    must respect that same scope, and restart recovery must finish before state
    is served.  Exiting without commit must discard or roll back every stage.

    ``get_managed_tree`` describes a logical owned-asset set, not merely a
    directory lookup.  It must return a deterministic empty-tree snapshot for
    a live item whose directory is absent, and a snapshot for an orphaned
    physical tree so restore detects the collision.  It returns ``None`` only
    when neither a live logical set nor a physical tree occupies the identity.
    Deleting or restoring a logical empty tree is a no-op move and need not
    materialize a placeholder directory.

    The public tombstone snapshot intentionally omits catalogue content and
    storage locations.  ``stage_delete`` must persist the complete raw record
    and any move metadata in a private repository envelope keyed by a fresh,
    collision-checked tombstone id.  ``stage_restore`` reads that envelope and
    must generate fresh item and tombstone revisions while preserving the
    public snapshot invariants validated by the service.

    Files reached through external/reference representation locators are not
    in the managed tree and must remain untouched by both stage methods.
    """

    def receipt(self, operation_id: str) -> ItemLifecycleReceipt | None: ...

    def get_item(self, item_id: str) -> LifecycleItemSnapshot | None: ...

    def get_managed_tree(self, item_id: str) -> ManagedTreeSnapshot | None: ...

    def get_tombstone(
        self, tombstone_id: str
    ) -> ItemTombstoneSnapshot | None: ...

    def stage_delete(
        self,
        item: LifecycleItemSnapshot,
        managed_tree: ManagedTreeSnapshot,
    ) -> ItemTombstoneSnapshot: ...

    def stage_restore(
        self,
        tombstone: ItemTombstoneSnapshot,
    ) -> StagedItemRestoration: ...

    def commit(self, receipt: ItemLifecycleReceipt) -> None: ...


class ItemLifecycleRepositoryPort(Protocol):
    """Open one operation-scoped, isolated lifecycle unit of work."""

    def unit_of_work(
        self, *, operation_id: str
    ) -> ContextManager[ItemLifecycleUnitOfWorkPort]: ...


class ItemLifecycleService:
    """Conditionally stage and idempotently commit item lifecycle commands."""

    def __init__(self, repository: ItemLifecycleRepositoryPort) -> None:
        self._repository = repository

    def delete(self, command: DeleteItemCommand) -> ItemLifecycleResult:
        if not isinstance(command, DeleteItemCommand):
            raise ValidationError(
                "delete requires a DeleteItemCommand",
                code="invalid_item_lifecycle_command",
            )
        item_id = self._item_id(command.item_id)
        expected_item_revision = self._expected_revision(
            command.expected_item_revision,
            field="expected_item_revision",
            required_code="item_revision_required",
            invalid_code="invalid_item_revision",
            details={"item_id": item_id},
        )
        expected_tree_revision = self._expected_revision(
            command.expected_managed_tree_revision,
            field="expected_managed_tree_revision",
            required_code="managed_tree_revision_required",
            invalid_code="invalid_managed_tree_revision",
            details={"item_id": item_id},
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "delete",
                "item_id": item_id,
                "expected_item_revision": expected_item_revision,
                "expected_managed_tree_revision": expected_tree_revision,
            }
        )
        try:
            with self._repository.unit_of_work(
                operation_id=operation_id
            ) as unit:
                replay = self._replay_delete(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    expected_item_revision=expected_item_revision,
                    expected_tree_revision=expected_tree_revision,
                )
                if replay is not None:
                    return replay
                item = self._require_item(unit, item_id)
                self._match_item_revision(item, expected_item_revision)
                tree = self._require_tree(unit, item_id)
                self._match_tree_revision(tree, expected_tree_revision)
                tombstone = self._tombstone(
                    unit.stage_delete(item, tree), required=True
                )
                assert tombstone is not None
                self._validate_deleted_tombstone(
                    tombstone, item=item, tree=tree
                )
                receipt = ItemLifecycleReceipt(
                    action="delete",
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    deleted_item_revision=item.revision,
                    restored_item_revision="",
                    managed_tree_revision=tree.revision,
                    tombstone_before_revision="",
                    tombstone=tombstone,
                )
                unit.commit(receipt)
                return ItemLifecycleResult(receipt)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    def restore(self, command: RestoreItemCommand) -> ItemLifecycleResult:
        if not isinstance(command, RestoreItemCommand):
            raise ValidationError(
                "restore requires a RestoreItemCommand",
                code="invalid_item_lifecycle_command",
            )
        tombstone_id = self._tombstone_id(command.tombstone_id)
        expected_tombstone_revision = self._expected_revision(
            command.expected_tombstone_revision,
            field="expected_tombstone_revision",
            required_code="tombstone_revision_required",
            invalid_code="invalid_tombstone_revision",
            details={"tombstone_id": tombstone_id},
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "restore",
                "tombstone_id": tombstone_id,
                "expected_tombstone_revision": expected_tombstone_revision,
            }
        )
        try:
            with self._repository.unit_of_work(
                operation_id=operation_id
            ) as unit:
                replay = self._replay_restore(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    tombstone_id=tombstone_id,
                    expected_tombstone_revision=expected_tombstone_revision,
                )
                if replay is not None:
                    return replay
                tombstone = self._require_tombstone(unit, tombstone_id)
                self._match_tombstone_revision(
                    tombstone, expected_tombstone_revision
                )
                if tombstone.state != "deleted":
                    raise ConflictError(
                        "the tombstone is not restorable",
                        code="tombstone_state_conflict",
                        details={
                            "tombstone_id": tombstone_id,
                            "current_state": tombstone.state,
                        },
                    )
                self._require_restore_target_absent(unit, tombstone.item_id)
                staged = unit.stage_restore(tombstone)
                if not isinstance(staged, StagedItemRestoration):
                    raise RepositoryError(
                        "the lifecycle repository returned an invalid restoration",
                        code="invalid_item_restoration",
                    )
                self._validate_restoration(staged, before=tombstone)
                receipt = ItemLifecycleReceipt(
                    action="restore",
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=tombstone.item_id,
                    deleted_item_revision=tombstone.deleted_item_revision,
                    restored_item_revision=staged.item.revision,
                    managed_tree_revision=tombstone.managed_tree_revision,
                    tombstone_before_revision=tombstone.revision,
                    tombstone=staged.tombstone,
                )
                unit.commit(receipt)
                return ItemLifecycleResult(receipt)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    @staticmethod
    def _operation_id(value: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an operation id is required",
                code="operation_id_required",
                details={"field": "operation_id"},
            )
        try:
            return _identifier(value, "operation_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "operation id must be a portable identifier",
                code="invalid_operation_id",
            ) from exc

    @staticmethod
    def _item_id(value: str) -> str:
        if not value:
            raise ValidationError("item id is required", code="item_id_required")
        try:
            return _identifier(value, "item_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "item id must be a portable identifier",
                code="invalid_item_id",
            ) from exc

    @staticmethod
    def _tombstone_id(value: str) -> str:
        if not value:
            raise ValidationError(
                "tombstone id is required", code="tombstone_id_required"
            )
        try:
            return _identifier(value, "tombstone_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "tombstone id must be a portable identifier",
                code="invalid_tombstone_id",
            ) from exc

    @staticmethod
    def _expected_revision(
        value: str,
        *,
        field: str,
        required_code: str,
        invalid_code: str,
        details: JsonMapping,
    ) -> str:
        if not value:
            raise PreconditionRequiredError(
                f"{field} is required",
                code=required_code,
                details=details,
            )
        try:
            return _revision(value, field)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                f"{field} is invalid", code=invalid_code, details=details
            ) from exc

    @staticmethod
    def _command_hash(value: JsonMapping) -> str:
        return hashlib.sha256(_canonical(value)).hexdigest()

    @staticmethod
    def _item(value: Any) -> LifecycleItemSnapshot | None:
        if value is None:
            return None
        if not isinstance(value, LifecycleItemSnapshot):
            raise RepositoryError(
                "the lifecycle repository returned an invalid item",
                code="invalid_lifecycle_item_snapshot",
            )
        return value

    @staticmethod
    def _tree(value: Any) -> ManagedTreeSnapshot | None:
        if value is None:
            return None
        if not isinstance(value, ManagedTreeSnapshot):
            raise RepositoryError(
                "the lifecycle repository returned an invalid managed tree",
                code="invalid_managed_tree_snapshot",
            )
        return value

    @staticmethod
    def _tombstone(
        value: Any, *, required: bool = False
    ) -> ItemTombstoneSnapshot | None:
        if value is None and not required:
            return None
        if not isinstance(value, ItemTombstoneSnapshot):
            raise RepositoryError(
                "the lifecycle repository returned an invalid tombstone",
                code="invalid_item_tombstone",
            )
        return value

    def _require_item(
        self, unit: ItemLifecycleUnitOfWorkPort, item_id: str
    ) -> LifecycleItemSnapshot:
        item = self._item(unit.get_item(item_id))
        if item is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": item_id},
            )
        if item.item_id != item_id:
            raise RepositoryError(
                "the lifecycle repository returned another item",
                code="lifecycle_repository_scope_mismatch",
            )
        return item

    def _require_tree(
        self, unit: ItemLifecycleUnitOfWorkPort, item_id: str
    ) -> ManagedTreeSnapshot:
        tree = self._tree(unit.get_managed_tree(item_id))
        if tree is None:
            raise NotFoundError(
                "the managed item tree does not exist",
                code="managed_tree_not_found",
                details={"item_id": item_id},
            )
        if tree.item_id != item_id:
            raise RepositoryError(
                "the lifecycle repository returned another managed tree",
                code="lifecycle_repository_scope_mismatch",
            )
        return tree

    def _require_tombstone(
        self, unit: ItemLifecycleUnitOfWorkPort, tombstone_id: str
    ) -> ItemTombstoneSnapshot:
        tombstone = self._tombstone(unit.get_tombstone(tombstone_id))
        if tombstone is None:
            raise NotFoundError(
                "the item tombstone does not exist",
                code="item_tombstone_not_found",
                details={"tombstone_id": tombstone_id},
            )
        if tombstone.tombstone_id != tombstone_id:
            raise RepositoryError(
                "the lifecycle repository returned another tombstone",
                code="lifecycle_repository_scope_mismatch",
            )
        return tombstone

    @staticmethod
    def _match_item_revision(
        item: LifecycleItemSnapshot, expected_revision: str
    ) -> None:
        if item.revision != expected_revision:
            raise ConflictError(
                "the item changed elsewhere",
                code="item_revision_conflict",
                details={
                    "item_id": item.item_id,
                    "expected_revision": expected_revision,
                    "current_revision": item.revision,
                },
            )

    @staticmethod
    def _match_tree_revision(
        tree: ManagedTreeSnapshot, expected_revision: str
    ) -> None:
        if tree.revision != expected_revision:
            raise ConflictError(
                "the managed item tree changed elsewhere",
                code="managed_tree_revision_conflict",
                details={
                    "item_id": tree.item_id,
                    "expected_revision": expected_revision,
                    "current_revision": tree.revision,
                },
            )

    @staticmethod
    def _match_tombstone_revision(
        tombstone: ItemTombstoneSnapshot, expected_revision: str
    ) -> None:
        if tombstone.revision != expected_revision:
            raise ConflictError(
                "the item tombstone changed elsewhere",
                code="tombstone_revision_conflict",
                details={
                    "tombstone_id": tombstone.tombstone_id,
                    "expected_revision": expected_revision,
                    "current_revision": tombstone.revision,
                },
            )

    def _require_restore_target_absent(
        self, unit: ItemLifecycleUnitOfWorkPort, item_id: str
    ) -> None:
        item = self._item(unit.get_item(item_id))
        if item is not None:
            if item.item_id != item_id:
                raise RepositoryError(
                    "the lifecycle repository returned another item",
                    code="lifecycle_repository_scope_mismatch",
                )
            raise ConflictError(
                "an item already occupies the restore identity",
                code="item_restore_collision",
                details={"item_id": item_id, "current_revision": item.revision},
            )
        tree = self._tree(unit.get_managed_tree(item_id))
        if tree is not None:
            if tree.item_id != item_id:
                raise RepositoryError(
                    "the lifecycle repository returned another managed tree",
                    code="lifecycle_repository_scope_mismatch",
                )
            raise ConflictError(
                "a managed tree already occupies the restore identity",
                code="managed_tree_restore_collision",
                details={
                    "item_id": item_id,
                    "current_revision": tree.revision,
                },
            )

    @staticmethod
    def _validate_deleted_tombstone(
        tombstone: ItemTombstoneSnapshot,
        *,
        item: LifecycleItemSnapshot,
        tree: ManagedTreeSnapshot,
    ) -> None:
        if (
            tombstone.state != "deleted"
            or tombstone.item_id != item.item_id
            or tombstone.deleted_item_revision != item.revision
            or tombstone.managed_tree_revision != tree.revision
        ):
            raise RepositoryError(
                "the lifecycle repository staged the wrong deletion",
                code="lifecycle_repository_content_mismatch",
            )

    @staticmethod
    def _validate_restoration(
        staged: StagedItemRestoration, *, before: ItemTombstoneSnapshot
    ) -> None:
        if (
            staged.item.item_id != before.item_id
            or staged.item.revision == before.deleted_item_revision
            or staged.managed_tree.item_id != before.item_id
            or staged.managed_tree.revision != before.managed_tree_revision
            or staged.tombstone.tombstone_id != before.tombstone_id
            or staged.tombstone.revision == before.revision
            or staged.tombstone.state != "restored"
            or staged.tombstone.item_id != before.item_id
            or staged.tombstone.deleted_item_revision
            != before.deleted_item_revision
            or staged.tombstone.managed_tree_revision
            != before.managed_tree_revision
            or staged.tombstone.restored_item_revision != staged.item.revision
        ):
            raise RepositoryError(
                "the lifecycle repository staged the wrong restoration",
                code="lifecycle_repository_content_mismatch",
            )

    @staticmethod
    def _receipt(
        unit: ItemLifecycleUnitOfWorkPort, operation_id: str
    ) -> ItemLifecycleReceipt | None:
        prior = unit.receipt(operation_id)
        if prior is None:
            return None
        if not isinstance(prior, ItemLifecycleReceipt):
            raise RepositoryError(
                "the lifecycle repository returned an invalid receipt",
                code="invalid_item_lifecycle_receipt",
            )
        if prior.operation_id != operation_id:
            raise RepositoryError(
                "the lifecycle repository returned another operation receipt",
                code="receipt_scope_mismatch",
            )
        return prior

    @classmethod
    def _replay_delete(
        cls,
        unit: ItemLifecycleUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        item_id: str,
        expected_item_revision: str,
        expected_tree_revision: str,
    ) -> ItemLifecycleResult | None:
        prior = cls._receipt(unit, operation_id)
        if prior is None:
            return None
        cls._match_replay_command(
            prior,
            operation_id=operation_id,
            command_sha256=command_sha256,
            action="delete",
        )
        if (
            prior.item_id != item_id
            or prior.deleted_item_revision != expected_item_revision
            or prior.managed_tree_revision != expected_tree_revision
            or prior.restored_item_revision
            or prior.tombstone_before_revision
            or prior.tombstone.state != "deleted"
        ):
            raise RepositoryError(
                "the stored delete receipt has inconsistent preconditions",
                code="invalid_item_lifecycle_receipt",
            )
        return ItemLifecycleResult(prior, replayed=True)

    @classmethod
    def _replay_restore(
        cls,
        unit: ItemLifecycleUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        tombstone_id: str,
        expected_tombstone_revision: str,
    ) -> ItemLifecycleResult | None:
        prior = cls._receipt(unit, operation_id)
        if prior is None:
            return None
        cls._match_replay_command(
            prior,
            operation_id=operation_id,
            command_sha256=command_sha256,
            action="restore",
        )
        if (
            prior.tombstone.tombstone_id != tombstone_id
            or prior.tombstone_before_revision != expected_tombstone_revision
            or prior.tombstone.state != "restored"
            or not prior.restored_item_revision
        ):
            raise RepositoryError(
                "the stored restore receipt has inconsistent preconditions",
                code="invalid_item_lifecycle_receipt",
            )
        return ItemLifecycleResult(prior, replayed=True)

    @staticmethod
    def _match_replay_command(
        prior: ItemLifecycleReceipt,
        *,
        operation_id: str,
        command_sha256: str,
        action: ItemLifecycleAction,
    ) -> None:
        if prior.command_sha256 != command_sha256 or prior.action != action:
            raise ConflictError(
                "operation id was already used for another lifecycle command",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )

    @staticmethod
    def _repository_failure(exc: Exception) -> RepositoryError:
        return RepositoryError(
            "the item lifecycle repository failed",
            code="item_lifecycle_repository_unavailable",
            details={"cause_type": type(exc).__name__},
            retryable=True,
        )


__all__ = [
    "DeleteItemCommand",
    "ItemLifecycleAction",
    "ItemLifecycleReceipt",
    "ItemLifecycleRepositoryPort",
    "ItemLifecycleResult",
    "ItemLifecycleService",
    "ItemLifecycleUnitOfWorkPort",
    "ItemTombstoneSnapshot",
    "LifecycleItemSnapshot",
    "ManagedTreeSnapshot",
    "RestoreItemCommand",
    "StagedItemRestoration",
    "TombstoneState",
]
