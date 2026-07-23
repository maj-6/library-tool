from __future__ import annotations

import pytest
import server

from librarytool.engine.errors import NotFoundError
from librarytool.engine.correction_transforms import (
    CorrectionTransformCommand,
    CorrectionTransformRunResult,
    CorrectionTransformService,
    OcrFollowupOutcome,
    OcrFollowupState,
)
from librarytool.engine.jobs import JobManager


def _command() -> CorrectionTransformCommand:
    return CorrectionTransformCommand(
        item_id="book-1",
        artifact_id="capture-1",
        artifact_revision="artifact-r1",
        source_revision="bytes-r1",
        source_sha256="a" * 64,
        quad=((0, 0), (1, 0), (1, 1), (0, 1)),
        operation_id="transform-host-op",
    )


def test_process_host_rejects_transform_start_after_item_disappears(
    monkeypatch,
):
    monkeypatch.setattr(server.lib, "load_json", lambda _path, _default: {})
    jobs = JobManager()
    service = CorrectionTransformService(
        jobs,
        start_guard_for=server._correction_transform_job_start_guard,
    )

    with pytest.raises(NotFoundError) as missing:
        service.queue(_command())

    assert missing.value.code == "correction_item_not_found"
    assert jobs.list() == []


def test_process_host_deduplicates_window_independent_transform_threads(
    monkeypatch,
):
    command = _command()
    jobs = JobManager()
    job_id = CorrectionTransformService.job_id_for(command.operation_id)
    executed = []

    def execute(value):
        executed.append(value)
        return CorrectionTransformRunResult(
            job_id,
            value.operation_id,
            None,
            OcrFollowupOutcome(OcrFollowupState.NOT_REQUESTED),
            cancelled_before_commit=True,
        )

    service = CorrectionTransformService(jobs, executor=execute)
    queued = service.queue(command)
    threads = []

    class DeferredThread:
        def __init__(self, *, target, daemon, name):
            threads.append((target, daemon, name))

        def start(self):
            return None

    monkeypatch.setattr(server.threading, "Thread", DeferredThread)
    with server._correction_transform_runs_lock:
        server._correction_transform_runs.clear()
    try:
        server._submit_correction_transform(service, command, queued)
        server._submit_correction_transform(service, command, queued)

        assert len(threads) == 1
        target, daemon, name = threads[0]
        assert daemon is True
        assert queued.job_id in name
        target()
        assert executed == [command]
    finally:
        with server._correction_transform_runs_lock:
            server._correction_transform_runs.clear()
