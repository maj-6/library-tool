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

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Iterator, Literal

from ...engine.errors import RepositoryError
from ._file_lock import (
    UnsafeLockFileError,
    lock_stream,
    open_lock_file,
    unlock_stream,
)


JournalState = Literal[
    "prepared",
    "applying",
    "committed",
    "rolled_back",
    "recovery_required",
]
PublishHook = Callable[[int, Path], None]

_LEGACY_JOURNAL_VERSION = 1
_TREE_MOVE_JOURNAL_VERSION = 2
_SUPPORTED_JOURNAL_VERSIONS = frozenset(
    {_LEGACY_JOURNAL_VERSION, _TREE_MOVE_JOURNAL_VERSION}
)
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


@dataclass(frozen=True, slots=True)
class _StagedTreeMove:
    source: str
    destination: str


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
        self._owner_pid = os.getpid()
        self._thread_lock = _process_lock(self.lock_path)
        self._lock_state = threading.local()
        self._publish_hook = publish_hook

    @contextmanager
    def workspace_lease(self) -> Iterator[None]:
        """Hold the cross-process write lease across snapshot and commit.

        Repository adapters use this when merge decisions depend on a live
        destination snapshot. Calls to ``begin``, ``prepare``, and ``commit``
        on this same instance are reentrant inside the lease, so no competing
        process can change write-set-managed state between planning and
        publication.
        """

        with self._workspace_lock():
            self._assert_recovery_clear_locked()
            yield

    @contextmanager
    def recovery_lease(self) -> Iterator[None]:
        """Hold the workspace lock while coordinating startup recovery.

        Unlike :meth:`workspace_lease`, this deliberately does not reject an
        unfinished journal: inspecting and repairing that journal is the only
        valid purpose of this scope. Repository composition may acquire its
        legacy lock inside this lease and then call :meth:`recover_all`, whose
        process lock is reentrant.
        """

        with self._workspace_lock():
            yield

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
            # Version-2 publication applies every tree move first, followed by
            # file entries in their staged order.  Undo the exact reverse so a
            # catalogue or receipt can remain the final publication boundary.
            # Version-1 journals contain no tree moves, preserving their
            # original file-only rollback semantics.
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
            for move in reversed(self._tree_moves(journal)):
                self._publish_tree_move(directory, journal, move, forward=False)
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

    def _mark_tree_recovery_required(
        self,
        directory: Path,
        journal: dict[str, Any],
        move: Mapping[str, Any],
        source_current: Mapping[str, Any],
        destination_current: Mapping[str, Any],
    ) -> None:
        journal["state"] = "recovery_required"
        journal["recovery_conflict"] = {
            "source": move["source"],
            "destination": move["destination"],
            "expected_tree": dict(move["fingerprint"]),
            "source_current": dict(source_current),
            "destination_current": dict(destination_current),
        }
        self._write_journal(directory, journal)
        raise RecoveryRequiredError(
            "a moved tree is neither the transaction's before- nor after-state",
            details={
                "transaction_id": journal.get("transaction_id"),
                "source": str(self.root / str(move["source"])),
                "destination": str(self.root / str(move["destination"])),
                "source_sha256": source_current.get("sha256"),
                "destination_sha256": destination_current.get("sha256"),
            },
        )

    def _publish_tree_move(
        self,
        directory: Path,
        journal: dict[str, Any],
        move: Mapping[str, Any],
        *,
        forward: bool,
    ) -> None:
        source = self._target(str(move["source"]), allow_directory=True)
        destination = self._target(
            str(move["destination"]), allow_directory=True
        )
        expected = move["fingerprint"]
        source_current = self._snapshot_tree_description(source)
        destination_current = self._snapshot_tree_description(destination)
        missing = _missing_tree_description()

        is_before = _same_tree_snapshot(source_current, expected) and _same_tree_snapshot(
            destination_current, missing
        )
        is_after = _same_tree_snapshot(source_current, missing) and _same_tree_snapshot(
            destination_current, expected
        )
        desired_state = is_after if forward else is_before
        if desired_state:
            return
        current_state = is_before if forward else is_after
        if not current_state:
            self._mark_tree_recovery_required(
                directory,
                journal,
                move,
                source_current,
                destination_current,
            )

        move_source, move_destination = (
            (source, destination) if forward else (destination, source)
        )
        self._assert_same_tree_move_filesystem(move_source, move_destination)
        move_destination.parent.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink(move_destination)
        # The final existence check improves diagnostics.  Publication still
        # uses an atomic no-replace primitive below; a check followed by plain
        # POSIX ``rename`` could silently replace a concurrently created empty
        # destination directory.
        if os.path.lexists(move_destination):
            self._mark_tree_recovery_required(
                directory,
                journal,
                move,
                self._snapshot_tree_description(source),
                self._snapshot_tree_description(destination),
            )
        try:
            _rename_tree_no_replace(move_source, move_destination)
        except OSError as exc:
            # A platform call normally reports failure before changing either
            # endpoint.  Inspect both paths anyway: wrappers, filesystems, and
            # fault injection can report an error after the rename took
            # effect.  Treat an exact after-state as published, an exact
            # before-state as a retryable publication failure, and every other
            # state as an unresolved collision that must not be overwritten.
            failed_source = self._snapshot_tree_description(source)
            failed_destination = self._snapshot_tree_description(destination)
            failed_is_before = _same_tree_snapshot(
                failed_source, expected
            ) and _same_tree_snapshot(failed_destination, missing)
            failed_is_after = _same_tree_snapshot(
                failed_source, missing
            ) and _same_tree_snapshot(failed_destination, expected)
            failed_desired_state = failed_is_after if forward else failed_is_before
            if not failed_desired_state:
                failed_current_state = (
                    failed_is_before if forward else failed_is_after
                )
                if not failed_current_state:
                    self._mark_tree_recovery_required(
                        directory,
                        journal,
                        move,
                        failed_source,
                        failed_destination,
                    )
                if exc.errno == errno.EXDEV:
                    raise UnsafeTargetError(
                        "a recoverable tree move crossed a filesystem boundary",
                        code="cross_device_tree_move",
                        details={
                            "source": str(move_source),
                            "destination": str(move_destination),
                            "cause": str(exc),
                        },
                    ) from exc
                raise WriteSetError(
                    "the directory tree could not be moved atomically",
                    code="write_set_tree_move_failed",
                    details={
                        "source": str(move_source),
                        "destination": str(move_destination),
                        "cause": str(exc),
                    },
                    retryable=True,
                ) from exc
        _fsync_directory(move_source.parent)
        if move_destination.parent != move_source.parent:
            _fsync_directory(move_destination.parent)

        published_source = self._snapshot_tree_description(source)
        published_destination = self._snapshot_tree_description(destination)
        published = (
            _same_tree_snapshot(published_source, missing)
            and _same_tree_snapshot(published_destination, expected)
            if forward
            else _same_tree_snapshot(published_source, expected)
            and _same_tree_snapshot(published_destination, missing)
        )
        if not published:
            self._mark_tree_recovery_required(
                directory,
                journal,
                move,
                published_source,
                published_destination,
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

    def _tree_moves(self, journal: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return validated tree moves from a version-2 journal.

        Version-1 journals predate tree moves and omit ``tree_moves``.  Treating
        that omission as an empty list preserves their recovery behavior; the
        journal reader rejects a v1 document that contains the field.
        """

        raw = journal.get("tree_moves", [])
        if not isinstance(raw, list):
            raise RecoveryRequiredError(
                "the transaction journal has an invalid tree-moves list",
                details={"transaction_id": journal.get("transaction_id")},
            )
        moves: list[dict[str, Any]] = []
        endpoints: list[str] = []
        file_targets = [str(entry["target"]) for entry in self._entries(journal)]
        for value in raw:
            if not isinstance(value, dict):
                raise RecoveryRequiredError(
                    "the transaction journal contains an invalid tree move",
                    details={"transaction_id": journal.get("transaction_id")},
                )
            source = value.get("source")
            destination = value.get("destination")
            fingerprint = value.get("fingerprint")
            if (
                not isinstance(source, str)
                or not isinstance(destination, str)
                or not _valid_tree_fingerprint(fingerprint)
            ):
                raise RecoveryRequiredError(
                    "the transaction journal contains an invalid tree move",
                    details={"transaction_id": journal.get("transaction_id")},
                )
            self._target(source, allow_directory=True)
            self._target(destination, allow_directory=True)
            if _paths_overlap(source, destination):
                raise RecoveryRequiredError(
                    "the transaction journal contains overlapping tree endpoints",
                    details={"source": source, "destination": destination},
                )
            for endpoint in (source, destination):
                if any(_paths_overlap(endpoint, other) for other in endpoints):
                    raise RecoveryRequiredError(
                        "the transaction journal contains overlapping tree moves",
                        details={"target": endpoint},
                    )
                if any(_paths_overlap(endpoint, target) for target in file_targets):
                    raise RecoveryRequiredError(
                        "the transaction journal overlaps file and tree operations",
                        details={"target": endpoint},
                    )
            endpoints.extend((source, destination))
            moves.append(value)
        return moves

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

    def _snapshot_tree_description(self, target: Path) -> dict[str, Any]:
        """Describe a directory tree without retaining a content copy."""

        self._assert_no_symlink(target)
        if not os.path.lexists(target):
            return _missing_tree_description()
        if _is_redirecting_path(target):
            return {
                "exists": True,
                "kind": "redirect",
                "sha256": None,
            }
        try:
            info = os.stat(target, follow_symlinks=False)
        except OSError as exc:
            raise WriteSetError(
                "a tree target could not be inspected",
                code="write_set_tree_inspection_failed",
                details={"target": str(target), "cause": str(exc)},
                retryable=True,
            ) from exc
        if not stat.S_ISDIR(info.st_mode):
            return {
                "exists": True,
                "kind": _filesystem_kind(info.st_mode),
                "sha256": None,
            }
        return _fingerprint_tree(target)

    def _assert_same_tree_move_filesystem(
        self, source: Path, destination: Path
    ) -> None:
        ancestor = destination.parent
        while not os.path.lexists(ancestor):
            if ancestor == self.root:
                break
            ancestor = ancestor.parent
        if _is_redirecting_path(ancestor):
            raise UnsafeTargetError(
                "a tree move destination crosses a redirecting link",
                details={"destination": str(destination), "link": str(ancestor)},
            )
        try:
            ancestor_info = os.stat(ancestor, follow_symlinks=False)
            source_info = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            raise UnsafeTargetError(
                "a tree move endpoint could not be inspected",
                code="unsafe_tree_move_endpoint",
                details={
                    "source": str(source),
                    "destination": str(destination),
                    "cause": str(exc),
                },
            ) from exc
        if not stat.S_ISDIR(ancestor_info.st_mode):
            raise UnsafeTargetError(
                "a tree move destination parent is not a directory",
                code="unsafe_tree_move_endpoint",
                details={"destination": str(destination), "parent": str(ancestor)},
            )
        if source_info.st_dev != ancestor_info.st_dev:
            raise UnsafeTargetError(
                "a recoverable tree move must remain on one filesystem",
                code="cross_device_tree_move",
                details={
                    "source": str(source),
                    "destination": str(destination),
                },
            )

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
        version = value.get("version") if isinstance(value, dict) else None
        if type(version) is not int or version not in _SUPPORTED_JOURNAL_VERSIONS:
            raise RecoveryRequiredError(
                "the transaction journal version is unsupported",
                details={"path": str(directory / _JOURNAL_NAME)},
            )
        has_tree_moves = "tree_moves" in value
        if version == _LEGACY_JOURNAL_VERSION and has_tree_moves:
            raise RecoveryRequiredError(
                "a version-1 transaction journal may not contain tree moves",
                details={"path": str(directory / _JOURNAL_NAME)},
            )
        if version == _TREE_MOVE_JOURNAL_VERSION and (
            not isinstance(value.get("tree_moves"), list)
            or not value["tree_moves"]
        ):
            raise RecoveryRequiredError(
                "a version-2 transaction journal must contain tree moves",
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
        if os.getpid() != self._owner_pid:
            raise WriteSetError(
                "a recoverable write set cannot be reused after process fork",
                code="write_set_process_changed",
                details={"root": str(self.root)},
            )
        with self._thread_lock:
            depth = int(getattr(self._lock_state, "depth", 0))
            if depth:
                self._lock_state.depth = depth + 1
                try:
                    yield
                finally:
                    self._lock_state.depth = depth
                return
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                stream = open_lock_file(self.lock_path)
            except UnsafeLockFileError as exc:
                raise UnsafeTargetError(
                    "the workspace lock is not a private regular file",
                    details={"path": str(self.lock_path)},
                ) from exc
            except OSError as exc:
                raise WriteSetError(
                    "the workspace lock could not be opened",
                    details={"path": str(self.lock_path)},
                    retryable=True,
                ) from exc
            operation_failed = False
            lock_acquired = False
            try:
                try:
                    lock_stream(
                        stream,
                        blocking=True,
                        path=self.lock_path,
                    )
                    lock_acquired = True
                except UnsafeLockFileError as exc:
                    operation_failed = True
                    raise UnsafeTargetError(
                        "the workspace lock identity changed during acquisition",
                        details={"path": str(self.lock_path)},
                    ) from exc
                except OSError as exc:
                    operation_failed = True
                    raise WriteSetError(
                        "the workspace lock could not be acquired",
                        details={"path": str(self.lock_path)},
                        retryable=True,
                    ) from exc
                self._lock_state.depth = 1
                body_failed = False
                try:
                    yield
                except BaseException:
                    operation_failed = body_failed = True
                    raise
                finally:
                    self._lock_state.depth = 0
                    if lock_acquired:
                        try:
                            unlock_stream(stream)
                        except OSError as exc:
                            if not body_failed:
                                operation_failed = True
                                raise WriteSetError(
                                    "the workspace lock could not be released",
                                    details={"path": str(self.lock_path)},
                                ) from exc
            finally:
                try:
                    stream.close()
                except OSError as exc:
                    if not operation_failed:
                        operation_failed = True
                        raise WriteSetError(
                            "the workspace lock file could not be closed",
                            details={"path": str(self.lock_path)},
                        ) from exc


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
        self._tree_move_operations: list[_StagedTreeMove] = []
        self._prepared = False
        self._committed = False

    @property
    def journal_path(self) -> Path:
        return self._owner.transactions_dir / self.transaction_id / _JOURNAL_NAME

    def stage_write(self, target: str | Path, payload: bytes) -> None:
        """Stage the final bytes for one relative file target."""
        self._ensure_stageable()
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("write-set payload must be bytes-like")
        path = self._owner._target(target)
        relative = path.relative_to(self._owner.root).as_posix()
        self._assert_file_target_does_not_overlap_tree_move(relative)
        self._operations[relative] = _StagedOperation(relative, bytes(payload))

    def stage_delete(self, target: str | Path) -> None:
        """Stage deletion of one relative file target."""
        self._ensure_stageable()
        path = self._owner._target(target)
        relative = path.relative_to(self._owner.root).as_posix()
        self._assert_file_target_does_not_overlap_tree_move(relative)
        self._operations[relative] = _StagedOperation(relative, None)

    def stage_tree_move(
        self, source: str | Path, destination: str | Path
    ) -> None:
        """Stage an atomic, same-filesystem rename of one directory tree.

        Tree bytes are never copied into the journal.  Preparation records a
        deterministic fingerprint and commit publishes the tree with one
        directory rename.  This makes the primitive suitable for large item
        tombstones while retaining deterministic restart recovery.
        """

        self._ensure_stageable()
        source_path = self._owner._target(source, allow_directory=True)
        destination_path = self._owner._target(destination, allow_directory=True)
        source_relative = source_path.relative_to(self._owner.root).as_posix()
        destination_relative = destination_path.relative_to(
            self._owner.root
        ).as_posix()
        if _paths_overlap(source_relative, destination_relative):
            raise WriteSetError(
                "a tree move source and destination may not overlap",
                code="overlapping_write_set_operations",
                details={
                    "source": source_relative,
                    "destination": destination_relative,
                },
            )
        for target in self._operations:
            if _paths_overlap(source_relative, target) or _paths_overlap(
                destination_relative, target
            ):
                raise WriteSetError(
                    "file and tree operations may not overlap",
                    code="overlapping_write_set_operations",
                    details={"target": target},
                )
        for move in self._tree_move_operations:
            for endpoint in (source_relative, destination_relative):
                if _paths_overlap(endpoint, move.source) or _paths_overlap(
                    endpoint, move.destination
                ):
                    raise WriteSetError(
                        "staged tree moves may not overlap",
                        code="overlapping_write_set_operations",
                        details={"target": endpoint},
                    )

        source_snapshot = self._owner._snapshot_tree_description(source_path)
        if not _is_tree_snapshot(source_snapshot):
            raise UnsafeTargetError(
                "a tree move source must be an existing regular directory tree",
                code="unsafe_tree_move_source",
                details={"source": source_relative},
            )
        destination_snapshot = self._owner._snapshot_tree_description(
            destination_path
        )
        if bool(destination_snapshot.get("exists")):
            raise WriteSetError(
                "a tree move destination already exists",
                code="write_set_tree_destination_exists",
                details={"destination": destination_relative},
            )
        self._owner._assert_same_tree_move_filesystem(
            source_path, destination_path
        )
        self._tree_move_operations.append(
            _StagedTreeMove(source_relative, destination_relative)
        )

    def prepare(self) -> None:
        """Persist preimages, postimages, hashes, and a ``prepared`` journal."""
        self._ensure_open()
        if self._prepared:
            return
        if not self._operations and not self._tree_move_operations:
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
                # Version 2 reserves the leading publication slots for tree
                # moves.  File entries retain their staging order afterward,
                # allowing a repository to stage its catalogue entry last.
                tree_moves = self._owner._tree_moves(journal)
                for index, move in enumerate(tree_moves):
                    destination = self._owner._target(
                        move["destination"], allow_directory=True
                    )
                    if self._owner._publish_hook is not None:
                        self._owner._publish_hook(index, destination)
                    self._owner._publish_tree_move(
                        directory, journal, move, forward=True
                    )
                tree_move_count = len(tree_moves)
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
                        self._owner._publish_hook(tree_move_count + index, target)
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
        tree_moves: list[dict[str, Any]] = []
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
            for operation in self._tree_move_operations:
                source = self._owner._target(
                    operation.source, allow_directory=True
                )
                destination = self._owner._target(
                    operation.destination, allow_directory=True
                )
                fingerprint = self._owner._snapshot_tree_description(source)
                if not _is_tree_snapshot(fingerprint):
                    raise UnsafeTargetError(
                        "a tree move source must remain an existing regular tree",
                        code="unsafe_tree_move_source",
                        details={"source": operation.source},
                    )
                # A second full pass makes a concurrently changing tree fail
                # preparation instead of producing a hybrid fingerprint.
                confirmation = self._owner._snapshot_tree_description(source)
                if not _same_tree_snapshot(fingerprint, confirmation):
                    raise WriteSetError(
                        "a tree changed while its fingerprint was captured",
                        code="write_set_prepare_conflict",
                        details={"source": operation.source},
                        retryable=True,
                    )
                destination_snapshot = self._owner._snapshot_tree_description(
                    destination
                )
                if bool(destination_snapshot.get("exists")):
                    raise WriteSetError(
                        "a tree move destination already exists",
                        code="write_set_tree_destination_exists",
                        details={"destination": operation.destination},
                    )
                self._owner._assert_same_tree_move_filesystem(source, destination)
                parent = destination.parent
                while parent != self._owner.root and not parent.exists():
                    created_directories.add(
                        parent.relative_to(self._owner.root).as_posix()
                    )
                    parent = parent.parent
                tree_moves.append(
                    {
                        "source": operation.source,
                        "destination": operation.destination,
                        "fingerprint": fingerprint,
                    }
                )
            journal = {
                "version": (
                    _TREE_MOVE_JOURNAL_VERSION
                    if tree_moves
                    else _LEGACY_JOURNAL_VERSION
                ),
                "transaction_id": self.transaction_id,
                "operation_id": self.operation_id,
                "scope": self.scope,
                "state": "prepared",
                "metadata": self.metadata,
                "created_directories": sorted(created_directories),
                "entries": entries,
            }
            if tree_moves:
                # Version 2 is a feature boundary, not merely an additive
                # field.  A pre-tree-move binary must reject this journal
                # instead of silently ignoring a live directory rename.
                journal["tree_moves"] = tree_moves
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

    def _assert_file_target_does_not_overlap_tree_move(self, target: str) -> None:
        for move in self._tree_move_operations:
            if _paths_overlap(target, move.source) or _paths_overlap(
                target, move.destination
            ):
                raise WriteSetError(
                    "file and tree operations may not overlap",
                    code="overlapping_write_set_operations",
                    details={"target": target},
                )


def _paths_overlap(left: str, right: str) -> bool:
    """Compare journal paths conservatively across case-sensitive platforms."""

    left_parts = tuple(part.casefold() for part in PurePath(left).parts)
    right_parts = tuple(part.casefold() for part in PurePath(right).parts)
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def _missing_tree_description() -> dict[str, Any]:
    return {
        "exists": False,
        "kind": "directory_tree",
        "sha256": None,
        "file_count": 0,
        "directory_count": 0,
    }


def _is_tree_snapshot(value: Mapping[str, Any]) -> bool:
    return (
        bool(value.get("exists"))
        and value.get("kind") == "directory_tree"
        and isinstance(value.get("sha256"), str)
    )


def _valid_tree_fingerprint(value: object) -> bool:
    if not isinstance(value, dict) or not _is_tree_snapshot(value):
        return False
    digest = value.get("sha256")
    file_count = value.get("file_count")
    directory_count = value.get("directory_count")
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
        and isinstance(file_count, int)
        and not isinstance(file_count, bool)
        and file_count >= 0
        and isinstance(directory_count, int)
        and not isinstance(directory_count, bool)
        and directory_count >= 1
    )


def _same_tree_snapshot(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    if bool(left.get("exists")) != bool(right.get("exists")):
        return False
    if not bool(right.get("exists")):
        return True
    return (
        left.get("kind") == right.get("kind")
        and left.get("sha256") == right.get("sha256")
        and left.get("file_count") == right.get("file_count")
        and left.get("directory_count") == right.get("directory_count")
    )


def _filesystem_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory_tree"
    if stat.S_ISLNK(mode):
        return "redirect"
    return "special"


def _stable_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
    )


def _fingerprint_tree(root: Path) -> dict[str, Any]:
    """Hash paths, empty directories, bytes, and modes in deterministic order."""

    records: list[dict[str, Any]] = []
    file_count = 0
    directory_count = 0
    tree_device: int | None = None

    def inspect_file(path: Path, relative: str) -> None:
        nonlocal file_count
        if _is_redirecting_path(path):
            raise UnsafeTargetError(
                "recoverable tree moves may not contain redirecting links",
                details={"path": str(path)},
            )
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        # If an attacker swaps a previously inspected regular file for a FIFO
        # or device, opening it must fail validation rather than block recovery.
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise WriteSetError(
                "a tree file could not be opened while fingerprinting",
                code="write_set_tree_inspection_failed",
                details={"path": str(path), "cause": str(exc)},
                retryable=True,
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise UnsafeTargetError(
                    "recoverable tree moves may contain only regular files",
                    details={"path": str(path), "kind": _filesystem_kind(before.st_mode)},
                )
            if tree_device is None or before.st_dev != tree_device:
                raise UnsafeTargetError(
                    "recoverable tree moves may not cross filesystem boundaries",
                    code="cross_device_tree_move",
                    details={"path": str(path)},
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise WriteSetError(
                "a tree file changed while it was fingerprinted",
                code="write_set_prepare_conflict",
                details={"path": str(path)},
                retryable=True,
            ) from exc
        if (
            _is_redirecting_path(path)
            or _stable_stat_identity(before) != _stable_stat_identity(after)
            or _stable_stat_identity(before) != _stable_stat_identity(path_after)
        ):
            raise WriteSetError(
                "a tree file changed while it was fingerprinted",
                code="write_set_prepare_conflict",
                details={"path": str(path)},
                retryable=True,
            )
        records.append(
            {
                "kind": "file",
                "path": relative,
                "mode": stat.S_IMODE(before.st_mode),
                "size": int(before.st_size),
                "sha256": digest.hexdigest(),
            }
        )
        file_count += 1

    def inspect_directory(path: Path, relative: str) -> None:
        nonlocal directory_count, tree_device
        if _is_redirecting_path(path):
            raise UnsafeTargetError(
                "recoverable tree moves may not contain redirecting links",
                details={"path": str(path)},
            )
        try:
            before = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise WriteSetError(
                "a tree directory could not be inspected",
                code="write_set_tree_inspection_failed",
                details={"path": str(path), "cause": str(exc)},
                retryable=True,
            ) from exc
        if not stat.S_ISDIR(before.st_mode):
            raise UnsafeTargetError(
                "recoverable tree moves may contain only regular directories",
                details={"path": str(path), "kind": _filesystem_kind(before.st_mode)},
            )
        if tree_device is None:
            tree_device = int(before.st_dev)
        elif before.st_dev != tree_device:
            raise UnsafeTargetError(
                "recoverable tree moves may not cross filesystem boundaries",
                code="cross_device_tree_move",
                details={"path": str(path)},
            )
        records.append(
            {
                "kind": "directory",
                "path": relative,
                "mode": stat.S_IMODE(before.st_mode),
            }
        )
        directory_count += 1
        try:
            with os.scandir(path) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError as exc:
            raise WriteSetError(
                "a tree directory could not be enumerated",
                code="write_set_tree_inspection_failed",
                details={"path": str(path), "cause": str(exc)},
                retryable=True,
            ) from exc
        for child in children:
            child_path = Path(child.path)
            child_relative = (
                child.name if not relative else f"{relative}/{child.name}"
            )
            if _is_redirecting_path(child_path):
                raise UnsafeTargetError(
                    "recoverable tree moves may not contain redirecting links",
                    details={"path": str(child_path)},
                )
            try:
                child_info = os.stat(child_path, follow_symlinks=False)
            except OSError as exc:
                raise WriteSetError(
                    "a tree entry changed while it was fingerprinted",
                    code="write_set_prepare_conflict",
                    details={"path": str(child_path)},
                    retryable=True,
                ) from exc
            if stat.S_ISDIR(child_info.st_mode):
                inspect_directory(child_path, child_relative)
            elif stat.S_ISREG(child_info.st_mode):
                inspect_file(child_path, child_relative)
            else:
                raise UnsafeTargetError(
                    "recoverable tree moves may not contain special files",
                    details={
                        "path": str(child_path),
                        "kind": _filesystem_kind(child_info.st_mode),
                    },
                )
        try:
            after = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise WriteSetError(
                "a tree directory changed while it was fingerprinted",
                code="write_set_prepare_conflict",
                details={"path": str(path)},
                retryable=True,
            ) from exc
        if (
            _is_redirecting_path(path)
            or _stable_stat_identity(before) != _stable_stat_identity(after)
        ):
            raise WriteSetError(
                "a tree directory changed while it was fingerprinted",
                code="write_set_prepare_conflict",
                details={"path": str(path)},
                retryable=True,
            )

    inspect_directory(root, "")
    digest = hashlib.sha256()
    digest.update(b"librarytool-directory-tree-v1\0")
    for record in sorted(records, key=lambda value: (str(value["path"]), str(value["kind"]))):
        encoded = json.dumps(
            record,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8", errors="surrogatepass")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {
        "exists": True,
        "kind": "directory_tree",
        "sha256": digest.hexdigest(),
        "file_count": file_count,
        "directory_count": directory_count,
    }


def _rename_tree_no_replace(source: Path, destination: Path) -> None:
    """Rename one directory without ever replacing a destination entry.

    Windows ``os.rename`` already has no-replace behavior.  Linux and macOS
    expose the equivalent only through platform APIs that Python does not wrap.
    Other POSIX platforms are refused explicitly: falling back to
    ``os.rename`` would allow a concurrently created empty destination
    directory to be silently removed.
    """

    if os.name == "nt":
        os.rename(source, destination)
        return

    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    library = ctypes.CDLL(None, use_errno=True)
    function: Any
    arguments: tuple[Any, ...]
    if sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        if function is None:
            raise WriteSetError(
                "atomic no-replace directory moves are unavailable",
                code="atomic_tree_move_unavailable",
                details={"platform": sys.platform},
            )
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        # Linux AT_FDCWD and RENAME_NOREPLACE.
        arguments = (-100, encoded_source, -100, encoded_destination, 1)
    elif sys.platform == "darwin":
        function = getattr(library, "renamex_np", None)
        if function is None:
            raise WriteSetError(
                "atomic no-replace directory moves are unavailable",
                code="atomic_tree_move_unavailable",
                details={"platform": sys.platform},
            )
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        # macOS RENAME_EXCL.
        arguments = (encoded_source, encoded_destination, 0x00000004)
    else:
        raise WriteSetError(
            "atomic no-replace directory moves are unavailable",
            code="atomic_tree_move_unavailable",
            details={"platform": sys.platform},
        )

    ctypes.set_errno(0)
    if function(*arguments) == 0:
        return
    error_number = ctypes.get_errno()
    unsupported_errors = {
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    }
    if error_number in unsupported_errors:
        raise WriteSetError(
            "atomic no-replace directory moves are unavailable",
            code="atomic_tree_move_unavailable",
            details={
                "platform": sys.platform,
                "source": str(source),
                "destination": str(destination),
                "cause": os.strerror(error_number),
            },
        )
    raise OSError(error_number, os.strerror(error_number), str(destination))


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


__all__ = [
    "RecoverableWriteSet",
    "RecoverableWriteTransaction",
    "RecoveryRequiredError",
    "RecoveryResult",
    "UnsafeTargetError",
    "WriteSetError",
]
