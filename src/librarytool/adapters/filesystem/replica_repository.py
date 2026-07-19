"""Callback-configured filesystem Replica workspace repository."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, MutableMapping
from pathlib import Path
from typing import Any, ContextManager

from ...engine.errors import RepositoryError


LayoutPathCallback = Callable[[str], Path]
ReadJsonCallback = Callable[[Path], Any]
WriteJsonCallback = Callable[[Path, Mapping[str, Any]], None]
LockCallback = Callable[[str], ContextManager[Any]]


class FilesystemReplicaRepository:
    """Store the Replica aggregate in a JSON file selected by a callback.

    The path, JSON I/O, and lock context are all injectable.  The transitional
    Flask adapter can therefore reuse its current entry-folder calculation,
    strict atomic writer, and process lock without this package importing it.
    Defaults provide a standalone, per-item locked, atomic JSON repository for
    headless clients and tests.
    """

    def __init__(
        self,
        layout_path_for: LayoutPathCallback,
        *,
        read_json: ReadJsonCallback | None = None,
        write_json: WriteJsonCallback | None = None,
        lock_context_for: LockCallback | None = None,
    ) -> None:
        self._layout_path_for = layout_path_for
        self._read_json = read_json or self._default_read_json
        self._write_json = write_json or self._default_write_json
        self._external_lock_context_for = lock_context_for
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def snapshot(self, item_id: str) -> Mapping[str, Any]:
        with self._lock_context(item_id):
            return copy.deepcopy(self._load(item_id))

    def unit_of_work(self, item_id: str) -> "FilesystemReplicaUnitOfWork":
        return FilesystemReplicaUnitOfWork(self, item_id)

    def _path(self, item_id: str) -> Path:
        try:
            return Path(self._layout_path_for(item_id))
        except Exception as exc:
            raise RepositoryError(
                "could not resolve the Replica workspace path",
                code="replica_path_failed",
                details={"item_id": item_id, "cause": str(exc)},
            ) from exc

    def _lock_context(self, item_id: str) -> ContextManager[Any]:
        if self._external_lock_context_for is not None:
            try:
                return self._external_lock_context_for(item_id)
            except Exception as exc:
                raise RepositoryError(
                    "could not acquire a Replica workspace lock context",
                    code="replica_lock_failed",
                    details={"item_id": item_id, "cause": str(exc)},
                    retryable=True,
                ) from exc
        with self._locks_guard:
            return self._locks.setdefault(item_id, threading.RLock())

    def _load(self, item_id: str) -> dict[str, Any]:
        path = self._path(item_id)
        try:
            value = self._read_json(path)
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "could not read the Replica workspace",
                code="replica_read_failed",
                details={
                    "item_id": item_id,
                    "path": str(path),
                    "cause": str(exc),
                },
                retryable=True,
            ) from exc
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise RepositoryError(
                "the Replica workspace is not a JSON object",
                code="invalid_replica_workspace",
                details={"item_id": item_id, "path": str(path)},
            )
        return copy.deepcopy(dict(value))

    def _save(self, item_id: str, workspace: Mapping[str, Any]) -> None:
        path = self._path(item_id)
        try:
            self._write_json(path, copy.deepcopy(dict(workspace)))
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "could not save the Replica workspace",
                code="replica_write_failed",
                details={
                    "item_id": item_id,
                    "path": str(path),
                    "cause": str(exc),
                },
                retryable=True,
            ) from exc

    @staticmethod
    def _default_read_json(path: Path) -> Any:
        if not path.is_file():
            return {}
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)

    @staticmethod
    def _default_write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(
                    value,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class FilesystemReplicaUnitOfWork:
    """Explicit-commit, multi-commit transaction over one workspace file."""

    def __init__(
        self, repository: FilesystemReplicaRepository, item_id: str
    ) -> None:
        self._repository = repository
        self._item_id = item_id
        self._lock_context: ContextManager[Any] | None = None
        self._workspace: MutableMapping[str, Any] | None = None
        self._active = False
        self.commit_count = 0

    @property
    def workspace(self) -> MutableMapping[str, Any]:
        if not self._active or self._workspace is None:
            raise RepositoryError(
                "the Replica unit of work is not active",
                code="inactive_replica_unit_of_work",
                details={"item_id": self._item_id},
            )
        return self._workspace

    def __enter__(self) -> "FilesystemReplicaUnitOfWork":
        if self._active:
            raise RepositoryError(
                "the Replica unit of work is already active",
                code="replica_unit_of_work_reentered",
                details={"item_id": self._item_id},
            )
        self._lock_context = self._repository._lock_context(self._item_id)
        self._lock_context.__enter__()
        try:
            self._workspace = self._repository._load(self._item_id)
        except BaseException:
            self._lock_context.__exit__(*sys.exc_info())
            self._lock_context = None
            raise
        self._active = True
        return self

    def commit(self) -> None:
        if not self._active or self._workspace is None:
            raise RepositoryError(
                "the Replica unit of work is not active",
                code="inactive_replica_unit_of_work",
                details={"item_id": self._item_id},
            )
        self._repository._save(self._item_id, self._workspace)
        self.commit_count += 1

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if self._lock_context is not None:
                self._lock_context.__exit__(exc_type, exc, traceback)
        finally:
            self._active = False
            self._workspace = None
            self._lock_context = None
        return False
