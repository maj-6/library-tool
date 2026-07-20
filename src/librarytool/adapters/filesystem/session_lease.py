"""Exclusive process-lifetime ownership for one engine workspace.

Recoverable write-set leases protect individual transactions.  They cannot be
held for an application's lifetime because worker threads must acquire them for
each command.  This separate non-blocking lease prevents two conforming engine
hosts from opening the same job history and mutable workspace concurrently.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO

from ...engine.errors import RepositoryError
from ._file_lock import (
    UnsafeLockFileError,
    is_lock_contention,
    lock_stream,
    open_lock_file,
    unlock_stream,
)
from .recoverable_write_set import RecoverableWriteSet


class WorkspaceSessionError(RepositoryError):
    """A process-lifetime workspace session could not be managed safely."""

    default_code = "workspace_session_failed"


class WorkspaceAlreadyOpenError(WorkspaceSessionError):
    """Another conforming engine host already owns this workspace."""

    default_code = "workspace_already_open"


_active_guard = threading.Lock()
_active_roots: set[str] = set()
_active_leases: set[WorkspaceSessionLease] = set()


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


class WorkspaceSessionLease:
    """One explicit, non-blocking lifetime lease for a workspace root."""

    __slots__ = (
        "_closed",
        "_guard",
        "_key",
        "_path",
        "_root",
        "_stream",
    )

    def __init__(
        self,
        *,
        root: Path,
        path: Path,
        key: str,
        stream: BinaryIO,
    ) -> None:
        self._root = root
        self._path = path
        self._key = key
        self._stream = stream
        self._closed = False
        self._guard = threading.Lock()

    @classmethod
    def acquire(cls, write_set: RecoverableWriteSet) -> WorkspaceSessionLease:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        root = write_set.root
        path = write_set.transactions_dir / "engine-session.lock"
        key = _root_key(root)
        with _active_guard:
            if key in _active_roots:
                raise WorkspaceAlreadyOpenError(
                    "the engine workspace is already open",
                    details={"workspace": str(root)},
                    retryable=True,
                )
            _active_roots.add(key)

        stream: BinaryIO | None = None
        try:
            try:
                stream = open_lock_file(path)
            except UnsafeLockFileError as exc:
                raise WorkspaceSessionError(
                    "the workspace session lock is not a private regular file",
                    code="unsafe_workspace_session_path",
                    details={"workspace": str(root)},
                ) from exc
            except OSError as exc:
                raise WorkspaceSessionError(
                    "the workspace session lock could not be opened",
                    details={"workspace": str(root)},
                    retryable=True,
                ) from exc
            try:
                lock_stream(stream, blocking=False, path=path)
            except OSError as exc:
                if is_lock_contention(exc):
                    raise WorkspaceAlreadyOpenError(
                        "the engine workspace is already open",
                        details={"workspace": str(root)},
                        retryable=True,
                    ) from exc
                raise WorkspaceSessionError(
                    "the workspace session lock could not be acquired",
                    details={"workspace": str(root)},
                    retryable=True,
                ) from exc
            lease = cls(
                root=root,
                path=path,
                key=key,
                stream=stream,
            )
            with _active_guard:
                _active_leases.add(lease)
            return lease
        except BaseException:
            try:
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            finally:
                with _active_guard:
                    _active_roots.discard(key)
            raise

    @property
    def root(self) -> Path:
        return self._root

    @property
    def path(self) -> Path:
        return self._path

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._closed

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            error: OSError | None = None
            try:
                unlock_stream(self._stream)
            except OSError as exc:
                error = exc
            finally:
                try:
                    self._stream.close()
                except OSError as exc:
                    if error is None:
                        error = exc
                finally:
                    self._closed = True
                    with _active_guard:
                        _active_roots.discard(self._key)
                        _active_leases.discard(self)
            if error is not None:
                raise WorkspaceSessionError(
                    "the workspace session lock could not be released",
                    details={"workspace": str(self._root)},
                ) from error

    def __enter__(self) -> WorkspaceSessionLease:
        if self.closed:
            raise WorkspaceSessionError(
                "the workspace session lease is closed",
                code="workspace_session_closed",
            )
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise


def _after_fork_in_child() -> None:
    """Drop inherited descriptors without unlocking the parent's lease."""

    global _active_guard
    global _active_leases
    global _active_roots

    inherited = tuple(_active_leases)
    _active_guard = threading.Lock()
    _active_leases = set()
    _active_roots = set()
    for lease in inherited:
        try:
            lease._stream.close()
        except OSError:
            pass
        lease._closed = True
        lease._guard = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


__all__ = [
    "WorkspaceAlreadyOpenError",
    "WorkspaceSessionError",
    "WorkspaceSessionLease",
]
