"""Versioned Replica region-detection jobs and their legacy OCR worker adapter."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest

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


def _revision(client, bid: str, page: int = 1,
              source_id: str = "primary") -> str:
    response = client.get(
        f"/api/builds/{bid}/ocr-regions?src={source_id}&page={page}"
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


def _start(client, bid: str, revision: str, page: int = 1,
           *, source_id: str = "primary", operation_id: str | None = None):
    return client.post(
        f"/api/v1/items/{bid}/replica/region-detection-jobs",
        headers={"If-Match": f'"{revision}"'},
        json={
            "source_id": source_id,
            "page": page,
            "provider": "automatic",
            "idempotency_key": (operation_id or
                                f"detect-{bid}-{source_id}-{page}"),
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


@pytest.mark.parametrize("action", ("apply", "dismiss"))
def test_protected_detection_stages_figures_until_proposal_decision(
    client, data_root, monkeypatch, action,
):
    bid = f"detect-protected-figures-{action}"
    _seed_build(data_root, bid)
    saved = client.put(
        f"/api/builds/{bid}/ocr-regions",
        headers={"If-Match": f'"{_revision(client, bid)}"'},
        json={"src": "primary", "page": 1,
              "items": [_region("Human text")]},
    ).get_json()
    started = _install_inline_worker(monkeypatch)
    _install_mistral_secret(monkeypatch)
    monkeypatch.setitem(server._OCR_SERVICES, "mistral", lambda *_args: {
        "text": "![plate](plate.jpeg)\n\nMachine text",
        "images": [{
            "id": "plate.jpeg",
            "data": b"proposal-figure",
            "bbox": {"x": 0.2, "y": 0.1, "w": 0.5, "h": 0.4},
        }],
        "regions": [{
            **_region("![plate](plate.jpeg)"),
            "role": "figure",
        }, _region("Machine text")],
        "dims": {"w": 900, "h": 1400},
    })
    try:
        detected = _start(client, bid, saved["revision"])
        assert detected.status_code == 200
        page = client.get(
            f"/api/builds/{bid}/ocr-regions?src=primary&page=1"
        ).get_json()
        proposal = page["proposal"]
        assert proposal["base_revision"] == page["revision"]
        names = set(proposal["staged_figures"])
        assert len(names) == 1
        name = next(iter(names))
        assert name.startswith("proposal-primary-")
        assert proposal["proposal_id"][4:] in name
        image_path = (server._entry_dir(bid) / "ocr" / "images" / name)
        assert image_path.read_bytes() == b"proposal-figure"
        layout = server.lib.load_json(
            server._entry_dir(bid) / "ocr" / "layout.json", {})
        assert name not in (layout.get("images") or {})
        assert page["items"][0]["text"] == "Human text"

        decided = client.post(
            f"/api/builds/{bid}/ocr-region-proposals",
            headers={
                "If-Match": f'"{page["revision"]}"',
                "If-Proposal-Match": f'"{proposal["revision"]}"',
            },
            json={"src": "primary", "page": 1, "action": action},
        )
        assert decided.status_code == 200
        layout = server.lib.load_json(
            server._entry_dir(bid) / "ocr" / "layout.json", {})
        assert "region_proposals" not in layout
        if action == "apply":
            assert name in layout["images"]
            assert "proposal_id" not in layout["images"][name]
            assert image_path.read_bytes() == b"proposal-figure"
            assert name in decided.get_json()["items"][0]["text"]
        else:
            assert name not in (layout.get("images") or {})
            assert not image_path.exists()
            assert decided.get_json()["items"][0]["text"] == "Human text"
    finally:
        _cleanup(started)


def test_detection_replay_is_durable_and_does_not_run_provider_twice(
    client, data_root, monkeypatch,
):
    bid = "detect-durable-replay"
    _seed_build(data_root, bid)
    started = _install_inline_worker(monkeypatch)
    _install_mistral_secret(monkeypatch)
    calls = 0

    def detect(*_args):
        nonlocal calls
        calls += 1
        return {
            "text": "Machine text",
            "regions": [_region("Machine text")],
            "dims": {"w": 900, "h": 1400},
        }

    monkeypatch.setitem(server._OCR_SERVICES, "mistral", detect)
    revision = _revision(client, bid)
    operation_id = "detect:durable:replay"
    try:
        first = _start(
            client, bid, revision, operation_id=operation_id).get_json()
        assert first["already"] is False
        assert first["job"]["state"] == "done"
        assert calls == 1

        # Reopen the process-lifetime engine. The completed job can be served
        # from its persisted command receipt even though detection changed the
        # page revision carried by the original command.
        server._close_engine_session()
        server._ensure_engine_session()
        replay = _start(
            client, bid, revision, operation_id=operation_id)
        assert replay.status_code == 200
        body = replay.get_json()
        assert body["already"] is True
        assert body["job"]["id"] == first["job"]["id"]
        assert body["receipt"]["terminal"] is True
        assert calls == 1

        changed = _start(
            client, bid, _revision(client, bid), page=2,
            operation_id=operation_id)
        assert changed.status_code == 409
        assert changed.get_json()["code"] == "operation_id_conflict"
        assert calls == 1
    finally:
        _cleanup(started)


def test_detection_requires_an_exact_portable_idempotency_key(
    client, data_root, monkeypatch,
):
    bid = "detect-idempotency-validation"
    _seed_build(data_root, bid)
    _install_mistral_secret(monkeypatch)
    revision = _revision(client, bid)
    url = f"/api/v1/items/{bid}/replica/region-detection-jobs"
    base = {"source_id": "primary", "page": 1,
            "provider": "automatic"}

    missing = client.post(
        url, headers={"If-Match": f'"{revision}"'}, json=base)
    assert missing.status_code == 428
    assert missing.get_json()["code"] == "idempotency_key_required"
    for unsafe in (" unsafe", "unsafe/key", "x" * 129):
        response = client.post(
            url, headers={"If-Match": f'"{revision}"'},
            json={**base, "idempotency_key": unsafe})
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_idempotency_key"


def test_protected_proposal_figures_are_source_scoped_and_survive_restart_gc(
    client, data_root, monkeypatch,
):
    bid = "detect-cross-source-proposals"
    _seed_build(data_root, bid)
    secondary = data_root / "downloads" / f"{bid}-scan2.pdf"
    secondary.write_bytes(b"%PDF-secondary-replica-detection-test")
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds[bid]["pdf_sources"] = [{"id": "scan2", "path": str(secondary)}]
    server.lib.save_json(server.BUILDS_PATH, builds)
    for source_id in ("primary", "scan2"):
        saved = client.put(
            f"/api/builds/{bid}/ocr-regions",
            headers={
                "If-Match": f'"{_revision(client, bid, source_id=source_id)}"'
            },
            json={"src": source_id, "page": 1,
                  "items": [_region(f"Human {source_id}")]},
        )
        assert saved.status_code == 200

    started = _install_inline_worker(monkeypatch)
    _install_mistral_secret(monkeypatch)
    monkeypatch.setitem(server._OCR_SERVICES, "mistral", lambda *_args: {
        "text": "![plate](same.jpeg)",
        "images": [{
            "id": "same.jpeg", "data": b"same-crop",
            "bbox": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4},
        }],
        "regions": [{**_region("![plate](same.jpeg)"), "role": "figure"}],
        "dims": {"w": 900, "h": 1400},
    })
    try:
        names = {}
        for source_id in ("primary", "scan2"):
            response = _start(
                client, bid,
                _revision(client, bid, source_id=source_id),
                source_id=source_id,
            )
            assert response.status_code == 200
            page = client.get(
                f"/api/builds/{bid}/ocr-regions?src={source_id}&page=1"
            ).get_json()
            name = next(iter(page["proposal"]["staged_figures"]))
            names[source_id] = name
            info = page["proposal"]["staged_figures"][name]
            assert info["src_key"] == source_id

        assert names["primary"] != names["scan2"]
        image_dir = server._entry_dir(bid) / "ocr" / "images"
        for name in names.values():
            assert (image_dir / name).read_bytes() == b"same-crop"
        orphan = image_dir / "proposal-unreferenced.jpeg"
        orphan.write_bytes(b"orphan")

        server._close_engine_session()
        server._ensure_engine_session()

        assert not orphan.exists()
        for source_id, name in names.items():
            assert (image_dir / name).read_bytes() == b"same-crop"
            page = client.get(
                f"/api/builds/{bid}/ocr-regions?src={source_id}&page=1"
            ).get_json()
            assert name in page["proposal"]["staged_figures"]
        layout = server.lib.load_json(
            server._entry_dir(bid) / "ocr" / "layout.json", {})
        assert not layout.get("images")
    finally:
        _cleanup(started)


def test_staged_figure_gc_runs_once_per_open_engine_session(
    data_root, monkeypatch,
):
    bid = "detect-startup-gc-once"
    _seed_build(data_root, bid)
    calls = []
    monkeypatch.setattr(
        server, "_ocr_cleanup_staged_figure_orphans", calls.append)

    server._close_engine_session()
    server._ensure_engine_session()
    first_count = len(calls)
    assert bid in calls
    server._ensure_engine_session()
    server._ensure_engine_session()
    assert len(calls) == first_count


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
        json={"source_id": "primary", "page": 1,
              "idempotency_key": "detect-stale-revision"},
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


def test_distinct_command_can_observe_identical_active_detection(
    client, data_root, monkeypatch,
):
    bid = "detect-job-observer-join"
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
        first = _start(
            client, bid, revision, operation_id="detect:first-observer"
        ).get_json()
        second = _start(
            client, bid, revision, operation_id="detect:second-observer"
        )
        assert second.status_code == 200
        observed = second.get_json()
        assert observed["already"] is True
        assert observed["job"]["id"] == first["job"]["id"]
        assert "receipt" not in observed
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
