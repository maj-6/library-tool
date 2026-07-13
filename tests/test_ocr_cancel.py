"""Cancellation behavior for background OCR batches."""
from __future__ import annotations

import server


def _job(job_id: str, pages: int = 3) -> dict:
    return {
        "id": job_id,
        "cfg": {},
        "pdf": "unused.pdf",
        "pages": [
            {"page": n, "service": "tesseract", "status": "queued"}
            for n in range(1, pages + 1)
        ],
        "width": 1400,
        "build_id": "cancelbook001",
        "target": "compiled.txt",
        "src_key": "primary",
        "done": 0,
        "errors": 0,
        "cancelled": 0,
        "cancel_requested": False,
        "status": "running",
    }


def test_cancel_endpoint_marks_running_job_as_cancelling(client):
    job_id = "cancelapi001"
    server._ocr_jobs[job_id] = _job(job_id)
    try:
        response = client.post(f"/api/ocr/job/{job_id}/cancel")
        data = response.get_json()
        assert response.status_code == 200
        assert data["ok"] is True
        assert data["job"]["status"] == "cancelling"
        assert data["job"]["cancel_requested"] is True
    finally:
        server._ocr_jobs.pop(job_id, None)


def test_cancel_stops_before_next_page_and_keeps_completed_page(monkeypatch):
    job_id = "cancelrun001"
    job = _job(job_id)
    server._ocr_jobs[job_id] = job
    monkeypatch.setattr(server, "_ocr_page_png", lambda *_: b"png")
    monkeypatch.setattr(server, "_ocr_save_page_words", lambda *_: None)
    monkeypatch.setattr(server, "_ocr_merge_page", lambda *_: None)

    def finish_one_then_cancel(_png, _cfg):
        job["cancel_requested"] = True
        return "finished first page"

    monkeypatch.setitem(server._OCR_SERVICES, "tesseract", finish_one_then_cancel)
    try:
        server._ocr_job_run(job_id)
        assert job["status"] == "cancelled"
        assert job["done"] == 1
        assert job["cancelled"] == 2
        assert [page["status"] for page in job["pages"]] == [
            "ok", "cancelled", "cancelled",
        ]
    finally:
        server._ocr_jobs.pop(job_id, None)


def test_cancelled_job_is_idempotent(client):
    job_id = "canceldone001"
    job = _job(job_id)
    job.update(status="cancelled", cancel_requested=True, cancelled=3)
    server._ocr_jobs[job_id] = job
    try:
        data = client.post(f"/api/ocr/job/{job_id}/cancel").get_json()
        assert data["ok"] is True
        assert data["job"]["status"] == "cancelled"
        assert data["job"]["cancelled"] == 3
    finally:
        server._ocr_jobs.pop(job_id, None)
