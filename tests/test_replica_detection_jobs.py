"""Versioned Replica region-detection jobs and their legacy OCR worker adapter."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import server


def _install_mistral_secret(monkeypatch, value="configured"):
    monkeypatch.setattr(
        server, "_secret_is_configured",
        lambda key: key == "mistralKey" and bool(value),
    )

    @contextmanager
    def lease(key):
        assert key == "mistralKey"
        if not value:
            raise RuntimeError("not configured")
        yield value

    monkeypatch.setattr(server, "_lease_secret", lease)


def _seed_build(data_root, bid: str) -> None:
    pdf = data_root / "downloads" / f"{bid}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-replica-detection-test")
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds[bid] = {
        "id": bid,
        "title": f"Detection {bid}",
        "pdf_file": str(pdf),
    }
    server.lib.save_json(server.BUILDS_PATH, builds)


def _revision(client, bid: str, page: int = 1) -> str:
    response = client.get(
        f"/api/builds/{bid}/ocr-regions?src=primary&page={page}"
    )
    assert response.status_code == 200
    return response.get_json()["revision"]


def _region(text: str) -> dict:
    return {
        "role": "body",
        "order": 0,
        "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        "text": text,
    }


def _install_inline_worker(monkeypatch) -> list[dict]:
    started: list[dict] = []

    def start(job: dict, source_revision: int,
              record_source: bool = False) -> bool:
        started.append(job)
        if record_source:
            server._ocr_set_source(
                job["build_id"], job["target"], job["src_key"]
            )
        with server._ocr_jobs_lock:
            server._ocr_jobs[job["id"]] = job
        server._job_track_item_guarded(
            job,
            str(job.get("kind") or "ocr"),
            job["build_id"],
        )
        server._ocr_job_run(job["id"])
        return True

    monkeypatch.setattr(server, "_ocr_job_start_guarded", start)
    monkeypatch.setattr(server, "_ocr_page_png", lambda *_args: b"png")
    return started


def _cleanup(jobs: list[dict]) -> None:
    for job in jobs:
        job_id = str(job.get("id") or "")
        server._ocr_jobs.pop(job_id, None)
        with server._jobs_lock:
            server._jobs.pop(job_id, None)
            server._jobs_events.pop(job_id, None)


def _start(client, bid: str, revision: str, page: int = 1):
    return client.post(
        f"/api/v1/items/{bid}/replica/region-detection-jobs",
        headers={"If-Match": f'"{revision}"'},
        json={
            "source_id": "primary",
            "page": page,
            "provider": "automatic",
            "idempotency_key": f"detect-{bid}-{page}",
        },
    )


def test_detection_job_resolves_provider_server_side_and_publishes_output(
    client, data_root, monkeypatch,
):
    bid = "detect-job-success"
    _seed_build(data_root, bid)
    started = _install_inline_worker(monkeypatch)
    _install_mistral_secret(monkeypatch, "server-only-secret")
    monkeypatch.setattr(
        server, "_client_settings", lambda: {"ocrImageWidth": 1777})
    seen: dict = {}

    def detect(_png: bytes, cfg: dict) -> dict:
        seen.update(cfg)
        return {
            "text": "Machine text",
            "regions": [_region("Machine text")],
            "dims": {"w": 1000, "h": 1600, "dpi": 200},
        }

    monkeypatch.setitem(server._OCR_SERVICES, "mistral", detect)
    try:
        response = _start(client, bid, _revision(client, bid))
        assert response.status_code == 200
        body = response.get_json()
        job = body["job"]
        assert body["provider"] == "mistral"
        assert body["already"] is False
        assert job["kind"] == "replica.detect-regions"
        assert job["state"] == "done"
        assert job["subject"] == {
            "item_id": bid, "source_id": "primary", "page": 1,
        }
        assert job["progress"] == {
            "completed": 1, "total": 1,
            "unit": "page", "phase": "detecting-regions",
        }
        assert job["outputs"][0]["kind"] == "replica.region-page"
        assert job["outputs"][0]["ref"].endswith(
            f"/{bid}/replica/primary/pages/1"
        )
        assert seen["mistral_key"] == "server-only-secret"
        assert started[0]["width"] == 1777
        assert "server-only-secret" not in json.dumps(body)

        queried = client.get(f"/api/v1/jobs/{job['id']}").get_json()["job"]
        assert queried == job
        page = client.get(
            f"/api/builds/{bid}/ocr-regions?src=primary&page=1"
        ).get_json()
        assert page["found"] is True
        assert page["items"][0]["text"] == "Machine text"
    finally:
        _cleanup(started)


def test_detection_on_human_page_creates_proposal_without_replacing_it(
    client, data_root, monkeypatch,
):
    bid = "detect-job-proposal"
    _seed_build(data_root, bid)
    initial = _revision(client, bid)
    saved = client.put(
        f"/api/builds/{bid}/ocr-regions",
        headers={"If-Match": f'"{initial}"'},
        json={"src": "primary", "page": 1, "items": [_region("Human text")]},
    ).get_json()
    started = _install_inline_worker(monkeypatch)
    _install_mistral_secret(monkeypatch)
    monkeypatch.setitem(server._OCR_SERVICES, "mistral", lambda *_args: {
        "text": "New machine text",
        "regions": [_region("New machine text")],
        "dims": {"w": 900, "h": 1400},
    })
    try:
        response = _start(client, bid, saved["revision"])
        assert response.status_code == 200
        job = response.get_json()["job"]
        assert job["state"] == "done"
        assert job["outputs"][0]["kind"] == "replica.region-proposal"

        page = client.get(
            f"/api/builds/{bid}/ocr-regions?src=primary&page=1"
        ).get_json()
        assert page["items"][0]["text"] == "Human text"
        assert page["proposal"]["items"][0]["text"] == "New machine text"
    finally:
        _cleanup(started)


def test_detection_job_reports_provider_failure_as_failed(
    client, data_root, monkeypatch,
):
    bid = "detect-job-failure"
    _seed_build(data_root, bid)
    started = _install_inline_worker(monkeypatch)
    _install_mistral_secret(monkeypatch)

    def fail(*_args):
        raise RuntimeError("provider offline")

    monkeypatch.setitem(server._OCR_SERVICES, "mistral", fail)
    try:
        response = _start(client, bid, _revision(client, bid))
        assert response.status_code == 200
        job = response.get_json()["job"]
        assert job["state"] == "failed"
        assert job["error"]["code"] == "region_detection_failed"
        assert "provider offline" in job["error"]["message"]
        assert job["outputs"] == []
    finally:
        _cleanup(started)


def test_detection_start_requires_current_revision_and_configured_provider(
    client, data_root, monkeypatch,
):
    bid = "detect-job-preconditions"
    _seed_build(data_root, bid)
    url = f"/api/v1/items/{bid}/replica/region-detection-jobs"

    missing = client.post(url, json={"source_id": "primary", "page": 1})
    assert missing.status_code == 428
    assert missing.get_json()["code"] == "region_revision_required"

    stale = client.post(
        url, headers={"If-Match": '"rr-stale"'},
        json={"source_id": "primary", "page": 1},
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "region_revision_conflict"

    _install_mistral_secret(monkeypatch, "")
    unconfigured = _start(client, bid, _revision(client, bid))
    assert unconfigured.status_code == 400
    assert unconfigured.get_json()["code"] == \
        "region_detection_provider_not_configured"


def test_duplicate_detection_joins_the_active_page_job(
    client, data_root, monkeypatch,
):
    bid = "detect-job-dedupe"
    _seed_build(data_root, bid)
    _install_mistral_secret(monkeypatch)
    started: list[dict] = []

    def defer(job: dict, _source_revision: int,
              record_source: bool = False) -> bool:
        started.append(job)
        if record_source:
            server._ocr_set_source(job["build_id"], job["target"], job["src_key"])
        server._ocr_jobs[job["id"]] = job
        server._job_track(job, str(job["kind"]), label=bid)
        return True

    monkeypatch.setattr(server, "_ocr_job_start_guarded", defer)
    revision = _revision(client, bid)
    try:
        first = _start(client, bid, revision).get_json()
        second = _start(client, bid, revision).get_json()
        assert first["job"]["state"] == "running"
        assert second["already"] is True
        assert second["job"]["id"] == first["job"]["id"]
        assert len(started) == 1
    finally:
        for job in started:
            server._job_transition(job, "cancelled")
        _cleanup(started)


def test_detection_job_honors_generic_cooperative_cancellation(
    client, data_root, monkeypatch,
):
    bid = "detect-job-cancel"
    _seed_build(data_root, bid)
    _install_mistral_secret(monkeypatch)
    started: list[dict] = []

    def defer(job: dict, _source_revision: int,
              record_source: bool = False) -> bool:
        started.append(job)
        if record_source:
            server._ocr_set_source(job["build_id"], job["target"], job["src_key"])
        server._ocr_jobs[job["id"]] = job
        server._job_track(job, str(job["kind"]), label=bid)
        return True

    monkeypatch.setattr(server, "_ocr_job_start_guarded", defer)
    try:
        job = _start(client, bid, _revision(client, bid)).get_json()["job"]
        requested = client.post(
            f"/api/v1/jobs/{job['id']}/cancel"
        ).get_json()["job"]
        assert requested["state"] == "cancelling"

        # The existing OCR loop notices the JobManager event before rasterizing
        # the page and converts the semantic Replica job to a terminal cancel.
        server._ocr_job_run(job["id"])
        terminal = client.get(
            f"/api/v1/jobs/{job['id']}"
        ).get_json()["job"]
        assert terminal["state"] == "cancelled"
        assert terminal["progress"]["completed"] == 0
    finally:
        _cleanup(started)


def test_guarded_worker_registration_preserves_semantic_job_kind(
        tmp_path, monkeypatch):
    tracked: list[str] = []
    started: list[tuple] = []
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({
        "missing-label-is-fine": {
            "id": "missing-label-is-fine",
            "title": "Semantic detection",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    job = {
        "id": "semantic-kind",
        "kind": "replica.detect-regions",
        "build_id": "missing-label-is-fine",
    }

    monkeypatch.setattr(
        server, "_job_track",
        lambda _job, kind, **_kwargs: tracked.append(kind) or threading.Event(),
    )

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(server.threading, "Thread", DeferredThread)
    try:
        assert server._ocr_job_start_guarded(job, 0) is True
        assert tracked == ["replica.detect-regions"]
        assert job["subject"]["item_id"] == "missing-label-is-fine"
        assert started and started[0][1] == ("semantic-kind",)
    finally:
        with server._ocr_jobs_lock:
            server._ocr_jobs.pop("semantic-kind", None)
