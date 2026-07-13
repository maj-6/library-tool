"""Regression coverage for failures in background OCR jobs."""
from __future__ import annotations

import logging

import server


def test_ocr_job_preserves_and_logs_page_error(monkeypatch, caplog):
    job_id = "ocrdiag001"
    server._ocr_jobs[job_id] = {
        "cfg": {},
        "pdf": "unused.pdf",
        "pages": [{"page": 7, "service": "tesseract", "status": "queued"}],
        "width": 1400,
        "build_id": "bookdiag001",
        "target": "compiled.txt",
        "src_key": "primary",
        "done": 0,
        "errors": 0,
        "status": "running",
    }
    monkeypatch.setattr(server, "_ocr_page_png", lambda *_: b"png")

    def fail(_png, _cfg):
        raise FileNotFoundError("tesseract executable was not found")

    monkeypatch.setitem(server._OCR_SERVICES, "tesseract", fail)
    try:
        with caplog.at_level(logging.ERROR, logger="whl"):
            server._ocr_job_run(job_id)
        job = server._ocr_jobs[job_id]
        assert job["status"] == "done (with errors)"
        assert job["errors"] == 1
        assert job["pages"][0]["status"] == (
            "error: FileNotFoundError: tesseract executable was not found")
        assert "book=bookdiag001 page=7 service=tesseract" in caplog.text
        assert "FileNotFoundError: tesseract executable was not found" in caplog.text
    finally:
        server._ocr_jobs.pop(job_id, None)
