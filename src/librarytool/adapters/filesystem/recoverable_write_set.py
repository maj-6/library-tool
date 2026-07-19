"""Crash-recoverable publication of a set of filesystem artifacts.

There is no portable filesystem primitive that atomically replaces several
unrelated files.  This module provides the next-best storage guarantee for the
filesystem adapters: every target has a durable before-image and after-image,
the intended operation is journaled before live files are touched, an ordinary
failure is rolled back synchronously, and an interrupted operation can be
recovered deterministically on the next start.

The primitive deliberately deals in paths relative to one configured root.  A
domain repository should map logical artifacts to those paths; application
services should not depend on this module or learn the on-disk layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, BinaryIO, Iterator, Literal

from ...engine.errors import RepositoryError


JournalState = Literal[
    "prepared",
    "applying",
    "committed",
    "rolled_back",
    "recovery_required",
]
PublishHook = Callable[[int, Path], None]

_JOURNAL_VERSION = 1
_TRANSACTIONS_NAME = ".transactions"
_JOURNAL_NAME = "journal.json"
_KNOWN_JOURNAL_STATES = frozenset(
    {
        "prepared",
        "applying",
        "committed",
        "rolled_back",
        "recovery_required",
    }
)
_BLOCKING_JOURNAL_STATES = frozenset({"applying", "recovery_required"})


class WriteSetError(RepositoryError):
    """Base error raised by the recoverable write-set adapter."""


class UnsafeTargetError(WriteSetError):
    """A target escaped the configured root or crossed a symbolic link."""

    default_code = "unsafe_write_set_target"


class RecoveryRequiredError(WriteSetError):
    """Recovery found live content that is neither known pre- nor post-state."""

    default_code = "write_set_recovery_required"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Outcome of inspecting or recovering one retained transaction."""

    transaction_id: str
    previous_state: str
    state: JournalState
    action: str


@dataclass(frozen=True, slots=True)
class _StagedOperation:
    target: str
    payload: bytes | None


_process_locks_guard = threading.Lock()
_process_locks: dict[str, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _process_locks_guard:
        return _process_locks.setdefault(key, threading.RLock())


class RecoverableWriteSet:
    """Factory and recovery coordinator for transactions under one root.

    ``root`` is the only tree transactions may modify.  Journals and their
    payloads live in ``root/.transactions`` so preimages and postimages stay on
    the same storage device as normal workspace data.

    ``publish_hook`` is an instrumentation seam.  It runs immediately before
    each forward publication and is useful for fault-injection tests.  Normal
    ``Exception`` failures trigger rollback; a process crash naturally bypasses
    Python cleanup, which restart recovery handles from the journal.
    """

    def __init__(
        self,
        root: Path,
        *,
        publish_hook: PublishHook | None = None,
    ) -> None:
        configured = Path(root)
        configured.mkdir(parents=True, exist_ok=True)
        self.root = configured.resolve()
        self.transactions_dir = self.root / _TRANSACTIONS_NAME
        if _is_redirecting_path(self.transactions_dir):
            raise UnsafeTargetError(
                "the transaction directory may not redirect through a link",
                details={"path": str(self.transactions_dir)},
            )
        self.transactions_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if _is_redirecting_path(self.transactions_dir):
            raise UnsafeTargetError(
                "the transaction directory may not redirect through a link",
                details={"path": str(self.transactions_dir)},
            )
        self.lock_path = self.transactions_dir / "workspace.lock"
        self._thread_lock = _process_lock(self.lock_path)
        self._publish_hook = publish_hook

    def begin(
        self,
        *,
        operation_id: str = "",
        scope: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "RecoverableWriteTransaction":
        """Create an unprepared transaction.

        ``operation_id`` is stored as data, never used as a path.  A future
        repository can use it to retain/replay an import receipt without
        changing this publication primitive.
        """
        with self._workspace_lock():
            self._assert_recovery_clear_locked()
            return RecoverableWriteTransaction(
                self,
                transaction_id=uuid.uuid4().hex,
                operation_id=str(operation_id),
                scope=str(scope),
                metadata=dict(metadata or {}),
            )

    def recover_all(self) -> tuple[RecoveryResult, ...]:
        """Recover every retained transaction in deterministic order.

        A directory without a valid journal can only be an interrupted prepare:
        live targets are not touched until the ``prepared`` journal is durable,
        so such a directory is safe to discard.
        """
        results: list[RecoveryResult] = []
        with self._workspace_lock():
            for directory in sorted(self.transactions_dir.iterdir()):
                if directory == self.lock_path or not directory.is_dir():
                    continue
                if _is_redirecting_path(directory):
                    raise RecoveryRequiredError(
                        "a transaction record redirects through a link",
                        details={"path": str(directory)},
                    )
                journal_path = directory / _JOURNAL_NAME
                if not journal_path.is_file():
                    shutil.rmtree(directory)
                    results.append(
                        RecoveryResult(
                            transaction_id=directory.name,
                            previous_state="preparing",
                            state="rolled_back",
                            action="discarded_incomplete_prepare",
                        )
                    )
                    continue
                journal = self._read_journal(directory)
                results.append(self._recover_locked(directory, journal))
        return tuple(results)

    def recover(self, transaction_id: str) -> RecoveryResult:
        """Recover one retained transaction by its generated identifier."""
        with self._workspace_lock():
            directory = self._transaction_directory(transaction_id)
            journal = self._read_journal(directory)
            return self._recover_locked(directory, journal)

    def cleanup(self, transaction_id: str) -> None:
        """Remove a safely terminal journal and its retained payloads.

        Committed and rolled-back journals are intentionally retained by
        default so callers may keep receipts or inspect recovery.  Their
        sensitive payload copies are discarded as soon as they become
        terminal.  Cleanup refuses non-terminal or unresolved transactions.
        """
        with self._workspace_lock():
            directory = self._transaction_directory(transaction_id)
            journal = self._read_journal(directory)
            state = str(journal.get("state") or "")
            if state not in {"committed", "rolled_back"}:
                raise WriteSetError(
                    "only a terminal transaction can be cleaned up",
                    code="write_set_not_terminal",
                    details={"transaction_id": transaction_id, "state": state},
                )
            shutil.rmtree(directory)
            _fsync_directory(self.transactions_dir)

    def _recover_locked(
        self, directory: Path, journal: dict[str, Any]
    ) -> RecoveryResult:
        transaction_id = str(journal["transaction_id"])
        previous = str(journal.get("state") or "")
        if previous == "prepared":
            # A prepared journal is the publication boundary: live files are
            # untouched until the state advances to applying.  Abandon it
            # without comparing targets, which may have been legitimately
            # changed by a later transaction before this stale journal was
            # discovered.
            journal["state"] = "rolled_back"
            self._write_journal(directory, journal)
            self._discard_terminal_payloads_locked(directory)
            return RecoveryResult(
                transaction_id, previous, "rolled_back", "abandoned_prepared"
            )
        if previous == "applying":
            self._rollback_locked(directory, journal)
            return RecoveryResult(
                transaction_id, previous, "rolled_back", "rolled_back_interrupted"
            )
        if previous == "committed":
            self._discard_terminal_payloads_locked(directory)
            return RecoveryResult(
                transaction_id, previous, "committed", "already_committed"
            )
        if previous == "rolled_back":
            self._discard_terminal_payloads_locked(directory)
            return RecoveryResult(
                transaction_id, previous, "rolled_back", "already_rolled_back"
            )
        if previous == "recovery_required":
            raise RecoveryRequiredError(
                "the transaction requires manual recovery",
                details={
                    "transaction_id": transaction_id,
                    "journal": str(directory / _JOURNAL_NAME),
                },
            )
        raise RecoveryRequiredError(
            "the transaction journal has an unknown state",
            details={"transaction_id": transaction_id, "state": previous},
        )

    def _rollback_locked(self, directory: Path, journal: dict[str, Any]) -> None:
        try:
            for entry in reversed(self._entries(journal)):
                target = self._target(entry["target"])
                before = entry["before"]
                after = entry["after"]
                current = self._snapshot_description(target)
                if _same_snapshot(current, before):
                    continue
                if not _same_snapshot(current, after):
                    self._mark_recovery_required(
                        directory, journal, target, before, after, current
                    )
                self._publish_description(directory, target, before)
            self._remove_created_directories(journal)
            journal["state"] = "rolled_back"
            self._write_journal(directory, journal)
            self._discard_terminal_payloads_locked(directory)
        except RecoveryRequiredError:
            raise
        except Exception as exc:
            journal["state"] = "recovery_required"
            journal["recovery_error"] = str(exc)
            self._write_journal(directory, journal)
            raise RecoveryRequiredError(
                "could not roll back the transaction",
                details={
                    "transaction_id": journal.get("transaction_id"),
                    "cause": str(exc),
                },
                retryable=True,
            ) from exc

    def _mark_recovery_required(
        self,
        directory: Path,
        journal: dict[str, Any],
        target: Path,
        expected: Mapping[str, Any],
        alternative: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> None:
        journal["state"] = "recovery_required"
        journal["recovery_conflict"] = {
            "target": target.relative_to(self.root).as_posix(),
            "expected": dict(expected),
            "alternative": dict(alternative),
            "current": dict(current),
        }
        self._write_journal(directory, journal)
        raise RecoveryRequiredError(
            "live content is neither the transaction's before- nor after-state",
            details={
                "transaction_id": journal.get("transaction_id"),
                "target": str(target),
                "current_sha256": current.get("sha256"),
            },
        )

    def _publish_description(
        self,
        directory: Path,
        target: Path,
        description: Mapping[str, Any],
    ) -> None:
        self._target(target.relative_to(self.root).as_posix())
        if bool(description.get("exists")):
            blob_name = description.get("blob")
            if not isinstance(blob_name, str):
                raise RecoveryRequiredError(
                    "a journal payload reference is missing",
                    details={"target": str(target)},
                )
            blob = self._journal_blob(directory, blob_name)
            if _sha256_file(blob) != description.get("sha256"):
                raise RecoveryRequiredError(
                    "a journal payload does not match its recorded hash",
                    details={"target": str(target), "blob": str(blob)},
                )
            mode = description.get("mode")
            _atomic_copy(
                blob,
                target,
                mode=mode if isinstance(mode, int) else None,
            )
        else:
            if os.path.lexists(target):
                if target.is_dir() and not target.is_symlink():
                    raise RecoveryRequiredError(
                        "a file target was replaced by a directory",
                        details={"target": str(target)},
                    )
                target.unlink()
                _fsync_directory(target.parent)

    def _remove_created_directories(self, journal: Mapping[str, Any]) -> None:
        values = journal.get("created_directories") or []
        if not isinstance(values, list):
            return
        paths: list[Path] = []
        for value in values:
            if not isinstance(value, str):
                continue
            path = self._target(value, allow_directory=True)
            paths.append(path)
        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir()
                _fsync_directory(path.parent)
            except OSError:
                pass

    def _entries(self, journal: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw = journal.get("entries")
        if not isinstance(raw, list):
            raise RecoveryRequiredError(
                "the transaction journal has no entries list",
                details={"transaction_id": journal.get("transaction_id")},
            )
        entries: list[dict[str, Any]] = []
        for value in raw:
            if not isinstance(value, dict):
                raise RecoveryRequiredError(
                    "the transaction journal contains an invalid entry",
                    details={"transaction_id": journal.get("transaction_id")},
                )
            target = value.get("target")
            before = value.get("before")
            after = value.get("after")
            if (
                not isinstance(target, str)
                or not isinstance(before, dict)
                or not isinstance(after, dict)
            ):
                raise RecoveryRequiredError(
                    "the transaction journal contains an invalid entry",
                    details={"transaction_id": journal.get("transaction_id")},
                )
            self._target(target)
            entries.append(value)
        return entries

    def _discard_terminal_payloads_locked(self, directory: Path) -> None:
        """Best-effort removal of content copies no longer needed for recovery."""
        for name in ("before", "after"):
            shutil.rmtree(directory / name, ignore_errors=True)
        _fsync_directory(directory)

    def _assert_recovery_clear_locked(self) -> None:
        """Refuse new work while prior publication has an unknown outcome.

        Callers hold the workspace lock, making the inspection and the next
        transaction transition one cross-process critical section.  An
        incomplete prepare without a journal is safe to ignore because no live
        target is touched before the prepared journal is durable.
        """
        blocking: list[dict[str, str]] = []
        for directory in sorted(self.transactions_dir.iterdir()):
            if directory == self.lock_path or not directory.is_dir():
                continue
            if directory.is_symlink():
                raise RecoveryRequiredError(
                    "workspace recovery state cannot be verified",
                    details={"path": str(directory), "reason": "symlink"},
                )
            journal_path = directory / _JOURNAL_NAME
            if not journal_path.is_file():
                continue
            try:
                journal = self._read_journal(directory)
            except RecoveryRequiredError as exc:
                raise RecoveryRequiredError(
                    "workspace recovery state cannot be verified",
                    details={
                        "transaction_id": directory.name,
                        "journal": str(journal_path),
                    },
                ) from exc
            state = str(journal.get("state") or "")
            if state in _BLOCKING_JOURNAL_STATES:
                blocking.append(
                    {
                        "transaction_id": directory.name,
                        "state": state,
                        "journal": str(journal_path),
                    }
                )
            elif state not in _KNOWN_JOURNAL_STATES:
                raise RecoveryRequiredError(
                    "workspace recovery state cannot be verified",
                    details={
                        "transaction_id": directory.name,
                        "state": state,
                        "journal": str(journal_path),
                    },
                )
        if blocking:
            raise RecoveryRequiredError(
                "unfinished recovery blocks new write-set work",
                details={"root": str(self.root), "transactions": blocking},
            )

    def _target(self, relative: str | Path, *, allow_directory: bool = False) -> Path:
        value = str(relative)
        pure = PurePath(value)
        if (
            not value
            or value in {".", ".."}
            or pure.is_absolute()
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise UnsafeTargetError(
                "write-set targets must be normalized relative paths",
                details={"target": value},
            )
        # Backslashes are path separators on Windows and ambiguous data on
        # POSIX.  Use one portable journal grammar on every platform.
        if "\\" in value or ":" in value:
            raise UnsafeTargetError(
                "write-set targets must use forward slashes",
                details={"target": value},
            )
        relative_path = Path(*pure.parts)
        # Keep one portable reserved namespace.  A case variant is a distinct
        # path on some filesystems but aliases the journal directory on Windows.
        if relative_path.parts[0].casefold() == _TRANSACTIONS_NAME.casefold():
            raise UnsafeTargetError(
                "a transaction may not target its own journal tree",
                details={"target": value},
            )
        target = self.root.joinpath(relative_path)
        try:
            resolved_target = target.resolve(strict=False)
            resolved_target.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeTargetError(
                "write-set target escapes the configured root",
                details={"target": value},
            ) from exc
        try:
            resolved_target.relative_to(self.transactions_dir.resolve())
        except ValueError:
            pass
        else:
            raise UnsafeTargetError(
                "a transaction may not resolve into its own journal tree",
                details={"target": value},
            )
        self._assert_no_symlink(target)
        if not allow_directory and target.exists() and target.is_dir():
            raise UnsafeTargetError(
                "a write-set file target is a directory",
                details={"target": value},
            )
        return target

    def _assert_no_symlink(self, target: Path) -> None:
        relative = target.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if _is_redirecting_path(current):
                raise UnsafeTargetError(
                    "write-set targets may not cross redirecting links",
                    details={"target": str(target), "link": str(current)},
                )

    def _snapshot_description(self, target: Path) -> dict[str, Any]:
        self._assert_no_symlink(target)
        if not os.path.lexists(target):
            return {"exists": False, "sha256": None, "blob": None}
        if not target.is_file():
            return {
                "exists": True,
                "sha256": None,
                "blob": None,
                "kind": "non_file",
            }
        info = target.stat()
        return {
            "exists": True,
            "sha256": _sha256_file(target),
            "blob": None,
            "mode": stat.S_IMODE(info.st_mode),
        }

    def _transaction_directory(self, transaction_id: str) -> Path:
        value = str(transaction_id)
        if not value or not value.isalnum() or len(value) > 64:
            raise WriteSetError(
                "invalid transaction identifier",
                code="invalid_write_set_transaction",
                details={"transaction_id": value},
            )
        directory = self.transactions_dir / value
        if not directory.is_dir() or _is_redirecting_path(directory):
            raise WriteSetError(
                "no such write-set transaction",
                code="write_set_transaction_not_found",
                details={"transaction_id": value},
            )
        return directory

    def _read_journal(self, directory: Path) -> dict[str, Any]:
        try:
            value = json.loads((directory / _JOURNAL_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "the transaction journal cannot be read",
                details={"path": str(directory / _JOURNAL_NAME)},
            ) from exc
        if not isinstance(value, dict) or value.get("version") != _JOURNAL_VERSION:
            raise RecoveryRequiredError(
                "the transaction journal version is unsupported",
                details={"path": str(directory / _JOURNAL_NAME)},
            )
        if str(value.get("transaction_id") or "") != directory.name:
            raise RecoveryRequiredError(
                "the transaction journal identity does not match its directory",
                details={"path": str(directory / _JOURNAL_NAME)},
            )
        return value

    def _write_journal(self, directory: Path, value: Mapping[str, Any]) -> None:
        payload = (
            json.dumps(dict(value), indent=2, ensure_ascii=False, allow_nan=False)
            + "\n"
        ).encode("utf-8")
        _atomic_bytes(directory / _JOURNAL_NAME, payload)

    def _journal_blob(self, directory: Path, relative: str) -> Path:
        pure = PurePath(relative)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise RecoveryRequiredError(
                "a journal payload path is unsafe",
                details={"blob": relative},
            )
        blob = directory.joinpath(*pure.parts)
        try:
            blob.resolve(strict=True).relative_to(directory.resolve())
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "a journal payload is missing or escapes its transaction",
                details={"blob": relative},
            ) from exc
        if blob.is_symlink() or not blob.is_file():
            raise RecoveryRequiredError(
                "a journal payload is not a regular file",
                details={"blob": relative},
            )
        return blob

    @contextmanager
    def _workspace_lock(self) -> Iterator[None]:
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as stream:
                _lock_stream(stream)
                try:
                    yield
                finally:
                    _unlock_stream(stream)


class RecoverableWriteTransaction:
    """One explicit-commit write set created by :class:`RecoverableWriteSet`."""

    def __init__(
        self,
        owner: RecoverableWriteSet,
        *,
        transaction_id: str,
        operation_id: str,
        scope: str,
        metadata: dict[str, Any],
    ) -> None:
        self._owner = owner
        self.transaction_id = transaction_id
        self.operation_id = operation_id
        self.scope = scope
        self.metadata = metadata
        self._operations: dict[str, _StagedOperation] = {}
        self._prepared = False
        self._committed = False

    @property
    def journal_path(self) -> Path:
        return self._owner.transactions_dir / self.transaction_id / _JOURNAL_NAME

    def stage_write(self, target: str | Path, payload: bytes) -> None:
        """Stage the final bytes for one relative file target."""
        self._ensure_stageable()
        path = self._owner._target(target)
        relative = path.relative_to(self._owner.root).as_posix()
        self._operations[relative] = _StagedOperation(relative, bytes(payload))

    def stage_delete(self, target: str | Path) -> None:
        """Stage deletion of one relative file target."""
        self._ensure_stageable()
        path = self._owner._target(target)
        relative = path.relative_to(self._owner.root).as_posix()
        self._operations[relative] = _StagedOperation(relative, None)

    def prepare(self) -> None:
        """Persist preimages, postimages, hashes, and a ``prepared`` journal."""
        self._ensure_open()
        if self._prepared:
            return
        if not self._operations:
            raise WriteSetError(
                "a write-set transaction has no operations",
                code="empty_write_set",
                details={"transaction_id": self.transaction_id},
            )
        with self._owner._workspace_lock():
            self._owner._assert_recovery_clear_locked()
            self._prepare_locked()

    def commit(self, *, receipt: Mapping[str, Any] | None = None) -> None:
        """Publish the write set, rolling it back on an ordinary failure.

        ``BaseException`` is intentionally not intercepted: process death,
        forced cancellation, and fault-injection that models a crash leave the
        durable ``applying`` journal for a new adapter instance to recover.
        """
        self._ensure_open()
        with self._owner._workspace_lock():
            self._owner._assert_recovery_clear_locked()
            if not self._prepared:
                self._prepare_locked()
            directory = self.journal_path.parent
            journal = self._owner._read_journal(directory)
            prior_state = str(journal.get("state") or "")
            if prior_state != "prepared":
                raise WriteSetError(
                    "the write-set transaction cannot be committed in its "
                    f"current state ({prior_state})",
                    code="write_set_not_committable",
                    details={
                        "transaction_id": self.transaction_id,
                        "state": prior_state,
                    },
                )
            journal["state"] = "applying"
            if receipt is not None:
                journal["receipt"] = dict(receipt)
            self._owner._write_journal(directory, journal)
            try:
                for index, entry in enumerate(self._owner._entries(journal)):
                    target = self._owner._target(entry["target"])
                    before = entry["before"]
                    after = entry["after"]
                    current = self._owner._snapshot_description(target)
                    if _same_snapshot(current, after):
                        continue
                    if not _same_snapshot(current, before):
                        self._owner._mark_recovery_required(
                            directory, journal, target, before, after, current
                        )
                    if self._owner._publish_hook is not None:
                        self._owner._publish_hook(index, target)
                    self._owner._publish_description(directory, target, after)
                    published = self._owner._snapshot_description(target)
                    if not _same_snapshot(published, after):
                        self._owner._mark_recovery_required(
                            directory, journal, target, after, before, published
                        )
                journal["state"] = "committed"
                self._owner._write_journal(directory, journal)
                self._committed = True
                self._owner._discard_terminal_payloads_locked(directory)
            except Exception as exc:
                try:
                    self._owner._rollback_locked(directory, journal)
                except RecoveryRequiredError as recovery_error:
                    raise recovery_error from exc
                raise

    def _prepare_locked(self) -> None:
        if self._prepared:
            return
        directory = self.journal_path.parent
        if directory.exists():
            raise WriteSetError(
                "the transaction directory already exists",
                code="write_set_transaction_exists",
                details={"transaction_id": self.transaction_id},
            )
        directory.mkdir(parents=False, mode=0o700)
        (directory / "before").mkdir(mode=0o700)
        (directory / "after").mkdir(mode=0o700)
        entries: list[dict[str, Any]] = []
        created_directories: set[str] = set()
        try:
            for index, operation in enumerate(self._operations.values()):
                target = self._owner._target(operation.target)
                before = self._owner._snapshot_description(target)
                before_blob = None
                if before.get("exists"):
                    if before.get("kind") == "non_file":
                        raise UnsafeTargetError(
                            "a write-set target is not a regular file",
                            details={"target": operation.target},
                        )
                    before_blob = f"before/{index:06d}.bin"
                    _copy_new(target, directory / before_blob)
                    if _sha256_file(directory / before_blob) != before["sha256"]:
                        raise WriteSetError(
                            "a target changed while its preimage was captured",
                            code="write_set_prepare_conflict",
                            details={"target": operation.target},
                            retryable=True,
                        )
                before["blob"] = before_blob

                after_blob = None
                if operation.payload is not None:
                    after_blob = f"after/{index:06d}.bin"
                    _write_new(directory / after_blob, operation.payload)
                    after = {
                        "exists": True,
                        "sha256": _sha256_bytes(operation.payload),
                        "blob": after_blob,
                    }
                    if isinstance(before.get("mode"), int):
                        after["mode"] = before["mode"]
                else:
                    after = {"exists": False, "sha256": None, "blob": None}

                parent = target.parent
                while parent != self._owner.root and not parent.exists():
                    created_directories.add(
                        parent.relative_to(self._owner.root).as_posix()
                    )
                    parent = parent.parent
                entries.append(
                    {
                        "target": operation.target,
                        "before": before,
                        "after": after,
                    }
                )
            journal = {
                "version": _JOURNAL_VERSION,
                "transaction_id": self.transaction_id,
                "operation_id": self.operation_id,
                "scope": self.scope,
                "state": "prepared",
                "metadata": self.metadata,
                "created_directories": sorted(created_directories),
                "entries": entries,
            }
            self._owner._write_journal(directory, journal)
            _fsync_directory(directory)
            _fsync_directory(self._owner.transactions_dir)
            self._prepared = True
        except BaseException:
            # No live path is touched before the prepared journal is durable.
            # Removing an incomplete prepare is therefore always safe.
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def _ensure_open(self) -> None:
        if self._committed:
            raise WriteSetError(
                "the write-set transaction is already committed",
                code="write_set_already_committed",
                details={"transaction_id": self.transaction_id},
            )

    def _ensure_stageable(self) -> None:
        self._ensure_open()
        if self._prepared:
            raise WriteSetError(
                "a prepared write-set transaction cannot be changed",
                code="write_set_already_prepared",
                details={"transaction_id": self.transaction_id},
            )


def _is_redirecting_path(path: Path) -> bool:
    """Detect symlinks, Windows junctions, and other reparse redirects."""

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


def _same_snapshot(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    same_content = bool(left.get("exists")) == bool(right.get("exists")) and left.get(
        "sha256"
    ) == right.get("sha256")
    expected_mode = right.get("mode")
    if same_content and isinstance(expected_mode, int):
        return left.get("mode") == expected_mode
    return same_content


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        path.chmod(0o600)
        os.fsync(stream.fileno())


def _copy_new(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1 << 20)
        writer.flush()
        destination.chmod(0o600)
        os.fsync(writer.fileno())


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            temporary.chmod(0o600)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _atomic_copy(source: Path, destination: Path, *, mode: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            # Make the temporary private before any user data is copied.  The
            # intended final mode is applied before publication so chmod
            # failure cannot leave a committed file with surprising access.
            temporary.chmod(0o600)
            shutil.copyfileobj(reader, writer, length=1 << 20)
            writer.flush()
            if mode is not None:
                temporary.chmod(mode)
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    """Best-effort directory flush; Windows does not expose portable dir fsync."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _lock_stream(stream: BinaryIO) -> None:
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
        os.fsync(stream.fileno())
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = [
    "RecoverableWriteSet",
    "RecoverableWriteTransaction",
    "RecoveryRequiredError",
    "RecoveryResult",
    "UnsafeTargetError",
    "WriteSetError",
]
