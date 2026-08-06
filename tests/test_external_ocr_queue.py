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
