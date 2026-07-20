"""Framework-neutral lifecycle management for background work.

The manager owns job identity, lifecycle transitions, cooperative cancellation,
bounded history, progress snapshots, and restart recovery.  It deliberately
does not own threads or provider-specific work: a CLI, Flask process, desktop
shell, or future Qt/Godot client may choose a different executor while sharing
the same observable lifecycle contract.

During the incremental migration, workers may keep their existing mutable job
dictionaries.  ``JobManager`` serializes lifecycle mutations and exposes only
an allowlisted public snapshot to repositories and clients.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .errors import ConflictError, ValidationError
from .ports import JobHistoryRepositoryPort


log = logging.getLogger(__name__)

ACTIVE_JOB_STATES = ("queued", "running", "cancelling")
_OPERATION_RECEIPTS_KEY = "$operation_receipts"
_PORTABLE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMMAND_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_JOB_FIELDS = (
    "id",
    "kind",
    "build_id",
    "label",
    "volume",
    "state",
    "status",
    "done",
    "total",
    "errors",
    "error",
    "note",
    "created_at",
    "finished_at",
    "subject",
    "progress",
    "cancellable",
    "revision",
    "updated_at",
    "input_revisions",
    "outputs",
    "failure",
)

_STATUS_STATES = {
    "queued": "queued",
    "running": "running",
    "cancelling": "cancelling",
    "cancelled": "cancelled",
    "error": "failed",
    "failed": "failed",
    "done": "done",
    "done (with errors)": "done",
    "interrupted": "interrupted",
}

JobIdFactory = Callable[[set[str]], str]
UtcNow = Callable[[], datetime]
MonotonicClock = Callable[[], float]


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    DONE = "done"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class JobSubject:
    item_id: str = ""
    source_id: str = ""
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "item_id": self.item_id,
            "source_id": self.source_id,
        }
        if self.page is not None:
            value["page"] = self.page
        return value


@dataclass(frozen=True, slots=True)
class JobProgress:
    completed: int = 0
    total: int = 0
    unit: str = ""
    phase: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "total": self.total,
            "unit": self.unit,
            "phase": self.phase,
        }


@dataclass(frozen=True, slots=True)
class JobOutput:
    kind: str
    ref: str
    partial: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "partial": self.partial}


@dataclass(frozen=True, slots=True)
class JobFailure:
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            value["details"] = dict(self.details)
        return value


@dataclass(frozen=True, slots=True)
class JobView:
    job_id: str
    kind: str
    state: JobState
    subject: JobSubject
    progress: JobProgress
    cancellable: bool
    revision: int
    created_at: str
    updated_at: str
    finished_at: str = ""
    note: str = ""
    error: JobFailure | None = None
    input_revisions: Mapping[str, Any] = field(default_factory=dict)
    outputs: tuple[JobOutput, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.job_id,
            "kind": self.kind,
            "state": self.state.value,
            "subject": self.subject.as_dict(),
            "progress": self.progress.as_dict(),
            "cancellable": self.cancellable,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "note": self.note,
            "error": self.error.as_dict() if self.error is not None else None,
            "input_revisions": dict(self.input_revisions),
            "outputs": [output.as_dict() for output in self.outputs],
        }


@dataclass(frozen=True, slots=True)
class JobEvent:
    sequence: int
    type: str
    job: JobView

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.type,
            "job": self.job.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class JobCommandReceipt:
    """Durable binding between an idempotent command and its job outcome."""

    operation_id: str
    command_sha256: str
    job: JobView

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "job_id": self.job.job_id,
            "state": self.job.state.value,
            "terminal": self.job.state.value not in ACTIVE_JOB_STATES,
        }


def _default_id_factory(existing: set[str]) -> str:
    while True:
        candidate = uuid.uuid4().hex[:16]
        if candidate not in existing:
            return candidate


def _default_utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobManager:
    """Thread-safe lifecycle registry shared by all background processors.

    ``records``, ``cancel_events``, and ``lock`` are compatibility views for
    workers that predate the engine package.  New code should use the query and
    command methods instead of mutating those collections directly.
    """

    def __init__(
        self,
        repository: JobHistoryRepositoryPort | None = None,
        *,
        keep: int = 50,
        id_factory: JobIdFactory | None = None,
        utcnow: UtcNow | None = None,
        monotonic: MonotonicClock | None = None,
        checkpoint_interval: float = 1.0,
        event_keep: int = 500,
        receipt_keep: int | None = None,
    ) -> None:
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 0:
            raise ValidationError("job history retention must be a non-negative integer")
        if checkpoint_interval < 0:
            raise ValidationError("job checkpoint interval must be non-negative")
        if not isinstance(event_keep, int) or isinstance(event_keep, bool) or event_keep < 0:
            raise ValidationError("job event retention must be a non-negative integer")
        if receipt_keep is not None and (
            not isinstance(receipt_keep, int)
            or isinstance(receipt_keep, bool)
            or receipt_keep < keep
        ):
            raise ValidationError(
                "command receipt retention must cover job history retention"
            )
        self._repository = repository
        self._keep = keep
        self._id_factory = id_factory or _default_id_factory
        self._utcnow = utcnow or _default_utcnow
        self._monotonic = monotonic or time.monotonic
        self._checkpoint_interval = float(checkpoint_interval)
        self._event_keep = event_keep
        self._receipt_keep = (
            max(keep, 500) if receipt_keep is None else receipt_keep
        )
        self._records: dict[str, MutableMapping[str, Any]] = {}
        self._operation_receipts: dict[str, MutableMapping[str, Any]] = {}
        self._operation_by_job_id: dict[str, str] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._event_sequence = 0
        self._events: list[JobEvent] = []

    @property
    def records(self) -> dict[str, MutableMapping[str, Any]]:
        """Transitional mutable registry; prefer ``list`` or ``get``."""
        return self._records

    @property
    def cancel_events(self) -> dict[str, threading.Event]:
        """Transitional event registry used by existing worker loops."""
        return self._cancel_events

    @property
    def lock(self) -> threading.Lock:
        """Compatibility lock for adapters composing additional rows."""
        return self._lock

    @property
    def keep(self) -> int:
        return self._keep

    @staticmethod
    def state_of(status: object) -> str:
        return _STATUS_STATES.get(str(status or ""), "running")

    @staticmethod
    def interruption_note(kind: object) -> str:
        value = str(kind or "")
        if value == "ocr" or value.startswith("translate") or value == "annotate":
            return "interrupted by restart — progressive output kept"
        if value == "publish":
            return "interrupted by restart — not applied"
        return "interrupted by restart — output not written"

    @staticmethod
    def public(job: Mapping[str, Any]) -> dict[str, Any]:
        """Return the stable, credential-free client/persistence projection."""
        return {key: job.get(key) for key in PUBLIC_JOB_FIELDS if key in job}

    def list(
        self,
        *,
        states: Iterable[str] = (),
        kinds: Iterable[str] = (),
        item_id: str = "",
    ) -> list[dict[str, Any]]:
        """Return a stable public snapshot with optional engine-side filters."""
        state_filter = {str(value) for value in states if str(value)}
        kind_filter = {str(value) for value in kinds if str(value)}
        item_filter = str(item_id or "")
        with self._lock:
            rows = [
                self.public(job)
                for job in self._records.values()
                if (not state_filter or str(job.get("state") or "") in state_filter)
                and (not kind_filter or str(job.get("kind") or "") in kind_filter)
                and (
                    not item_filter
                    or self._subject(job).item_id == item_filter
                )
            ]
        rows.sort(key=lambda row: (
            str(row.get("created_at") or ""), str(row.get("id") or "")
        ))
        return rows

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._records.get(str(job_id or ""))
            return self.public(job) if job is not None else None

    @staticmethod
    def validate_operation_id(value: object) -> str:
        operation_id = value if isinstance(value, str) else ""
        if not operation_id:
            raise ValidationError(
                "an idempotency key is required",
                code="idempotency_key_required",
            )
        if _PORTABLE_OPERATION_ID.fullmatch(operation_id) is None:
            raise ValidationError(
                "the idempotency key is not a portable identifier",
                code="invalid_idempotency_key",
                details={"idempotency_key": operation_id[:128]},
            )
        return operation_id

    @staticmethod
    def validate_command_sha256(value: object) -> str:
        command_sha256 = value if isinstance(value, str) else ""
        if _COMMAND_SHA256.fullmatch(command_sha256) is None:
            raise ValidationError(
                "the command fingerprint must be a lowercase SHA-256 digest",
                code="invalid_command_fingerprint",
            )
        return command_sha256

    def command_receipt(
        self,
        operation_id: str,
        command_sha256: str,
        *,
        kind: str = "",
    ) -> JobCommandReceipt | None:
        """Return an exact replay or reject reuse for another command."""

        operation_id = self.validate_operation_id(operation_id)
        command_sha256 = self.validate_command_sha256(command_sha256)
        expected_kind = str(kind or "")
        with self._lock:
            stored = self._operation_receipts.get(operation_id)
            if stored is None:
                return None
            if (
                str(stored.get("command_sha256") or "") != command_sha256
                or (expected_kind and str(stored.get("kind") or "") != expected_kind)
            ):
                raise ConflictError(
                    "the idempotency key belongs to a different command",
                    code="operation_id_conflict",
                    details={"operation_id": operation_id},
                )
            job_id = str(stored.get("job_id") or "")
            raw_job = self._records.get(job_id) or stored.get("job")
            if not isinstance(raw_job, Mapping):
                raise ConflictError(
                    "the idempotency receipt has no recoverable job outcome",
                    code="operation_receipt_incomplete",
                    details={"operation_id": operation_id, "job_id": job_id},
                    retryable=True,
                )
            return JobCommandReceipt(
                operation_id=operation_id,
                command_sha256=command_sha256,
                job=self.view_of(raw_job),
            )

    def view(self, job_id: str) -> JobView | None:
        """Return the normalized contract used by versioned transports."""
        with self._lock:
            job = self._records.get(str(job_id or ""))
            return self.view_of(job) if job is not None else None

    def list_views(
        self,
        *,
        states: Iterable[str] = (),
        kinds: Iterable[str] = (),
        item_id: str = "",
    ) -> tuple[JobView, ...]:
        return tuple(
            self.view_of(row)
            for row in self.list(states=states, kinds=kinds, item_id=item_id)
        )

    def active(self) -> list[dict[str, Any]]:
        return self.list(states=ACTIVE_JOB_STATES)

    @contextmanager
    def item_deletion_guard(self, item_id: str):
        """Reserve an idle item against concurrent job registration.

        The caller must enter this guard only after acquiring the shared
        item/workspace mutation gate, and must retain both through lifecycle
        commit. Job starts must acquire that same outer gate, revalidate the
        live item, and then call :meth:`track`. This lock ordering makes the
        active-job check and registration mutually exclusive without teaching
        the lifecycle service about provider-specific job kinds.

        No other ``JobManager`` method may be called by the guarded thread
        while the context is active because the manager lock is deliberately
        non-reentrant.
        """

        item_id = str(item_id or "").strip()
        if not item_id:
            raise ValidationError(
                "item id is required",
                code="item_id_required",
            )
        self._lock.acquire()
        try:
            blockers = []
            for job in self._records.values():
                state = str(job.get("state") or self.state_of(job.get("status")))
                if state not in ACTIVE_JOB_STATES:
                    continue
                subject = self._subject(job)
                if subject.item_id != item_id:
                    continue
                blockers.append(
                    {
                        "job_id": str(job.get("id") or ""),
                        "kind": str(job.get("kind") or ""),
                        "state": state,
                    }
                )
            if blockers:
                blockers.sort(
                    key=lambda row: (row["kind"], row["job_id"])
                )
                raise ConflictError(
                    "active jobs prevent item deletion",
                    code="item_jobs_active",
                    details={"item_id": item_id, "jobs": blockers},
                )
            yield
        finally:
            self._lock.release()

    def events_after(self, sequence: int, *, limit: int = 200) -> tuple[JobEvent, ...]:
        """Return bounded in-process lifecycle events after a client cursor."""
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValidationError("job event sequence must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValidationError("job event limit must be a positive integer")
        with self._lock:
            return tuple(event for event in self._events if event.sequence > sequence)[:limit]

    @property
    def event_sequence(self) -> int:
        with self._lock:
            return self._event_sequence

    def track(
        self,
        job: MutableMapping[str, Any],
        kind: str,
        *,
        label: str = "",
    ) -> threading.Event:
        """Register a worker-owned record and return its cancellation event."""
        if not isinstance(job, MutableMapping):
            raise ValidationError("job must be a mutable mapping")
        kind = str(kind or "").strip()
        if not kind:
            raise ValidationError("job kind is required")
        raw_operation_id = job.get("operation_id")
        raw_command_sha256 = job.get("command_sha256")
        operation_id = ""
        command_sha256 = ""
        if raw_operation_id is not None or raw_command_sha256 is not None:
            operation_id = self.validate_operation_id(raw_operation_id)
            command_sha256 = self.validate_command_sha256(raw_command_sha256)
        event = threading.Event()
        with self._lock:
            job_id = str(job.get("id") or self._id_factory(set(self._records))).strip()
            if not job_id:
                raise ValidationError("job id is required")
            if job_id == _OPERATION_RECEIPTS_KEY:
                raise ValidationError(
                    "the job id is reserved for persistence metadata",
                    code="reserved_job_id",
                    details={"job_id": job_id},
                )
            current = self._records.get(job_id)
            if current is not None and current is not job:
                raise ConflictError(
                    "job id is already registered",
                    code="job_id_conflict",
                    details={"job_id": job_id},
                )
            if operation_id:
                prior = self._operation_receipts.get(operation_id)
                if prior is not None and (
                    str(prior.get("command_sha256") or "") != command_sha256
                    or str(prior.get("kind") or "") != kind
                    or str(prior.get("job_id") or "") != job_id
                ):
                    raise ConflictError(
                        "the idempotency key is already registered",
                        code="operation_id_conflict",
                        details={"operation_id": operation_id},
                    )
            now = self._timestamp()
            job["id"] = job_id
            job.setdefault("kind", kind)
            job.setdefault("build_id", "")
            for key, value in (
                ("done", 0), ("total", 0), ("errors", 0), ("note", "")
            ):
                job.setdefault(key, value)
            job.setdefault("status", "running")
            job["label"] = label or str(job.get("label") or "")
            job["state"] = self.state_of(job["status"])
            job["created_at"] = now
            job["updated_at"] = now
            job["finished_at"] = ""
            job.setdefault("cancellable", True)
            job["revision"] = max(1, self._integer(job.get("revision"), 1))
            job.setdefault("subject", self._subject(job).as_dict())
            job.setdefault("input_revisions", {})
            job.setdefault("outputs", [])
            self._records[job_id] = job
            if operation_id:
                self._operation_receipts[operation_id] = {
                    "operation_id": operation_id,
                    "command_sha256": command_sha256,
                    "kind": kind,
                    "job_id": job_id,
                    "job": self.public(job),
                }
                self._operation_by_job_id[job_id] = operation_id
            self._cancel_events[job_id] = event
            self._prune_locked()
            self._prune_command_receipts_locked()
            self._save_locked()
            self._emit_locked("created", job)
        return event

    def transition(
        self,
        job: MutableMapping[str, Any],
        status: str,
        **fields: Any,
    ) -> None:
        """Move a worker record to a new lifecycle state and persist it."""
        with self._lock:
            self._transition_locked(job, status, **fields)

    def transition_locked(
        self,
        job: MutableMapping[str, Any],
        status: str,
        **fields: Any,
    ) -> None:
        """Compatibility entry point for a caller already holding ``lock``."""
        self._transition_locked(job, status, **fields)

    def checkpoint(self, job: MutableMapping[str, Any], *, force: bool = False) -> None:
        """Persist live progress at a throttled processor boundary."""
        current = self._monotonic()
        with self._lock:
            if self._records.get(str(job.get("id") or "")) is not job:
                return
            last = float(job.get("_checkpoint_at") or 0.0)
            if not force and current - last < self._checkpoint_interval:
                return
            job["_checkpoint_at"] = current
            self._touch_locked(job)
            self._save_locked()
            self._emit_locked("progress", job)

    def request_cancel(
        self,
        job_id: str,
        *,
        fallback: MutableMapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically request cooperative cancellation.

        Terminal jobs are returned unchanged, making cancellation idempotent.
        ``fallback`` exists only for legacy per-kind endpoints whose tests may
        construct an untracked record; production work should always be tracked.
        """
        job_id = str(job_id or "")
        with self._lock:
            job = self._records.get(job_id) or fallback
            if job is None:
                return None
            state = job.get("state") or self.state_of(job.get("status"))
            event = self._cancel_events.get(job_id)
            if state in ACTIVE_JOB_STATES:
                if event is not None:
                    event.set()
                if job.get("kind") == "ocr" or fallback is not None:
                    job["cancel_requested"] = True
                self._transition_locked(job, "cancelling", event_type="cancel-requested")
            return dict(job)

    def is_cancelled(self, job: Mapping[str, Any]) -> bool:
        event = self._cancel_events.get(str(job.get("id") or ""))
        return event is not None and event.is_set()

    def rehydrate(self, *, strict: bool = False) -> None:
        """Load history and mark work interrupted by the previous shutdown."""
        if self._repository is None:
            return
        try:
            stored = self._repository.load()
        except (OSError, ValueError):
            if strict:
                raise
            return
        if not isinstance(stored, Mapping) or not stored:
            if strict and not isinstance(stored, Mapping):
                raise ValueError("job history must be a mapping")
            return
        raw_receipts = stored.get(_OPERATION_RECEIPTS_KEY, {})
        if strict and not isinstance(raw_receipts, Mapping):
            raise ValueError("job operation receipts must be a mapping")
        if strict and any(
            raw_id != _OPERATION_RECEIPTS_KEY
            and (not str(raw_id or "") or not isinstance(raw, Mapping))
            for raw_id, raw in stored.items()
        ):
            raise ValueError("job history contains an invalid record")
        now = self._timestamp()
        with self._lock:
            if isinstance(raw_receipts, Mapping):
                for raw_operation_id, raw_receipt in raw_receipts.items():
                    try:
                        operation_id = self.validate_operation_id(
                            raw_operation_id)
                        if not isinstance(raw_receipt, Mapping):
                            raise ValueError("receipt must be a mapping")
                        command_sha256 = self.validate_command_sha256(
                            raw_receipt.get("command_sha256"))
                        job_id = str(raw_receipt.get("job_id") or "")
                        kind = str(raw_receipt.get("kind") or "")
                        outcome = raw_receipt.get("job")
                        if not job_id or not kind or not isinstance(
                            outcome, Mapping
                        ):
                            raise ValueError("receipt is incomplete")
                    except (ValidationError, ValueError):
                        if strict:
                            raise ValueError(
                                "job history contains an invalid operation receipt"
                            ) from None
                        continue
                    self._operation_receipts[operation_id] = {
                        "operation_id": operation_id,
                        "command_sha256": command_sha256,
                        "kind": kind,
                        "job_id": job_id,
                        "job": self.public(outcome),
                    }
                    self._operation_by_job_id[job_id] = operation_id
            for raw_id, raw in stored.items():
                job_id = str(raw_id or "")
                if (
                    raw_id == _OPERATION_RECEIPTS_KEY
                    or not job_id
                    or job_id in self._records
                    or not isinstance(raw, Mapping)
                ):
                    continue
                job: MutableMapping[str, Any] = self.public(raw)
                job["id"] = job_id
                job.setdefault("cancellable", True)
                job["revision"] = max(1, self._integer(job.get("revision"), 1))
                job.setdefault("updated_at", str(
                    job.get("finished_at") or job.get("created_at") or now
                ))
                job.setdefault("subject", self._subject(job).as_dict())
                job.setdefault("input_revisions", {})
                job.setdefault("outputs", [])
                if job.get("state") in ACTIVE_JOB_STATES or not job.get("state"):
                    job["status"] = job["state"] = "interrupted"
                    job["note"] = self.interruption_note(job.get("kind"))
                    job["finished_at"] = job.get("finished_at") or now
                    job["updated_at"] = now
                    job["revision"] = self._integer(job.get("revision"), 0) + 1
                self._records[job_id] = job
                self._refresh_command_receipt_locked(job)
                self._emit_locked("recovered", job)
            self._prune_locked()
            self._prune_command_receipts_locked()
            self._save_locked(strict=strict)

    def _timestamp(self) -> str:
        value = self._utcnow()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _transition_locked(
        self,
        job: MutableMapping[str, Any],
        status: str,
        *,
        event_type: str = "changed",
        **fields: Any,
    ) -> None:
        job.update(fields)
        job["status"] = status
        job["state"] = self.state_of(status)
        now = self._timestamp()
        self._touch_locked(job, timestamp=now)
        if job["state"] not in ACTIVE_JOB_STATES and not job.get("finished_at"):
            job["finished_at"] = now
        self._refresh_command_receipt_locked(job)
        if self._records.get(str(job.get("id") or "")) is job:
            if job["state"] not in ACTIVE_JOB_STATES:
                self._prune_locked()
                self._prune_command_receipts_locked()
            self._save_locked()
            self._emit_locked(event_type, job)

    @classmethod
    def view_of(cls, job: Mapping[str, Any]) -> JobView:
        """Normalize either a legacy worker record or a public snapshot."""
        state_value = cls.state_of(job.get("state") or job.get("status"))
        try:
            state = JobState(state_value)
        except ValueError:
            state = JobState.RUNNING
        progress_raw = job.get("progress")
        progress = progress_raw if isinstance(progress_raw, Mapping) else {}
        outputs = []
        for raw in job.get("outputs") or ():
            if not isinstance(raw, Mapping):
                continue
            kind = str(raw.get("kind") or "")
            ref = str(raw.get("ref") or "")
            if kind and ref:
                outputs.append(JobOutput(kind, ref, bool(raw.get("partial"))))
        failure_raw = job.get("failure")
        failure = None
        if isinstance(failure_raw, Mapping):
            message = str(failure_raw.get("message") or job.get("error") or "")
            if message:
                details = failure_raw.get("details")
                failure = JobFailure(
                    str(failure_raw.get("code") or "job_failed"),
                    message,
                    bool(failure_raw.get("retryable")),
                    dict(details) if isinstance(details, Mapping) else {},
                )
        elif job.get("error"):
            failure = JobFailure("job_failed", str(job.get("error")))
        revisions = job.get("input_revisions")
        return JobView(
            job_id=str(job.get("id") or ""),
            kind=str(job.get("kind") or ""),
            state=state,
            subject=cls._subject(job),
            progress=JobProgress(
                completed=cls._integer(
                    progress.get("completed"), cls._integer(job.get("done"), 0)
                ),
                total=cls._integer(
                    progress.get("total"), cls._integer(job.get("total"), 0)
                ),
                unit=str(progress.get("unit") or ""),
                phase=str(progress.get("phase") or ""),
            ),
            cancellable=(
                bool(job.get("cancellable", True)) and state.value in ACTIVE_JOB_STATES
            ),
            revision=max(0, cls._integer(job.get("revision"), 0)),
            created_at=str(job.get("created_at") or ""),
            updated_at=str(
                job.get("updated_at") or job.get("finished_at")
                or job.get("created_at") or ""
            ),
            finished_at=str(job.get("finished_at") or ""),
            note=str(job.get("note") or ""),
            error=failure,
            input_revisions=dict(revisions) if isinstance(revisions, Mapping) else {},
            outputs=tuple(outputs),
        )

    @staticmethod
    def _integer(value: object, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @classmethod
    def _subject(cls, job: Mapping[str, Any]) -> JobSubject:
        raw = job.get("subject")
        subject = raw if isinstance(raw, Mapping) else {}
        page_value = subject.get("page", job.get("page"))
        page = None if page_value in (None, "") else cls._integer(page_value, -1)
        if page is not None and page < 1:
            page = None
        return JobSubject(
            item_id=str(subject.get("item_id") or job.get("build_id") or ""),
            source_id=str(subject.get("source_id") or job.get("src") or ""),
            page=page,
        )

    def _touch_locked(
        self,
        job: MutableMapping[str, Any],
        *,
        timestamp: str | None = None,
    ) -> None:
        job["revision"] = max(0, self._integer(job.get("revision"), 0)) + 1
        job["updated_at"] = timestamp or self._timestamp()

    def _refresh_command_receipt_locked(
        self, job: Mapping[str, Any]
    ) -> None:
        job_id = str(job.get("id") or "")
        if not job_id:
            return
        operation_id = self._operation_by_job_id.get(job_id)
        receipt = self._operation_receipts.get(operation_id or "")
        if receipt is not None:
            receipt["job"] = self.public(job)

    def _emit_locked(self, event_type: str, job: Mapping[str, Any]) -> None:
        self._event_sequence += 1
        if not self._event_keep:
            return
        self._events.append(JobEvent(
            sequence=self._event_sequence,
            type=str(event_type or "changed"),
            job=self.view_of(job),
        ))
        if len(self._events) > self._event_keep:
            del self._events[:len(self._events) - self._event_keep]

    def _prune_locked(self) -> None:
        finished = sorted(
            (
                job for job in self._records.values()
                if job.get("state") not in ACTIVE_JOB_STATES
            ),
            key=lambda job: str(
                job.get("finished_at") or job.get("created_at") or ""
            ),
            reverse=True,
        )
        for old in finished[self._keep:]:
            job_id = str(old.get("id") or "")
            self._records.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    def _prune_command_receipts_locked(self) -> None:
        terminal = sorted(
            (
                (operation_id, receipt)
                for operation_id, receipt in self._operation_receipts.items()
                if str((receipt.get("job") or {}).get("state") or "")
                not in ACTIVE_JOB_STATES
            ),
            key=lambda pair: str(
                (pair[1].get("job") or {}).get("finished_at")
                or (pair[1].get("job") or {}).get("updated_at")
                or (pair[1].get("job") or {}).get("created_at")
                or pair[0]
            ),
            reverse=True,
        )
        for operation_id, receipt in terminal[self._receipt_keep:]:
            self._operation_receipts.pop(operation_id, None)
            job_id = str(receipt.get("job_id") or "")
            if self._operation_by_job_id.get(job_id) == operation_id:
                self._operation_by_job_id.pop(job_id, None)

    def _save_locked(self, *, strict: bool = False) -> None:
        if self._repository is None:
            return
        for job in self._records.values():
            self._refresh_command_receipt_locked(job)
        snapshot = {
            job_id: self.public(job) for job_id, job in self._records.items()
        }
        if self._operation_receipts:
            snapshot[_OPERATION_RECEIPTS_KEY] = {
                operation_id: {
                    "command_sha256": str(
                        receipt.get("command_sha256") or ""),
                    "kind": str(receipt.get("kind") or ""),
                    "job_id": str(receipt.get("job_id") or ""),
                    "job": self.public(receipt.get("job") or {}),
                }
                for operation_id, receipt in self._operation_receipts.items()
            }
        try:
            self._repository.save(snapshot)
        except OSError:
            if strict:
                raise
            log.warning("could not persist the job registry", exc_info=True)


__all__ = [
    "ACTIVE_JOB_STATES",
    "JobEvent",
    "JobCommandReceipt",
    "JobFailure",
    "JobManager",
    "JobOutput",
    "JobProgress",
    "JobState",
    "JobSubject",
    "JobView",
    "PUBLIC_JOB_FIELDS",
]
