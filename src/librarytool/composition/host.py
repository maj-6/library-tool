"""Process-lifetime bootstrap for a filesystem-backed Library Engine.

The lower-level filesystem composer accepts already-created resources.  This
module owns their startup order: acquire exclusive workspace ownership,
recover interrupted publication, compose one immutable service graph, then
rehydrate observable job history before exposing the session to a transport.

Worker threads and provider executors are deliberately borrowed concerns.  A
session close releases only bootstrap-owned resources; it does not claim to
cancel or join work scheduled by Flask, a CLI, Qt, Godot, or another host.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field
from math import isfinite
from numbers import Real
from pathlib import Path
from typing import Any, ContextManager

from ..adapters.filesystem import (
    FilesystemJobHistoryRepository,
    RecoverableWriteSet,
    RecoveryResult,
    WorkspaceSessionLease,
)
from ..engine.errors import RepositoryError
from ..engine.jobs import JobManager
from ..engine.runtime import LibraryEngine
from ..engine.translations import TranslationProvenanceService
from ._filesystem_paths import (
    resolve_workspace_path,
    workspace_paths_overlap,
)
from .filesystem import (
    CanvasBindings,
    CatalogueBindings,
    ContributionFactory,
    FilesystemEnginePaths,
    FilesystemEngineResources,
    InterchangeBindings,
    ItemLockFactory,
    ReplicaBindings,
    TranslationBindings,
    compose_filesystem_engine,
)


JobIdFactory = Callable[[set[str]], str]
ReadJson = Callable[[Path, Any], Any]
WriteJson = Callable[[Path, Any], None]
RecoveryLockFactory = Callable[[], ContextManager[Any]]


class EngineSessionError(RuntimeError):
    """A filesystem engine session has invalid lifecycle state."""


class EngineSessionClosedError(EngineSessionError):
    """A caller tried to obtain a resource from a closed session."""


class EngineSessionForkedError(EngineSessionError):
    """A caller tried to reuse a session inherited across ``fork()``."""


def _null_recovery_lock() -> ContextManager[None]:
    return nullcontext()


@dataclass(frozen=True, slots=True)
class FilesystemEngineConfig:
    """Immutable, side-effect-free process configuration for one workspace."""

    workspace_root: Path
    paths: FilesystemEnginePaths
    job_history: Path = Path("jobs.json")
    job_keep: int = 50
    job_event_keep: int = 500
    job_checkpoint_interval: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_root", Path(self.workspace_root))
        object.__setattr__(self, "job_history", Path(self.job_history))
        if not isinstance(self.paths, FilesystemEnginePaths):
            raise TypeError("paths must be FilesystemEnginePaths")
        if (
            not isinstance(self.job_keep, int)
            or isinstance(self.job_keep, bool)
            or self.job_keep < 0
        ):
            raise ValueError("job_keep must be a non-negative integer")
        if (
            not isinstance(self.job_event_keep, int)
            or isinstance(self.job_event_keep, bool)
            or self.job_event_keep < 0
        ):
            raise ValueError("job_event_keep must be a non-negative integer")
        if (
            isinstance(self.job_checkpoint_interval, bool)
            or not isinstance(self.job_checkpoint_interval, Real)
        ):
            raise ValueError("job_checkpoint_interval must be a finite number")
        checkpoint_interval = float(self.job_checkpoint_interval)
        if not isfinite(checkpoint_interval) or checkpoint_interval < 0:
            raise ValueError(
                "job_checkpoint_interval must be a finite non-negative number"
            )
        object.__setattr__(
            self,
            "job_checkpoint_interval",
            checkpoint_interval,
        )


@dataclass(frozen=True, slots=True)
class JobHistoryBindings:
    """Optional compatibility I/O and identity policy for persisted jobs."""

    read_json: ReadJson | None = None
    write_json: WriteJson | None = None
    id_factory: JobIdFactory | None = None

    def __post_init__(self) -> None:
        for callback, name in (
            (self.read_json, "read_json"),
            (self.write_json, "write_json"),
            (self.id_factory, "id_factory"),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable")


@dataclass(frozen=True, slots=True)
class FilesystemHostBindings:
    """Borrowed storage, policy, codec, and transitional locking seams."""

    catalogue: CatalogueBindings
    replica: ReplicaBindings
    interchange: InterchangeBindings
    translation: TranslationBindings
    workspace_lock_context_for: ItemLockFactory
    jobs: JobHistoryBindings = field(default_factory=JobHistoryBindings)
    recovery_lock_context: RecoveryLockFactory = _null_recovery_lock
    canvases: CanvasBindings | None = None

    def __post_init__(self) -> None:
        for value, expected, name in (
            (self.catalogue, CatalogueBindings, "catalogue"),
            (self.replica, ReplicaBindings, "replica"),
            (self.interchange, InterchangeBindings, "interchange"),
            (self.translation, TranslationBindings, "translation"),
            (self.jobs, JobHistoryBindings, "jobs"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{name} has an invalid binding bundle")
        if self.canvases is not None and not isinstance(
            self.canvases,
            CanvasBindings,
        ):
            raise TypeError("canvases has an invalid binding bundle")
        for callback, name in (
            (
                self.workspace_lock_context_for,
                "workspace_lock_context_for",
            ),
            (self.recovery_lock_context, "recovery_lock_context"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")


class FilesystemEngineSession:
    """One open engine graph and its process-lifetime workspace ownership.

    Resources returned by this object are borrowed and valid only until
    :meth:`close`. Retaining a Python reference does not extend the lease and
    does not authorize calls after session closure or process fork.
    """

    __slots__ = (
        "_closed",
        "_engine",
        "_guard",
        "_jobs",
        "_lease",
        "_owner_pid",
        "_provenance",
        "_write_set",
        "config",
        "recovery_results",
        "session_id",
    )

    def __init__(
        self,
        *,
        config: FilesystemEngineConfig,
        engine: LibraryEngine,
        write_set: RecoverableWriteSet,
        jobs: JobManager,
        provenance: TranslationProvenanceService,
        recovery_results: tuple[RecoveryResult, ...],
        lease: WorkspaceSessionLease,
    ) -> None:
        self.config = config
        self.session_id = uuid.uuid4().hex
        self.recovery_results = tuple(recovery_results)
        self._engine = engine
        self._write_set = write_set
        self._jobs = jobs
        self._provenance = provenance
        self._lease = lease
        self._owner_pid = os.getpid()
        self._closed = False
        self._guard = threading.Lock()

    @property
    def closed(self) -> bool:
        if os.getpid() != self._owner_pid:
            return True
        with self._guard:
            return self._closed

    def _require_owner_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise EngineSessionForkedError(
                "an engine session cannot be reused after process fork"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise EngineSessionClosedError("the engine session is closed")

    @property
    def engine(self) -> LibraryEngine:
        self._require_owner_process()
        with self._guard:
            self._require_open()
            return self._engine

    @property
    def write_set(self) -> RecoverableWriteSet:
        self._require_owner_process()
        with self._guard:
            self._require_open()
            return self._write_set

    @property
    def jobs(self) -> JobManager:
        self._require_owner_process()
        with self._guard:
            self._require_open()
            return self._jobs

    @property
    def provenance(self) -> TranslationProvenanceService:
        self._require_owner_process()
        with self._guard:
            self._require_open()
            return self._provenance

    def close(self) -> None:
        """Release bootstrap ownership without guessing worker policy."""

        if os.getpid() != self._owner_pid:
            self._closed = True
            return
        with self._guard:
            if self._closed:
                return
            try:
                self._lease.close()
            finally:
                # Even an OS unlock error leaves the underlying stream closed;
                # never advertise a half-open session or retry a dead lease.
                self._closed = True

    def __enter__(self) -> FilesystemEngineSession:
        self._require_owner_process()
        with self._guard:
            self._require_open()
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        try:
            self.close()
        except Exception:
            if exc_type is None:
                raise


def _safe_job_history_path(
    write_set: RecoverableWriteSet,
    config: FilesystemEngineConfig,
) -> Path:
    job_history = resolve_workspace_path(
        write_set.root,
        config.job_history,
        artifact="job_history",
        directory=False,
    )
    catalogue = resolve_workspace_path(
        write_set.root,
        config.paths.catalogue,
        artifact="catalogue",
        directory=False,
    )
    entries = resolve_workspace_path(
        write_set.root,
        config.paths.entries,
        artifact="entries",
        directory=True,
    )

    if workspace_paths_overlap(job_history, catalogue):
        raise RepositoryError(
            "job history and the item catalogue cannot overlap",
            code="unsafe_filesystem_engine_path",
            details={"artifact": "job_history"},
        )
    if workspace_paths_overlap(job_history, entries):
        raise RepositoryError(
            "job history and the entries directory cannot overlap",
            code="unsafe_filesystem_engine_path",
            details={"artifact": "job_history"},
        )
    return job_history


def open_filesystem_engine(
    *,
    config: FilesystemEngineConfig,
    bindings: FilesystemHostBindings,
    contribute_modules: ContributionFactory,
) -> FilesystemEngineSession:
    """Open, recover, compose, and rehydrate one filesystem engine session."""

    if not isinstance(config, FilesystemEngineConfig):
        raise TypeError("config must be FilesystemEngineConfig")
    if not isinstance(bindings, FilesystemHostBindings):
        raise TypeError("bindings must be FilesystemHostBindings")
    if not callable(contribute_modules):
        raise TypeError("contribute_modules must be callable")

    write_set = RecoverableWriteSet(config.workspace_root)
    lease = WorkspaceSessionLease.acquire(write_set)
    try:
        job_history_path = _safe_job_history_path(write_set, config)
        with write_set.recovery_lease():
            with bindings.recovery_lock_context():
                recovery_results = write_set.recover_all()

        jobs = JobManager(
            FilesystemJobHistoryRepository(
                job_history_path,
                read_json=bindings.jobs.read_json,
                write_json=bindings.jobs.write_json,
            ),
            keep=config.job_keep,
            event_keep=config.job_event_keep,
            checkpoint_interval=config.job_checkpoint_interval,
            id_factory=bindings.jobs.id_factory,
        )
        provenance = TranslationProvenanceService()
        engine = compose_filesystem_engine(
            paths=config.paths,
            resources=FilesystemEngineResources(
                write_set=write_set,
                jobs=jobs,
                provenance=provenance,
                workspace_lock_context_for=(
                    bindings.workspace_lock_context_for
                ),
            ),
            catalogue=bindings.catalogue,
            replica=bindings.replica,
            interchange=bindings.interchange,
            translation=bindings.translation,
            contribution_factory=contribute_modules,
            canvases=bindings.canvases,
        )
        # Composition must succeed before restart recovery mutates job history.
        # The session is still unpublished, so clients cannot observe a partial
        # startup between rehydration and return.
        jobs.rehydrate(strict=True)
        return FilesystemEngineSession(
            config=config,
            engine=engine,
            write_set=write_set,
            jobs=jobs,
            provenance=provenance,
            recovery_results=recovery_results,
            lease=lease,
        )
    except BaseException:
        try:
            lease.close()
        except Exception:
            pass
        raise


__all__ = [
    "EngineSessionClosedError",
    "EngineSessionError",
    "EngineSessionForkedError",
    "FilesystemEngineConfig",
    "FilesystemEngineSession",
    "FilesystemHostBindings",
    "JobHistoryBindings",
    "open_filesystem_engine",
]
