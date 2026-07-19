"""Headless background-job lifecycle and persistence contracts."""

from __future__ import annotations

import json
import os
import stat
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import librarytool.adapters.filesystem.job_history as job_history_adapter
from librarytool.adapters.filesystem import FilesystemJobHistoryRepository
from librarytool.engine import (
    ACTIVE_JOB_STATES,
    ConflictError,
    JobManager,
    JobState,
)


class MemoryHistory:
    def __init__(self, initial: Mapping[str, Mapping] | None = None) -> None:
        self.value = {
            str(job_id): dict(job) for job_id, job in (initial or {}).items()
        }
        self.saves: list[dict[str, dict]] = []

    def load(self):
        return {job_id: dict(job) for job_id, job in self.value.items()}

    def save(self, jobs):
        self.value = {str(job_id): dict(job) for job_id, job in jobs.items()}
        self.saves.append(self.load())


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.ticks = 0.0

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current

    def monotonic(self) -> float:
        return self.ticks


def manager(history=None, *, keep=50, clock=None) -> JobManager:
    clock = clock or Clock()
    counter = iter(range(1000))
    return JobManager(
        history,
        keep=keep,
        id_factory=lambda existing: f"job-{next(counter):04d}",
        utcnow=clock.now,
        monotonic=clock.monotonic,
    )


def test_track_transition_and_public_snapshot_are_transport_neutral():
    history = MemoryHistory()
    jobs = manager(history)
    record = {
        "build_id": "book-1",
        "total": 4,
        "secret": "must-never-persist",
        "prompt": "also private",
    }

    event = jobs.track(record, "ocr", label="A Herbal")
    assert not event.is_set()
    assert record["id"] == "job-0000"
    assert record["state"] == record["status"] == "running"
    assert jobs.records[record["id"]] is record

    record["done"] = 4
    record["errors"] = 1
    jobs.transition(record, "done (with errors)")

    assert record["state"] == "done"
    assert record["finished_at"]
    public = jobs.get(record["id"])
    assert public is not None
    assert public["label"] == "A Herbal"
    assert "secret" not in public and "prompt" not in public
    assert "secret" not in history.value[record["id"]]


def test_cancel_is_atomic_idempotent_and_observable_to_worker():
    jobs = manager()
    record = {"build_id": "book-1"}
    event = jobs.track(record, "ocr")

    first = jobs.request_cancel(record["id"])
    assert first is not None and first["state"] == "cancelling"
    assert event.is_set() and jobs.is_cancelled(record)
    assert record["cancel_requested"] is True

    jobs.transition(record, "cancelled", note="one page kept")
    second = jobs.request_cancel(record["id"])
    assert second is not None and second["state"] == "cancelled"
    assert record["note"] == "one page kept"
    assert jobs.request_cancel("missing") is None


def test_cancel_finish_race_cannot_resurrect_terminal_job():
    jobs = manager()
    record = {"status": "running"}
    real_event = jobs.track(record, "summarize")
    go = threading.Event()
    attempted = threading.Event()

    class FinishWhileCancelling:
        def set(self):
            real_event.set()
            go.set()
            assert attempted.wait(timeout=2)

        def is_set(self):
            return real_event.is_set()

    jobs.cancel_events[record["id"]] = FinishWhileCancelling()

    def finish() -> None:
        assert go.wait(timeout=2)
        attempted.set()
        jobs.transition(record, "done")

    worker = threading.Thread(target=finish)
    worker.start()
    jobs.request_cancel(record["id"])
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert record["state"] == "done"
    assert record["state"] not in ACTIVE_JOB_STATES


def test_checkpoint_is_throttled_and_internal_clock_is_not_persisted():
    history = MemoryHistory()
    clock = Clock()
    jobs = manager(history, clock=clock)
    record = {"done": 0, "total": 100}
    jobs.track(record, "segment")
    baseline = len(history.saves)

    record["done"] = 10
    jobs.checkpoint(record)
    assert len(history.saves) == baseline

    clock.ticks = 2.0
    jobs.checkpoint(record)
    assert len(history.saves) == baseline + 1
    assert history.value[record["id"]]["done"] == 10
    assert "_checkpoint_at" not in history.value[record["id"]]


def test_normalized_views_revisions_outputs_failures_and_event_cursor():
    jobs = manager()
    record = {
        "status": "queued",
        "build_id": "book-typed",
        "src": "scan-a",
        "page": 7,
        "total": 1,
        "input_revisions": {"regions": "rr-1"},
    }
    jobs.track(record, "replica.detect-regions")
    created = jobs.view(record["id"])
    assert created is not None
    assert created.state is JobState.QUEUED
    assert created.subject.item_id == "book-typed"
    assert created.subject.source_id == "scan-a"
    assert created.subject.page == 7
    assert created.revision == 1

    record["done"] = 1
    jobs.transition(record, "error", outputs=[{
        "kind": "replica.region-proposal", "ref": "book-typed/scan-a/7",
        "partial": True,
    }], failure={
        "code": "provider_error", "message": "provider unavailable",
        "retryable": True,
    })
    failed = jobs.view(record["id"])
    assert failed is not None and failed.state is JobState.FAILED
    assert failed.progress.completed == 1
    assert failed.revision == 2
    assert failed.error is not None and failed.error.code == "provider_error"
    assert failed.outputs[0].partial is True
    assert failed.cancellable is False

    events = jobs.events_after(0)
    assert [event.type for event in events] == ["created", "changed"]
    assert [event.sequence for event in events] == [1, 2]
    assert jobs.events_after(1)[0].job.revision == 2
    assert jobs.event_sequence == 2


def test_rehydrate_marks_only_active_work_interrupted():
    history = MemoryHistory({
        "translate-1": {
            "id": "translate-1", "kind": "translate:fr", "state": "running",
            "status": "running", "done": 2, "total": 5,
        },
        "publish-1": {
            "id": "publish-1", "kind": "publish", "state": "queued",
            "status": "queued",
        },
        "done-1": {
            "id": "done-1", "kind": "ocr", "state": "done", "status": "done",
        },
    })
    jobs = manager(history)

    jobs.rehydrate()

    translated = jobs.get("translate-1")
    published = jobs.get("publish-1")
    assert translated is not None and translated["state"] == "interrupted"
    assert translated["done"] == 2
    assert translated["note"] == \
        "interrupted by restart — progressive output kept"
    assert published is not None and published["note"] == \
        "interrupted by restart — not applied"
    assert jobs.get("done-1")["state"] == "done"


@pytest.mark.parametrize("failure", (OSError("unreadable"), ValueError("bad")))
def test_strict_rehydrate_propagates_history_integrity_failures(failure):
    class BrokenHistory:
        def load(self):
            raise failure

        def save(self, _jobs):
            raise AssertionError("save must not follow a failed load")

    jobs = manager(BrokenHistory())

    with pytest.raises(type(failure), match=str(failure)):
        jobs.rehydrate(strict=True)

    # Legacy callers can still elect the historical best-effort behavior.
    jobs.rehydrate()


def test_strict_rehydrate_propagates_interruption_save_failure():
    class UnwritableHistory(MemoryHistory):
        def save(self, _jobs):
            raise OSError("history is read-only")

    jobs = manager(UnwritableHistory({
        "active": {"id": "active", "kind": "ocr", "state": "running"}
    }))

    with pytest.raises(OSError, match="read-only"):
        jobs.rehydrate(strict=True)


def test_pruning_keeps_active_and_newest_finished_records():
    jobs = manager(keep=2)
    active = {"id": "active"}
    jobs.track(active, "ocr")
    finished = []
    for index in range(4):
        record = {"id": f"done-{index}"}
        jobs.track(record, "segment")
        jobs.transition(record, "done")
        finished.append(record)

    assert "active" in jobs.records
    assert len([
        row for row in jobs.list() if row.get("state") not in ACTIVE_JOB_STATES
    ]) == 2
    assert finished[-1]["id"] in jobs.records
    assert finished[0]["id"] not in jobs.records


def test_duplicate_live_id_is_rejected_without_replacing_worker_record():
    jobs = manager()
    first = {"id": "same-id"}
    jobs.track(first, "ocr")
    with pytest.raises(ConflictError) as caught:
        jobs.track({"id": "same-id"}, "publish")
    assert caught.value.code == "job_id_conflict"
    assert jobs.records["same-id"] is first


def test_item_deletion_guard_blocks_every_active_job_for_the_item():
    jobs = manager()
    jobs.track(
        {
            "id": "analysis",
            "subject": {"item_id": "book", "source_id": "primary"},
        },
        "summarize",
    )
    jobs.track({"id": "other", "build_id": "other-book"}, "publish")

    with pytest.raises(ConflictError) as caught:
        with jobs.item_deletion_guard("book"):
            raise AssertionError("an active job must prevent entry")

    assert caught.value.code == "item_jobs_active"
    assert caught.value.details == {
        "item_id": "book",
        "jobs": [
            {"job_id": "analysis", "kind": "summarize", "state": "running"}
        ],
    }


def test_item_deletion_guard_ignores_finished_and_other_item_jobs():
    jobs = manager()
    finished = {"id": "finished", "build_id": "book"}
    jobs.track(finished, "ocr")
    jobs.transition(finished, "done")
    jobs.track({"id": "other", "build_id": "other-book"}, "publish")

    with jobs.item_deletion_guard("book"):
        pass


def test_item_filter_uses_normalized_subject_identity():
    jobs = manager()
    jobs.track(
        {
            "id": "semantic-subject",
            "build_id": "legacy-other",
            "subject": {"item_id": "book"},
        },
        "segment",
    )
    jobs.track({"id": "legacy-subject", "build_id": "book"}, "ocr")

    assert {row["id"] for row in jobs.list(item_id="book")} == {
        "semantic-subject",
        "legacy-subject",
    }


def test_item_deletion_guard_serializes_concurrent_job_registration():
    jobs = manager()
    started = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def register() -> None:
        started.set()
        try:
            jobs.track({"id": "later", "build_id": "book"}, "ocr")
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)
        finally:
            finished.set()

    with jobs.item_deletion_guard("book"):
        worker = threading.Thread(target=register)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.05)

    assert finished.wait(1)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert failures == []
    assert jobs.get("later") is not None


def test_filesystem_history_adapter_uses_injected_atomic_json_callbacks(tmp_path: Path):
    path = tmp_path / "output" / "jobs.json"

    def read_json(target: Path, default):
        if not target.is_file():
            return default
        return json.loads(target.read_text(encoding="utf-8"))

    def write_json(target: Path, value) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    repository = FilesystemJobHistoryRepository(
        path, read_json=read_json, write_json=write_json
    )
    jobs = manager(repository)
    record = {"build_id": "book"}
    jobs.track(record, "ocr")
    jobs.transition(record, "done")

    assert repository.path == path
    assert repository.load()[record["id"]]["state"] == "done"


def test_filesystem_history_native_json_round_trips_without_callbacks(
    tmp_path: Path,
):
    path = tmp_path / "output" / "jobs.json"
    repository = FilesystemJobHistoryRepository(path)

    assert repository.load() == {}

    repository.save({
        7: {
            "kind": "ocr",
            "label": "Flore française",
            "state": "done",
        }
    })

    assert repository.load() == {
        "7": {
            "kind": "ocr",
            "label": "Flore française",
            "state": "done",
        }
    }
    assert path.read_bytes().endswith(b"\n")
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))


def test_filesystem_history_native_writer_replaces_a_complete_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "jobs.json"
    repository = FilesystemJobHistoryRepository(path)
    repository.save({"old": {"state": "done"}})
    original_bytes = path.read_bytes()
    real_replace = job_history_adapter.os.replace
    observed: dict[str, object] = {}

    def inspect_replace(source: Path, destination: Path) -> None:
        observed["destination"] = destination
        observed["old"] = destination.read_bytes()
        observed["new"] = json.loads(source.read_text(encoding="utf-8"))
        observed["temporary"] = source
        real_replace(source, destination)

    monkeypatch.setattr(job_history_adapter.os, "replace", inspect_replace)
    replacement = {"new": {"label": "Herbal", "state": "running"}}

    repository.save(replacement)

    assert observed["destination"] == path
    assert observed["old"] == original_bytes
    assert observed["new"] == replacement
    assert repository.load() == replacement
    assert not Path(observed["temporary"]).exists()


def test_filesystem_history_failed_replace_preserves_the_previous_snapshot(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "jobs.json"
    repository = FilesystemJobHistoryRepository(path)
    previous = {"old": {"state": "done"}}
    repository.save(previous)
    temporary: list[Path] = []

    def fail_replace(source: Path, _destination: Path) -> None:
        temporary.append(source)
        raise OSError("replacement unavailable")

    monkeypatch.setattr(job_history_adapter.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replacement unavailable"):
        repository.save({"new": {"state": "running"}})

    assert repository.load() == previous
    assert temporary and not temporary[0].exists()
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ("{", None),
        (
            '{"job":{"state":"done","state":"running"}}',
            "duplicate JSON key: state",
        ),
        ('{"job":{"progress":NaN}}', "non-finite JSON number: NaN"),
        ('{"job":{"progress":Infinity}}', "non-finite JSON number: Infinity"),
        (
            '{"job":{"progress":-Infinity}}',
            "non-finite JSON number: -Infinity",
        ),
    ),
)
def test_filesystem_history_native_reader_rejects_invalid_json(
    tmp_path: Path,
    payload: str,
    message: str | None,
):
    path = tmp_path / "jobs.json"
    path.write_text(payload, encoding="utf-8")
    repository = FilesystemJobHistoryRepository(path)

    context = pytest.raises(ValueError, match=message) if message else (
        pytest.raises(ValueError)
    )
    with context:
        repository.load()


def test_filesystem_history_native_writer_rejects_non_finite_values(
    tmp_path: Path,
):
    path = tmp_path / "jobs.json"
    repository = FilesystemJobHistoryRepository(path)
    previous = {"old": {"state": "done"}}
    repository.save(previous)

    with pytest.raises(ValueError, match="Out of range float values"):
        repository.save({"job": {"progress": float("nan")}})

    assert repository.load() == previous
    assert not tuple(path.parent.glob(f".{path.name}.*.tmp"))


def test_filesystem_history_native_io_rejects_a_symbolic_link(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text('{"old":{"state":"done"}}', encoding="utf-8")
    link = tmp_path / "jobs.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("file symlinks are unavailable on this platform")
    repository = FilesystemJobHistoryRepository(link)

    with pytest.raises(OSError, match="not a regular file"):
        repository.load()
    with pytest.raises(OSError, match="may not be a symbolic link"):
        repository.save({"new": {"state": "running"}})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "old": {"state": "done"}
    }


def test_filesystem_history_native_io_rejects_a_hardlink_without_mutation(
    tmp_path: Path,
):
    external = tmp_path / "external.json"
    external.write_bytes(b'{"old":{"state":"done"}}\n')
    external.chmod(0o640)
    path = tmp_path / "jobs.json"
    try:
        os.link(external, path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hardlinks are unavailable: {exc}")
    repository = FilesystemJobHistoryRepository(path)
    before = (
        external.read_bytes(),
        stat.S_IMODE(external.stat().st_mode),
        external.stat().st_nlink,
    )

    with pytest.raises(OSError, match="not a regular file"):
        repository.load()
    with pytest.raises(OSError, match="not one private regular file"):
        repository.save({"new": {"state": "running"}})

    assert path.samefile(external)
    assert path.read_bytes() == before[0]
    assert (
        external.read_bytes(),
        stat.S_IMODE(external.stat().st_mode),
        external.stat().st_nlink,
    ) == before
    assert not tuple(tmp_path.glob(f".{path.name}.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is not portable")
def test_filesystem_history_native_writer_fsyncs_the_parent_directory(
    tmp_path: Path,
    monkeypatch,
):
    path = tmp_path / "output" / "jobs.json"
    repository = FilesystemJobHistoryRepository(path)
    real_open = job_history_adapter.os.open
    real_fsync = job_history_adapter.os.fsync
    directory_descriptors: set[int] = set()
    directory_fsyncs: list[int] = []

    def track_open(target, flags, *args, **kwargs):
        descriptor = real_open(target, flags, *args, **kwargs)
        if Path(target) == path.parent:
            directory_descriptors.add(descriptor)
        return descriptor

    def track_fsync(descriptor: int) -> None:
        if descriptor in directory_descriptors:
            directory_fsyncs.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(job_history_adapter.os, "open", track_open)
    monkeypatch.setattr(job_history_adapter.os, "fsync", track_fsync)

    repository.save({"job": {"state": "done"}})

    assert directory_fsyncs
    assert repository.load() == {"job": {"state": "done"}}
