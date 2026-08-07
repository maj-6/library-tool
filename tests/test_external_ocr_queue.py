"""The external-OCR folder queue: submit, worker protocol, strict collect.

Every test drives a throwaway data_root (tmp_path), playing both sides of
the protocol: this suite is the desktop app AND the external worker. The
worker's commit point is the DONE/ERROR marker — nothing before it may be
read, and nothing malformed after it may escape into the app.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import pathlib
import sys

import pytest
from PIL import Image

import external_ocr_queue as queue_mod


def _jpeg(width: int = 12, height: int = 9) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (250, 245, 235)).save(buf, "JPEG")
    return buf.getvalue()


def _valid_result(job_id: str) -> dict:
    return {
        "schema": queue_mod.RESULT_SCHEMA,
        "job_id": job_id,
        "engine": {"id": "my-ocr", "model": "my-ocr-v2.1"},
        "text": "THE HERBALL\nor Generall Historie of Plantes.",
        "regions": [
            {"id": "p0-r0", "type": "title", "text": "THE HERBALL",
             "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]],
             "confidence": 0.9},
            {"id": "p0-r1", "type": "text",
             "text": "or Generall Historie of Plantes.",
             "polygon": [[0.15, 0.25], [0.85, 0.25], [0.85, 0.3],
                         [0.15, 0.3]],
             "confidence": None},
        ],
    }


def _complete(job_dir, payload) -> None:
    """Play a well-behaved worker: full result first, DONE marker last."""
    (job_dir / queue_mod.RESULT_FILE).write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8")
    (job_dir / queue_mod.DONE_FILE).write_bytes(b"")


# --- submit ------------------------------------------------------------------


def test_submit_writes_job_files_and_instructions(tmp_path):
    image = _jpeg(12, 9)
    job_id = queue_mod.submit(
        image, media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "item-1", "artifact_id": "art-2",
                 "operation_id": 7})

    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    assert (job_dir / "input.jpg").read_bytes() == image  # verbatim bytes
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert job["schema"] == queue_mod.JOB_SCHEMA
    assert job["job_id"] == job_id
    assert job["submitted_at"].endswith("Z")
    assert job["image"] == {
        "file": "input.jpg",
        "sha256": hashlib.sha256(image).hexdigest(),
        "media_type": "image/jpeg",
        "width": 12,
        "height": 9,
    }
    assert job["context"] == {"item_id": "item-1", "artifact_id": "art-2",
                              "operation_id": 7}
    # the full expected-output contract rides inside every job.json
    expected = job["expected_result"]
    assert expected["schema"] == queue_mod.RESULT_SCHEMA
    assert expected["contract"]["job_id"] == job_id
    assert expected["limits"]["polygon_points"] == [3, 16]

    instructions = tmp_path / "ocr_external" / "INSTRUCTIONS.md"
    text = instructions.read_text(encoding="utf-8")
    assert text.splitlines()[0] == queue_mod.INSTRUCTIONS_HEADER
    assert queue_mod.RESULT_SCHEMA in text
    assert "DONE" in text and "ERROR" in text
    # no leftover staging junk once the job dir has appeared atomically
    staging = tmp_path / "ocr_external" / ".staging"
    assert not any(staging.iterdir())


def test_submit_rejects_undecodable_images_and_bad_context(tmp_path):
    with pytest.raises(ValueError):
        queue_mod.submit(b"not an image", media_type="image/jpeg",
                         data_root=tmp_path)
    with pytest.raises(ValueError):
        queue_mod.submit(_jpeg(), media_type="image/jpeg",
                         data_root=tmp_path,
                         context={"payload": {"nested": "dict"}})
    with pytest.raises(ValueError):
        queue_mod.submit(b"", media_type="image/jpeg", data_root=tmp_path)
    # nothing half-submitted leaked into pending/
    pending = tmp_path / "ocr_external" / "pending"
    assert not pending.exists() or not any(pending.iterdir())


def test_stale_instructions_are_refreshed_on_submit(tmp_path):
    root = tmp_path / "ocr_external"
    root.mkdir(parents=True)
    (root / "INSTRUCTIONS.md").write_text(
        "<!-- org.whl.external-ocr-instructions/0 -->\nold protocol\n",
        encoding="utf-8")
    queue_mod.submit(_jpeg(), media_type="image/jpeg", data_root=tmp_path)
    text = (root / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    assert text.splitlines()[0] == queue_mod.INSTRUCTIONS_HEADER
    assert "old protocol" not in text


# --- pending_jobs ------------------------------------------------------------


def test_pending_jobs_summarizes_the_queue(tmp_path):
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/png", data_root=tmp_path,
        context={"item_id": "item-9"})

    jobs = queue_mod.pending_jobs(data_root=tmp_path)
    assert jobs == [{
        "job_id": job_id,
        "submitted_at": jobs[0]["submitted_at"],
        "image_file": "input.png",
        "context": {"item_id": "item-9"},
        "ready": False,
    }]
    assert jobs[0]["submitted_at"].endswith("Z")

    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    _complete(job_dir, _valid_result(job_id))
    assert queue_mod.pending_jobs(data_root=tmp_path)[0]["ready"] is True


def test_pending_jobs_on_an_unsubmitted_root_is_empty(tmp_path):
    assert queue_mod.pending_jobs(data_root=tmp_path) == []
    assert queue_mod.collect(data_root=tmp_path) == []


# --- collect: the happy path -------------------------------------------------


def test_worker_roundtrip_collects_and_archives_the_job(tmp_path):
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "item-1", "operation_id": "op-5"})
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    _complete(job_dir, _valid_result(job_id))

    collected = queue_mod.collect(data_root=tmp_path)
    assert len(collected) == 1
    entry = collected[0]
    assert entry["job_id"] == job_id
    assert entry["status"] == "done"
    assert entry["error"] is None
    assert entry["context"] == {"item_id": "item-1", "operation_id": "op-5"}

    result = entry["result"]
    assert result["engine"] == "external"
    assert result["model"] == "my-ocr-v2.1"
    assert result["engine_version"] == queue_mod.ENGINE_VERSION
    assert result["text"] == "THE HERBALL\nor Generall Historie of Plantes."
    assert result["regions"] == [
        {"id": "p0-r0", "type": "title", "text": "THE HERBALL",
         "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.2], [0.1, 0.2]],
         "confidence": 0.9},
        {"id": "p0-r1", "type": "text",
         "text": "or Generall Historie of Plantes.",
         "polygon": [[0.15, 0.25], [0.85, 0.25], [0.85, 0.3], [0.15, 0.3]],
         "confidence": None},
    ]
    assert result["raw_meta"]["declared_engine_id"] == "my-ocr"

    # the whole job directory moved to done/, input preserved
    done_dir = tmp_path / "ocr_external" / "done" / job_id
    assert (done_dir / "input.jpg").exists()
    assert (done_dir / queue_mod.RESULT_FILE).exists()
    assert not job_dir.exists()
    # idempotent: a second collect finds nothing
    assert queue_mod.collect(data_root=tmp_path) == []
    assert queue_mod.pending_jobs(data_root=tmp_path) == []


# --- collect: failure paths --------------------------------------------------


def test_error_file_moves_the_job_to_failed(tmp_path):
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "item-3"})
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    (job_dir / queue_mod.ERROR_FILE).write_text(
        "model refused: page is blank", encoding="utf-8")

    collected = queue_mod.collect(data_root=tmp_path)
    assert len(collected) == 1
    assert collected[0]["status"] == "failed"
    assert collected[0]["error"] == "model refused: page is blank"
    assert collected[0]["result"] is None
    assert collected[0]["context"] == {"item_id": "item-3"}

    failed_dir = tmp_path / "ocr_external" / "failed" / job_id
    assert (failed_dir / queue_mod.ERROR_FILE).exists()  # message preserved
    assert not job_dir.exists()


def test_garbage_result_fails_with_a_diagnostic_not_an_exception(tmp_path):
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    _complete(job_dir, "this is not json {{{")

    collected = queue_mod.collect(data_root=tmp_path)  # must not raise
    assert collected[0]["status"] == "failed"
    assert "not valid JSON" in collected[0]["error"]

    failed_dir = tmp_path / "ocr_external" / "failed" / job_id
    diagnostic = (failed_dir / queue_mod.DIAGNOSTIC_FILE).read_text(
        encoding="utf-8")
    assert "not valid JSON" in diagnostic


def _huge_int_literal_result() -> str:
    """A result json.loads rejects with a BARE ValueError.

    CPython caps int/str conversion at 4300 digits; over that, int() raises
    a plain ValueError that json does not wrap in a JSONDecodeError.
    """
    return '{"schema": "x", "job_id": "y", "text": ' + "9" * 5000 + "}"


def _deeply_nested_result() -> str:
    """A result json.loads rejects with a RecursionError, not a ValueError.

    The depth is derived from the live recursion limit rather than
    hard-coded, so this still trips the scanner in an interpreter (or a
    conftest) that has raised the limit. Newer CPythons parse this without
    recursing at all and reject it by shape instead, which is why the
    caller asserts the wedge property rather than one error string.
    """
    depth = sys.getrecursionlimit() * 3
    return "[" * depth + "]" * depth


@pytest.mark.parametrize("poison", [
    _huge_int_literal_result,
    _deeply_nested_result,
])
def test_a_result_that_defeats_json_loads_cannot_wedge_the_queue(
        tmp_path, poison):
    """Neither json.loads failure mode may escape collect().

    Both are invisible to `except (UnicodeDecodeError, JSONDecodeError)` —
    one raises a bare ValueError, the other a RecursionError (a
    RuntimeError). Escaping collect() would be far worse than losing one
    job: the queue is ordered, so a single hostile or corrupt directory
    would strand every job behind it in pending/ forever. The healthy job
    submitted alongside must therefore come back from the SAME call,
    whichever way the UUID-named directories happen to sort.
    """
    poisoned_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "poisoned"})
    healthy_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "healthy"})
    pending = tmp_path / "ocr_external" / "pending"
    _complete(pending / poisoned_id, poison())
    _complete(pending / healthy_id, _valid_result(healthy_id))

    by_id = {e["job_id"]: e
             for e in queue_mod.collect(data_root=tmp_path)}  # must not raise

    assert by_id[poisoned_id]["status"] == "failed"
    # Which diagnostic appears is the interpreter's business — 3.11 raises
    # from json.loads, later versions parse it and reject the shape. What
    # must hold on every version is that the job is quarantined with SOME
    # reason and takes nothing down with it.
    assert by_id[poisoned_id]["error"]
    assert by_id[healthy_id]["status"] == "done"  # neighbour not stranded
    diagnostic = (pending.parent / "failed" / poisoned_id
                  / queue_mod.DIAGNOSTIC_FILE).read_text(encoding="utf-8")
    assert diagnostic.strip()
    assert not any(pending.iterdir())  # nothing wedged, nothing retried


@pytest.mark.parametrize("stat_lies", [False, True])
def test_an_oversized_result_is_rejected_by_the_read_not_by_stat(
        tmp_path, monkeypatch, stat_lies):
    """The cap has to bind the bytes actually taken in.

    A size from stat() is a claim about whatever the path named at the
    moment it ran: it follows symlinks, reports nothing meaningful for a
    FIFO or a device node, and can go stale before the read. So the second
    case here makes stat() under-report — the size guard is only real if
    the file is still rejected when the number it was given is a lie.
    """
    monkeypatch.setattr(queue_mod, "MAX_RESULT_FILE_BYTES", 64)
    if stat_lies:
        real_stat = pathlib.Path.stat

        def _understating_stat(self, *args, **kwargs):
            info = real_stat(self, *args, **kwargs)
            if self.name != queue_mod.RESULT_FILE:
                return info
            fields = list(info)
            fields[6] = 0  # st_size, as a special file would report it
            return os.stat_result(fields)

        monkeypatch.setattr(pathlib.Path, "stat", _understating_stat)

    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    _complete(job_dir, _valid_result(job_id))  # valid JSON, far over 64 bytes

    collected = queue_mod.collect(data_root=tmp_path)
    assert collected[0]["status"] == "failed"
    assert "64 byte limit" in collected[0]["error"]
    assert (tmp_path / "ocr_external" / "failed" / job_id
            / queue_mod.DIAGNOSTIC_FILE).exists()


def test_a_non_regular_result_file_is_rejected(tmp_path):
    """result.json must be a regular file.

    Only a regular file has a size the cap can mean anything about, and
    only a regular file cannot block the collector on open. The exact
    rejection is platform-specific (POSIX opens a directory and fstat
    refuses it; Windows refuses the open) — what matters is that the job
    is quarantined instead of crashing collect().
    """
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    (job_dir / queue_mod.RESULT_FILE).mkdir()  # not a file at all
    (job_dir / queue_mod.DONE_FILE).write_bytes(b"")

    collected = queue_mod.collect(data_root=tmp_path)  # must not raise
    assert collected[0]["status"] == "failed"
    assert queue_mod.RESULT_FILE in collected[0]["error"]
    assert (tmp_path / "ocr_external" / "failed" / job_id).exists()


def test_context_values_edited_on_disk_are_clipped_like_submitted_ones(
        tmp_path):
    """The submit-side context bound must also hold on the way back out.

    job.json sits in a directory the external side owns for the duration
    of the job, so what collect() echoes back is not necessarily what the
    caller submitted. Without the same cap on read, context becomes
    exactly the arbitrary-payload vehicle _validated_context refuses to be.
    """
    over_long = "x" * (queue_mod.MAX_CONTEXT_VALUE_CHARS + 1)
    with pytest.raises(ValueError):  # the submit-side bound, for contrast
        queue_mod.submit(_jpeg(), media_type="image/jpeg",
                         data_root=tmp_path, context={"note": over_long})

    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"note": "short"})
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    job_file = job_dir / queue_mod.JOB_FILE
    payload = json.loads(job_file.read_text(encoding="utf-8"))
    payload["context"]["note"] = "x" * 5000        # edited on disk
    payload["context"]["blob"] = {"nested": "dict"}  # non-scalar: dropped
    job_file.write_text(json.dumps(payload), encoding="utf-8")

    clipped = {"note": "x" * queue_mod.MAX_CONTEXT_VALUE_CHARS}
    assert queue_mod.pending_jobs(data_root=tmp_path)[0]["context"] == clipped

    _complete(job_dir, _valid_result(job_id))
    assert queue_mod.collect(data_root=tmp_path)[0]["context"] == clipped


@pytest.mark.parametrize("mutate, expected_problem", [
    (lambda r: r.update(schema="org.whl.external-ocr-result/999"),
     "schema"),
    (lambda r: r.update(job_id="someone-elses-job"), "job_id"),
    (lambda r: r.update(engine={}), "engine.id"),
    (lambda r: r.update(text=None), "text"),
    (lambda r: r["regions"][0].update(polygon=[[0.1, 0.1], [0.9, 0.1]]),
     "polygon"),
    (lambda r: r["regions"][0].update(
        polygon=[[0.1, 0.1], [1.5, 0.1], [0.9, 0.2]]), "polygon"),
    (lambda r: r["regions"][0].update(
        polygon=[[0.1, float("nan")], [0.9, 0.1], [0.9, 0.2]]), "polygon"),
    (lambda r: r["regions"][0].update(confidence=1.7), "confidence"),
    (lambda r: r["regions"][1].update(id="p0-r0"), "duplicated"),
])
def test_strict_validation_rejects_bad_results(tmp_path, mutate,
                                               expected_problem):
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    payload = _valid_result(job_id)
    mutate(payload)
    # json.dumps chokes on nan — write it by hand for that case
    _complete(job_dir, json.dumps(payload, allow_nan=True))

    collected = queue_mod.collect(data_root=tmp_path)
    assert collected[0]["status"] == "failed"
    assert expected_problem in collected[0]["error"]
    assert (tmp_path / "ocr_external" / "failed" / job_id).exists()


def test_result_without_done_marker_is_untouched(tmp_path):
    """A half-finished worker owns the job until DONE/ERROR appears."""
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    (job_dir / queue_mod.RESULT_FILE).write_text(
        json.dumps(_valid_result(job_id)), encoding="utf-8")
    # no DONE file

    assert queue_mod.collect(data_root=tmp_path) == []
    assert job_dir.exists()
    assert (job_dir / queue_mod.RESULT_FILE).exists()
    assert queue_mod.pending_jobs(data_root=tmp_path)[0]["ready"] is False


def test_region_count_over_the_cap_is_rejected(tmp_path):
    job_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)
    job_dir = tmp_path / "ocr_external" / "pending" / job_id
    payload = _valid_result(job_id)
    template = payload["regions"][0]
    payload["regions"] = [
        dict(template, id=f"p0-r{i}")
        for i in range(queue_mod.MAX_REGIONS + 1)
    ]
    _complete(job_dir, payload)

    collected = queue_mod.collect(data_root=tmp_path)
    assert collected[0]["status"] == "failed"
    assert str(queue_mod.MAX_REGIONS) in collected[0]["error"]


def test_multiple_jobs_collect_independently(tmp_path):
    """One bad job must never block its neighbours."""
    good_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "good"})
    bad_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path,
        context={"item_id": "bad"})
    waiting_id = queue_mod.submit(
        _jpeg(), media_type="image/jpeg", data_root=tmp_path)

    pending = tmp_path / "ocr_external" / "pending"
    _complete(pending / good_id, _valid_result(good_id))
    _complete(pending / bad_id, "garbage")

    by_id = {e["job_id"]: e for e in queue_mod.collect(data_root=tmp_path)}
    assert by_id[good_id]["status"] == "done"
    assert by_id[bad_id]["status"] == "failed"
    assert waiting_id not in by_id
    assert (pending / waiting_id).exists()
