"""Recoverable filesystem repository for framework-neutral item commands.

The adapter stores one mapping-shaped catalogue JSON document beneath a
configured :class:`RecoverableWriteSet` root. Record interpretation remains
outside this module: injected codec callbacks translate transitional or future
storage rows to and from the engine's canonical item snapshots.

Every unit of work holds the cross-process workspace lease before entering an
optional legacy in-process lock. Mutations remain memory-only until ``commit``;
the catalogue, global operation receipt, and (for delete) server-held tombstone
then publish or roll back as one recoverable write set.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager

from ...engine.errors import RepositoryError
from ...engine.item_commands import (
    ItemDeletionSnapshot,
    ItemDraft,
    ItemMutationReceipt,
    ItemRecordSnapshot,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    RecoverableWriteTransaction,
    WriteSetError,
)


RecordDecoder = Callable[[str, Mapping[str, Any]], ItemRecordSnapshot]
RecordEncoder = Callable[
    [str, ItemDraft, Mapping[str, Any] | None],
    Mapping[str, Any],
]
ItemIdAllocator = Callable[[frozenset[str]], str]
ItemIdValidator = Callable[[str], Any]
TombstoneIdAllocator = Callable[[frozenset[str]], str]
LockContextFactory = Callable[[], ContextManager[None]]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FILE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/item-commands")
_TOMBSTONE_ROOT = PurePosixPath(".engine/tombstones/items")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _json_string(value: Any, *, key: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError("JSON strings and object keys must be text")
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("JSON text contains an unpaired surrogate")
        if codepoint == 127 or (
            codepoint < 32 and (key or character not in "\n\r\t")
        ):
            raise ValueError("JSON text contains a control character")
    return value


def _strict_plain(
    value: Any,
    *,
    active: set[int] | None = None,
) -> Any:
    """Detach JSON-compatible callback/parser data into plain containers."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("JSON contains a non-finite number")
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON contains a reference cycle")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = _json_string(raw_key, key=True)
                if key in result:
                    raise ValueError("JSON mapping yields a duplicate key")
                result[key] = _strict_plain(item, active=active)
            return result
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON contains a reference cycle")
        active.add(identity)
        try:
            return [_strict_plain(item, active=active) for item in value]
        finally:
            active.remove(identity)
    raise TypeError(f"JSON contains {type(value).__name__}")


def _json_bytes(value: Any, *, artifact: str) -> bytes:
    try:
        return json.dumps(
            _strict_plain(value),
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RepositoryError(
            "an item repository artifact cannot be serialized",
            code="invalid_item_repository_artifact",
            details={"artifact": artifact, "cause_type": type(exc).__name__},
        ) from exc


def _read_json(path: Path, default: Any, *, artifact: str) -> Any:
    if not path.exists():
        return _strict_plain(default)
    if not path.is_file():
        raise RepositoryError(
            "an item repository artifact is not a regular file",
            code="invalid_item_repository_artifact",
            details={"artifact": artifact},
        )
    try:
        payload = path.read_bytes().decode("utf-8")
        value = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {constant}")
            ),
        )
        return _strict_plain(value)
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        raise RepositoryError(
            "an item repository artifact cannot be read",
            code="invalid_item_repository_artifact",
            details={"artifact": artifact, "cause_type": type(exc).__name__},
        ) from exc


def _is_redirecting_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if os.name == "nt" and os.path.lexists(path):
        try:
            attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
        except OSError:
            return False
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        return bool(reparse and attributes & reparse)
    return False


def _safe_cause(error: Exception, *, code: str, message: str) -> RepositoryError:
    return RepositoryError(
        message,
        code=code,
        details={"cause_type": type(error).__name__},
        retryable=True,
    )


class FilesystemItemCommandRepository:
    """Open catalogue-wide recoverable item command units of work.

    ``encode_record`` receives ``(item_id, draft, previous_raw_record)``. The
    previous record is a detached plain mapping or ``None`` for create, which
    lets transitional codecs retain storage-only fields without exposing them
    to the engine DTO. ``decode_record`` must return the exact canonical state
    represented by a raw row. ``allocate_item_id`` runs under both locks and
    receives the complete frozen set of current item identifiers.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        catalogue_path: str | Path,
        decode_record: RecordDecoder,
        encode_record: RecordEncoder,
        allocate_item_id: ItemIdAllocator,
        validate_item_id: ItemIdValidator | None = None,
        lock_context_for: LockContextFactory | None = None,
        allocate_tombstone_id: TombstoneIdAllocator | None = None,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (decode_record, "decode_record"),
            (encode_record, "encode_record"),
            (allocate_item_id, "allocate_item_id"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if lock_context_for is not None and not callable(lock_context_for):
            raise TypeError("lock_context_for must be callable")
        if validate_item_id is not None and not callable(validate_item_id):
            raise TypeError("validate_item_id must be callable")
        if allocate_tombstone_id is not None and not callable(
            allocate_tombstone_id
        ):
            raise TypeError("allocate_tombstone_id must be callable")

        self._write_set = write_set
        self._decode_record = decode_record
        self._encode_record = encode_record
        self._allocate_item_id = allocate_item_id
        self._validate_item_id = validate_item_id
        self._allocate_tombstone_id = allocate_tombstone_id
        self._lock_context_for = lock_context_for or (lambda: nullcontext())
        self._catalogue_relative = self._catalogue_path(catalogue_path)
        self._safe_target(self._catalogue_relative, artifact="catalogue")
        if recover:
            try:
                # Recovery is a workspace mutation too.  Acquire locks in the
                # same order as command units so lazy composition cannot race
                # a transitional writer that still knows only the legacy lock.
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except WriteSetError as exc:
                raise _safe_cause(
                    exc,
                    code="item_repository_recovery_failed",
                    message="the item repository could not recover",
                ) from exc

    @contextmanager
    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> Iterator["FilesystemItemCommandUnitOfWork"]:
        self._identifier(operation_id, field_name="operation_id")
        # This ordering is load-bearing while legacy writers still use their
        # own in-process catalogue lock without the workspace lease.
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    unit = self.open_locked_unit(operation_id=operation_id)
                    try:
                        yield unit
                    finally:
                        # A snapshot is valid only while both locks are held.
                        # Retained units must never publish after this scope.
                        unit.close()
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the item repository workspace is unavailable",
            ) from exc

    def open_locked_unit(
        self,
        *,
        operation_id: str,
    ) -> "FilesystemItemCommandUnitOfWork":
        """Build a unit while a composite caller already holds both locks.

        This is an adapter-composition seam, not an alternative locking API.
        The caller must hold this repository's workspace lease and the broad
        catalogue lock until it closes the returned unit.
        """

        self._identifier(operation_id, field_name="operation_id")
        return FilesystemItemCommandUnitOfWork(
            self._write_set,
            operation_id=operation_id,
            catalogue_relative=self._catalogue_relative,
            safe_target=self._safe_target,
            decode_record=self._decode_record,
            encode_record=self._encode_record,
            allocate_item_id=self._allocate_item_id,
            validate_item_id=self._validate_item_id,
            allocate_tombstone_id=self._allocate_tombstone_id,
        )

    @property
    def catalogue_relative(self) -> str:
        """Return the normalized catalogue target for adapter composition."""

        return self._catalogue_relative

    def target_path(self, relative: str, *, artifact: str) -> Path:
        """Resolve a transaction-relative target through shared hardening."""

        return self._safe_target(relative, artifact=artifact)

    def _catalogue_path(self, value: str | Path) -> str:
        configured = Path(value)
        candidate = configured if configured.is_absolute() else (
            self._write_set.root / configured
        )
        # absolute() is lexical. resolve() would follow a redirect before the
        # component walk below had a chance to reject it.
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(self._write_set.root)
        except ValueError as exc:
            raise RepositoryError(
                "the item catalogue escapes the repository root",
                code="unsafe_item_repository_path",
                details={"artifact": "catalogue"},
            ) from exc
        if not relative.parts or candidate.suffix.lower() != ".json":
            raise RepositoryError(
                "the item catalogue path is invalid",
                code="unsafe_item_repository_path",
                details={"artifact": "catalogue"},
            )
        pure = PurePosixPath(relative.as_posix())
        if (
            pure.parts[0].casefold() == ".transactions"
            or self._reserved_catalogue(pure)
        ):
            raise RepositoryError(
                "the item catalogue uses a reserved repository path",
                code="unsafe_item_repository_path",
                details={"artifact": "catalogue"},
            )
        return pure.as_posix()

    @staticmethod
    def _reserved_catalogue(path: PurePosixPath) -> bool:
        parts = tuple(part.casefold() for part in path.parts)
        receipts = tuple(part.casefold() for part in _RECEIPT_ROOT.parts)
        tombstones = tuple(part.casefold() for part in _TOMBSTONE_ROOT.parts)
        return (
            parts[0] == ".engine"
            or parts[: len(receipts)] == receipts
            or parts[: len(tombstones)] == tombstones
        )

    def _safe_target(self, relative: str, *, artifact: str) -> Path:
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any("\\" in part or ":" in part for part in pure.parts)
        ):
            raise RepositoryError(
                "an item repository target is unsafe",
                code="unsafe_item_repository_path",
                details={"artifact": artifact},
            )
        target = self._write_set.root.joinpath(*pure.parts)
        current = self._write_set.root
        for part in pure.parts:
            current = current / part
            if _is_redirecting_path(current):
                raise RepositoryError(
                    "an item repository target crosses a redirecting path",
                    code="unsafe_item_repository_path",
                    details={"artifact": artifact},
                )
        try:
            target.resolve(strict=False).relative_to(self._write_set.root)
        except (OSError, ValueError) as exc:
            raise RepositoryError(
                "an item repository target escapes its root",
                code="unsafe_item_repository_path",
                details={"artifact": artifact},
            ) from exc
        return target

    @staticmethod
    def _identifier(value: Any, *, field_name: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise RepositoryError(
                f"{field_name} is not a portable identifier",
                code="invalid_item_repository_identity",
                details={"field": field_name},
            )
        return value


class FilesystemItemCommandUnitOfWork:
    """One locked catalogue snapshot and memory-only staging buffer."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        catalogue_relative: str,
        safe_target: Callable[..., Path],
        decode_record: RecordDecoder,
        encode_record: RecordEncoder,
        allocate_item_id: ItemIdAllocator,
        validate_item_id: ItemIdValidator | None,
        allocate_tombstone_id: TombstoneIdAllocator | None,
    ) -> None:
        self._write_set = write_set
        self._operation_id = operation_id
        self._catalogue_relative = catalogue_relative
        self._safe_target = safe_target
        self._decode_record_callback = decode_record
        self._encode_record_callback = encode_record
        self._allocate_item_id_callback = allocate_item_id
        self._validate_item_id_callback = validate_item_id
        self._allocate_tombstone_id_callback = allocate_tombstone_id
        self._catalogue, self._snapshots = self._load_catalogue()
        self._allocated_item_id = ""
        self._staged_action = ""
        self._staged_item_id = ""
        self._staged_catalogue: dict[str, Any] | None = None
        self._staged_snapshot: ItemRecordSnapshot | None = None
        self._staged_deletion: ItemDeletionSnapshot | None = None
        self._deleted_raw: dict[str, Any] | None = None
        self._publication_staged = False
        self._committed = False
        self._closed = False

    def receipt(self, operation_id: str) -> ItemMutationReceipt | None:
        self._ensure_open()
        if operation_id != self._operation_id:
            raise RepositoryError(
                "the receipt request is outside this operation",
                code="receipt_scope_mismatch",
            )
        path = self._receipt_path(operation_id)
        if not path.exists():
            return None
        raw = _read_json(path, None, artifact="item_command_receipt")
        try:
            receipt = ItemMutationReceipt.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "an item command receipt is invalid",
                code="invalid_item_mutation_receipt",
                details={"cause_type": type(exc).__name__},
            ) from exc
        if receipt.operation_id != operation_id:
            raise RepositoryError(
                "the stored receipt belongs to another operation",
                code="receipt_scope_mismatch",
            )
        return receipt

    def get(self, item_id: str) -> ItemRecordSnapshot | None:
        self._ensure_open()
        self._item_id(item_id, field_name="item_id")
        return self._snapshots.get(item_id)

    def raw_record(self, item_id: str) -> Mapping[str, Any] | None:
        """Return one detached storage record to a composing adapter.

        This is deliberately not part of the framework-neutral item command
        port.  A separately installed aggregate (for example representation
        attachment) may own transitional storage fields that the catalogue
        item DTO intentionally hides.  Such an adapter can share this locked
        snapshot without learning how the catalogue document is stored.
        """

        self._ensure_open()
        self._item_id(item_id, field_name="item_id")
        raw = self._catalogue.get(item_id)
        return None if raw is None else _strict_plain(raw)

    def allocate_item_id(self) -> str:
        self._ensure_stageable()
        if self._allocated_item_id:
            return self._allocated_item_id
        try:
            value = self._allocate_item_id_callback(
                frozenset(self._catalogue)
            )
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="item_id_allocation_failed",
                message="the item repository could not allocate an identity",
            ) from exc
        item_id = self._item_id(value, field_name="allocated_item_id")
        folded = {existing.casefold() for existing in self._catalogue}
        if item_id.casefold() in folded:
            raise RepositoryError(
                "the item repository allocated an existing identity",
                code="allocated_item_id_collision",
                details={"item_id": item_id},
                retryable=True,
            )
        self._allocated_item_id = item_id
        return item_id

    def stage_create(
        self,
        item_id: str,
        draft: ItemDraft,
    ) -> ItemRecordSnapshot:
        self._ensure_stageable()
        self._item_id(item_id, field_name="item_id")
        if not isinstance(draft, ItemDraft):
            raise RepositoryError(
                "the item create draft is invalid",
                code="invalid_item_repository_command",
            )
        if item_id.casefold() in {
            existing.casefold() for existing in self._catalogue
        }:
            raise RepositoryError(
                "the item already exists",
                code="item_already_exists",
                details={"item_id": item_id},
            )
        raw, snapshot = self._encode_record(item_id, draft, None)
        catalogue = _strict_plain(self._catalogue)
        catalogue[item_id] = raw
        self._stage(
            action="create",
            item_id=item_id,
            catalogue=catalogue,
            snapshot=snapshot,
        )
        return snapshot

    def stage_replace(
        self,
        current: ItemRecordSnapshot,
        draft: ItemDraft,
    ) -> ItemRecordSnapshot:
        self._ensure_stageable()
        if not isinstance(current, ItemRecordSnapshot) or not isinstance(
            draft,
            ItemDraft,
        ):
            raise RepositoryError(
                "the item replacement input is invalid",
                code="invalid_item_repository_command",
            )
        stored = self._snapshots.get(current.item_id)
        if stored != current:
            raise RepositoryError(
                "the item replacement is outside the locked snapshot",
                code="item_repository_scope_mismatch",
                details={"item_id": current.item_id},
            )
        previous = self._catalogue[current.item_id]
        raw, snapshot = self._encode_record(current.item_id, draft, previous)
        catalogue = _strict_plain(self._catalogue)
        catalogue[current.item_id] = raw
        self._stage(
            action="update",
            item_id=current.item_id,
            catalogue=catalogue,
            snapshot=snapshot,
        )
        return snapshot

    def stage_managed_record(
        self,
        item_id: str,
        raw_record: Mapping[str, Any],
    ) -> ItemRecordSnapshot:
        """Stage a storage-managed record for a composing aggregate.

        The caller owns validation of its managed fields.  This unit still
        enforces catalogue identity, strict JSON detachment, and the shared
        record decoder before allowing the record into the publication set.
        The caller must stage its own receipt and then call
        :meth:`stage_catalogue_publication` in the same recoverable write set.
        """

        self._ensure_stageable()
        self._item_id(item_id, field_name="item_id")
        if item_id not in self._snapshots:
            raise RepositoryError(
                "the managed item does not exist",
                code="item_not_found",
                details={"item_id": item_id},
            )
        try:
            raw = _strict_plain(raw_record)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "the managed item record is invalid",
                code="invalid_item_repository_artifact",
                details={"artifact": "managed_record"},
            ) from exc
        if not isinstance(raw, dict):
            raise RepositoryError(
                "the managed item record is not an object",
                code="invalid_item_repository_artifact",
                details={"artifact": "managed_record"},
            )
        snapshot = self._decode_record(item_id, raw)
        catalogue = _strict_plain(self._catalogue)
        catalogue[item_id] = raw
        self._stage(
            action="managed",
            item_id=item_id,
            catalogue=catalogue,
            snapshot=snapshot,
        )
        return snapshot

    def stage_delete(
        self,
        current: ItemRecordSnapshot,
    ) -> ItemDeletionSnapshot:
        self._ensure_stageable()
        if not isinstance(current, ItemRecordSnapshot):
            raise RepositoryError(
                "the item deletion input is invalid",
                code="invalid_item_repository_command",
            )
        stored = self._snapshots.get(current.item_id)
        if stored != current:
            raise RepositoryError(
                "the item deletion is outside the locked snapshot",
                code="item_repository_scope_mismatch",
                details={"item_id": current.item_id},
            )
        tombstone_id = self._new_tombstone_id()
        deletion = ItemDeletionSnapshot(
            current.item_id,
            current.revision,
            tombstone_id,
        )
        catalogue = _strict_plain(self._catalogue)
        raw = catalogue.pop(current.item_id)
        self._stage(
            action="delete",
            item_id=current.item_id,
            catalogue=catalogue,
            deletion=deletion,
            deleted_raw=raw,
        )
        return deletion

    def commit(self, receipt: ItemMutationReceipt) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the item command unit is already committed",
                code="item_command_unit_committed",
            )
        try:
            transaction = self._write_set.begin(
                operation_id=self._operation_id,
                scope="item-command",
                metadata={
                    "action": self._staged_action,
                    "item_id": self._staged_item_id,
                },
            )
            self.stage_publication(transaction, receipt)
            transaction.commit(receipt=receipt.as_dict())
        except WriteSetError as exc:
            raise _safe_cause(
                exc,
                code=exc.code,
                message="the item repository transaction failed",
            ) from exc
        self._committed = True

    def stage_publication(
        self,
        transaction: RecoverableWriteTransaction,
        receipt: ItemMutationReceipt,
    ) -> None:
        """Stage this mutation into a caller-owned workspace transaction.

        This package-level composition seam lets a larger filesystem aggregate
        publish an item mutation without nesting another recoverable write set.
        Call it only while the repository's workspace and catalogue locks are
        still held.  The catalogue write is deliberately appended last, so a
        composite caller must stage every other artifact before invoking it.
        """

        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the item command unit is already committed",
                code="item_command_unit_committed",
            )
        if self._publication_staged:
            raise RepositoryError(
                "the item mutation publication is already staged",
                code="item_mutation_already_staged",
            )
        if not isinstance(transaction, RecoverableWriteTransaction) or (
            transaction._owner is not self._write_set
        ):
            raise RepositoryError(
                "the item mutation transaction belongs to another workspace",
                code="item_repository_scope_mismatch",
            )
        if not self._staged_action or self._staged_catalogue is None:
            raise RepositoryError(
                "no item mutation has been staged",
                code="item_mutation_not_staged",
            )
        self._validate_receipt(receipt)
        if self.receipt(self._operation_id) is not None:
            raise RepositoryError(
                "an item command receipt already exists",
                code="item_command_receipt_exists",
            )

        if self._staged_action == "delete":
            assert self._staged_deletion is not None
            assert self._deleted_raw is not None
            transaction.stage_write(
                self._tombstone_relative(self._staged_deletion.tombstone_id),
                _json_bytes(
                    {
                        "schema": "librarytool.item-tombstone/1",
                        "tombstone_id": self._staged_deletion.tombstone_id,
                        "item_id": self._staged_item_id,
                        "prior_revision": receipt.before_revision,
                        "operation_id": self._operation_id,
                        "command_sha256": receipt.command_sha256,
                        "record": self._deleted_raw,
                    },
                    artifact="item_tombstone",
                ),
            )
        transaction.stage_write(
            self._receipt_relative(self._operation_id),
            _json_bytes(
                receipt.as_dict(),
                artifact="item_command_receipt",
            ),
        )
        self.stage_catalogue_publication(transaction)

    def stage_catalogue_publication(
        self,
        transaction: RecoverableWriteTransaction,
    ) -> None:
        """Append only the staged catalogue write to a composite transaction.

        Composing adapters stage their aggregate artifacts and durable receipt
        first, then call this method so legacy readers cannot observe the new
        catalogue state before its supporting transaction data is live.
        """

        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the item command unit is already committed",
                code="item_command_unit_committed",
            )
        if self._publication_staged:
            raise RepositoryError(
                "the item mutation publication is already staged",
                code="item_mutation_already_staged",
            )
        if not isinstance(transaction, RecoverableWriteTransaction) or (
            transaction._owner is not self._write_set
        ):
            raise RepositoryError(
                "the item mutation transaction belongs to another workspace",
                code="item_repository_scope_mismatch",
            )
        if not self._staged_action or self._staged_catalogue is None:
            raise RepositoryError(
                "no item mutation has been staged",
                code="item_mutation_not_staged",
            )
        # Legacy readers that do not yet take the workspace lease cannot
        # discover a new state before all transaction metadata exists;
        # rollback removes this last-published row first.
        transaction.stage_write(
            self._catalogue_relative,
            _json_bytes(self._staged_catalogue, artifact="catalogue"),
        )
        self._publication_staged = True

    def close(self) -> None:
        """Invalidate this locked snapshot when its repository scope exits."""

        self._closed = True

    def _load_catalogue(
        self,
    ) -> tuple[dict[str, Any], dict[str, ItemRecordSnapshot]]:
        path = self._safe_target(
            self._catalogue_relative,
            artifact="catalogue",
        )
        value = _read_json(path, {}, artifact="catalogue")
        if not isinstance(value, dict):
            raise RepositoryError(
                "the item catalogue is not an object",
                code="invalid_item_catalogue",
            )
        snapshots: dict[str, ItemRecordSnapshot] = {}
        aliases: dict[str, str] = {}
        for item_id, raw in value.items():
            self._item_id(item_id, field_name="catalogue_item_id")
            alias = item_id.casefold()
            if alias in aliases:
                raise RepositoryError(
                    "the item catalogue contains aliased identities",
                    code="invalid_item_catalogue",
                    details={"item_ids": [aliases[alias], item_id]},
                )
            aliases[alias] = item_id
            if not isinstance(raw, dict):
                raise RepositoryError(
                    "the item catalogue contains a non-object record",
                    code="invalid_item_catalogue",
                    details={"item_id": item_id},
                )
            snapshots[item_id] = self._decode_record(item_id, raw)
        return value, snapshots

    def _decode_record(
        self,
        item_id: str,
        raw: Mapping[str, Any],
    ) -> ItemRecordSnapshot:
        try:
            result = self._decode_record_callback(
                item_id,
                _strict_plain(raw),
            )
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="item_record_codec_failed",
                message="the item repository could not decode a record",
            ) from exc
        if not isinstance(result, ItemRecordSnapshot):
            raise RepositoryError(
                "the item record decoder returned an invalid snapshot",
                code="invalid_item_record_snapshot",
                details={"item_id": item_id},
            )
        if result.item_id != item_id:
            raise RepositoryError(
                "the item record decoder returned another item",
                code="item_repository_scope_mismatch",
                details={
                    "catalogue_item_id": item_id,
                    "decoded_item_id": result.item_id,
                },
            )
        return result

    def _encode_record(
        self,
        item_id: str,
        draft: ItemDraft,
        previous: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], ItemRecordSnapshot]:
        try:
            result = self._encode_record_callback(
                item_id,
                draft,
                None if previous is None else _strict_plain(previous),
            )
            raw = _strict_plain(result)
        except Exception as exc:
            raise _safe_cause(
                exc,
                code="item_record_codec_failed",
                message="the item repository could not encode a record",
            ) from exc
        if not isinstance(raw, dict):
            raise RepositoryError(
                "the item record encoder returned a non-object",
                code="item_record_codec_failed",
                details={"cause_type": "InvalidReturnType"},
            )
        snapshot = self._decode_record(item_id, raw)
        if snapshot.as_draft() != draft:
            raise RepositoryError(
                "the item record codec changed canonical content",
                code="item_record_codec_mismatch",
                details={"item_id": item_id},
            )
        return raw, snapshot

    def _stage(
        self,
        *,
        action: str,
        item_id: str,
        catalogue: dict[str, Any],
        snapshot: ItemRecordSnapshot | None = None,
        deletion: ItemDeletionSnapshot | None = None,
        deleted_raw: dict[str, Any] | None = None,
    ) -> None:
        self._staged_action = action
        self._staged_item_id = item_id
        self._staged_catalogue = catalogue
        self._staged_snapshot = snapshot
        self._staged_deletion = deletion
        self._deleted_raw = deleted_raw

    def _validate_receipt(self, receipt: ItemMutationReceipt) -> None:
        if not isinstance(receipt, ItemMutationReceipt):
            raise RepositoryError(
                "the item command receipt is invalid",
                code="invalid_item_mutation_receipt",
            )
        if (
            receipt.operation_id != self._operation_id
            or receipt.action != self._staged_action
            or receipt.item_id != self._staged_item_id
        ):
            raise RepositoryError(
                "the item command receipt is outside the staged mutation",
                code="receipt_scope_mismatch",
            )
        if self._staged_action in {"create", "update"}:
            if receipt.item != self._staged_snapshot:
                raise RepositoryError(
                    "the receipt item does not match the staged record",
                    code="receipt_scope_mismatch",
                )
            if self._staged_action == "update" and (
                receipt.before_revision
                != self._snapshots[self._staged_item_id].revision
            ):
                raise RepositoryError(
                    "the receipt does not match the replaced revision",
                    code="receipt_scope_mismatch",
                )
        elif receipt.deletion != self._staged_deletion:
            raise RepositoryError(
                "the receipt deletion does not match the staged tombstone",
                code="receipt_scope_mismatch",
            )

    def _new_tombstone_id(self) -> str:
        root = self._safe_target(
            _TOMBSTONE_ROOT.as_posix(),
            artifact="item_tombstones",
        )
        existing_names: dict[str, str] = {}
        if root.exists() and not root.is_dir():
            raise RepositoryError(
                "the item tombstone store is not a directory",
                code="invalid_item_tombstone_store",
            )
        if root.is_dir():
            for path in root.iterdir():
                if path.suffix.casefold() != ".json":
                    continue
                if _is_redirecting_path(path):
                    raise RepositoryError(
                        "the item tombstone store contains a redirecting path",
                        code="unsafe_item_repository_path",
                        details={"artifact": "item_tombstones"},
                    )
                if not path.is_file():
                    raise RepositoryError(
                        "the item tombstone store contains an invalid entry",
                        code="invalid_item_tombstone_store",
                    )
                tombstone_id = self._file_identifier(
                    path.stem,
                    field_name="stored_tombstone_id",
                )
                folded = tombstone_id.casefold()
                if folded in existing_names:
                    raise RepositoryError(
                        "the item tombstone store contains aliased identities",
                        code="invalid_item_tombstone_store",
                    )
                existing_names[folded] = tombstone_id
        existing = frozenset(existing_names.values())
        if self._allocate_tombstone_id_callback is not None:
            try:
                value = self._allocate_tombstone_id_callback(existing)
            except Exception as exc:
                raise _safe_cause(
                    exc,
                    code="tombstone_id_allocation_failed",
                    message="the item repository could not allocate a tombstone",
                ) from exc
            result = self._file_identifier(value, field_name="tombstone_id")
            if result.casefold() in existing_names:
                raise RepositoryError(
                    "the item repository allocated an existing tombstone",
                    code="tombstone_id_collision",
                    retryable=True,
                )
            return result
        while True:
            result = "itm-" + uuid.uuid4().hex
            if result.casefold() not in existing_names:
                return result

    def _receipt_relative(self, operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return (_RECEIPT_ROOT / f"{digest}.json").as_posix()

    def _receipt_path(self, operation_id: str) -> Path:
        return self._safe_target(
            self._receipt_relative(operation_id),
            artifact="item_command_receipt",
        )

    @staticmethod
    def _tombstone_relative(tombstone_id: str) -> str:
        return (_TOMBSTONE_ROOT / f"{tombstone_id}.json").as_posix()

    @staticmethod
    def _identifier(value: Any, *, field_name: str) -> str:
        if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
            raise RepositoryError(
                f"{field_name} is not a portable identifier",
                code="invalid_item_repository_identity",
                details={"field": field_name},
            )
        return value

    def _item_id(self, value: Any, *, field_name: str) -> str:
        item_id = self._identifier(value, field_name=field_name)
        if self._validate_item_id_callback is None:
            return item_id
        try:
            self._validate_item_id_callback(item_id)
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                f"{field_name} is unsupported by this filesystem layout",
                code="invalid_item_repository_identity",
                details={"field": field_name, "cause_type": type(exc).__name__},
            ) from exc
        return item_id

    @staticmethod
    def _file_identifier(value: Any, *, field_name: str) -> str:
        if (
            not isinstance(value, str)
            or not _FILE_IDENTIFIER_RE.fullmatch(value)
            or value.endswith(".")
            or value.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
        ):
            raise RepositoryError(
                f"{field_name} is not a portable filesystem identifier",
                code="invalid_item_repository_identity",
                details={"field": field_name},
            )
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise RepositoryError(
                "the item command unit is closed",
                code="item_command_unit_closed",
            )

    def _ensure_stageable(self) -> None:
        self._ensure_open()
        if self._committed:
            raise RepositoryError(
                "the item command unit is already committed",
                code="item_command_unit_committed",
            )
        if self._staged_action:
            raise RepositoryError(
                "an item mutation is already staged",
                code="item_mutation_already_staged",
            )


__all__ = [
    "FilesystemItemCommandRepository",
    "FilesystemItemCommandUnitOfWork",
]
