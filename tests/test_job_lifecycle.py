"""Unified background-job lifecycle: registry, cancellation, persistence.

Covers issue #121: one lifecycle (queued/running/cancelling/cancelled/
failed/done + interrupted after a restart) shared by OCR, Analyze, and
publish jobs, snapshotted credential-free to DATA_ROOT/output/jobs.json,
pruned to a bounded history, and cancellable through POST
/api/jobs/<id>/cancel.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import server


ROOT = Path(__file__).parents[1]
APP = (ROOT / "tools" / "whl_explorer" / "static" / "app.js").read_text(
    encoding="utf-8")
STYLE = (ROOT / "tools" / "whl_explorer" / "static" / "style.css").read_text(
    encoding="utf-8")
TEMPLATE = (ROOT / "tools" / "whl_explorer" / "templates" / "index.html").read_text(
    encoding="utf-8")
DESKTOP_MAIN = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _ready_build(client, title: str) -> dict:
    response = client.post("/api/builds", json={"build": {
        "title": title, "status": "ready",
    }})
    assert response.status_code == 200
    return response.get_json()["build"]


def _finish(job_id: str) -> None:
    """Drop a test job out of the active set so it never trips other tests."""
    job = server._jobs.get(job_id)
    if job is not None and job.get("state") in server._JOB_ACTIVE:
        server._job_transition(job, "done")


# --- lifecycle unit behavior ---------------------------------------------------

def test_track_transition_and_snapshot():
    job = {"build_id": "lifecycb0001", "total": 4}
    ev = server._job_track(job, "summarize", label="A Herbal")
    try:
        assert not ev.is_set()
        assert job["state"] == "running" and job["status"] == "running"
        assert job["created_at"] and job["finished_at"] == ""
        assert server._jobs[job["id"]] is job

        job["errors"] = 1
        server._job_transition(job, "done (with errors)")
        assert job["state"] == "done"          # legacy string maps to canonical
        assert job["finished_at"]

        snap = json.loads(server.JOBS_PATH.read_text(encoding="utf-8"))
        assert snap[job["id"]]["state"] == "done"
        assert snap[job["id"]]["status"] == "done (with errors)"
        assert snap[job["id"]]["label"] == "A Herbal"
    finally:
        _finish(job["id"])


def test_item_job_registration_holds_lifecycle_gate_and_normalizes_subject(
        tmp_path, monkeypatch):
    """Deletion cannot enter its outer gate before registration is visible."""
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({
        "gate-book": {"id": "gate-book", "title": "Gate Herbal"},
    }), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)

    entered_track = threading.Event()
    release_track = threading.Event()
    contender_attempted = threading.Event()
    contender_acquired = threading.Event()
    worker_errors: list[BaseException] = []
    seen: dict = {}

    def blocking_track(job, kind, label=""):
        seen.update(job=job, kind=kind, label=label)
        entered_track.set()
        assert release_track.wait(timeout=2)
        return threading.Event()

    monkeypatch.setattr(server, "_job_track", blocking_track)
    job = {"id": "gate-job", "subject": {"source_id": "primary"}}

    def register():
        try:
            server._job_track_item_guarded(job, "ocr", "gate-book")
        except BaseException as exc:  # surfaced on the test thread below
            worker_errors.append(exc)

    def contend_for_lifecycle_gate():
        contender_attempted.set()
        with server._page_structure_lock:
            contender_acquired.set()

    worker = threading.Thread(target=register)
    worker.start()
    assert entered_track.wait(timeout=2)
    contender = threading.Thread(target=contend_for_lifecycle_gate)
    contender.start()
    assert contender_attempted.wait(timeout=2)
    assert not contender_acquired.wait(timeout=0.1)

    release_track.set()
    worker.join(timeout=2)
    contender.join(timeout=2)

    assert not worker.is_alive() and not contender.is_alive()
    assert worker_errors == []
    assert contender_acquired.is_set()
    assert job["build_id"] == "gate-book"
    assert job["subject"] == {
        "item_id": "gate-book", "source_id": "primary",
    }
    assert seen["kind"] == "ocr"
    assert seen["label"] == "Gate Herbal"


def test_item_job_waiting_behind_lifecycle_delete_is_not_registered(
        tmp_path, monkeypatch):
    """A start queued behind deletion must re-read the catalogue and fail."""
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({
        "deleted-book": {"id": "deleted-book", "title": "Gone"},
    }), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    attempted = threading.Event()
    caught: list[BaseException] = []
    job = {"id": "late-job"}

    def late_start():
        attempted.set()
        try:
            server._job_track_item_guarded(job, "ocr", "deleted-book")
        except BaseException as exc:
            caught.append(exc)

    with server._page_structure_lock:
        worker = threading.Thread(target=late_start)
        worker.start()
        assert attempted.wait(timeout=2)
        with server._builds_lock:
            server.lib.save_json(builds_path, {})

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], server._ItemJobStartRejected)
    assert "late-job" not in server._jobs


def test_error_status_maps_to_failed_state():
    job = {"build_id": "lifecycb0002"}
    server._job_track(job, "about")
    server._job_transition(job, "error", error="HTTP 500: boom")
    assert job["state"] == "failed"
    snap = json.loads(server.JOBS_PATH.read_text(encoding="utf-8"))
    assert snap[job["id"]]["error"] == "HTTP 500: boom"


def test_prune_keeps_newest_finished_entries():
    running = {"build_id": "prunerun0001"}
    server._job_track(running, "ocr")
    ids = []
    try:
        for i in range(server._JOBS_KEEP + 5):
            job = {"build_id": f"prune{i:04d}"}
            server._job_track(job, "summarize")
            server._job_transition(job, "done")
            # distinct, ordered sort keys (the wall clock only has seconds)
            job["finished_at"] = f"2000-01-01T00:{i // 60:02d}:{i % 60:02d}"
            ids.append(job["id"])
        # the next insert prunes the oldest finished entries beyond the cap
        extra = {"build_id": "prunelast001"}
        server._job_track(extra, "summarize")
        server._job_transition(extra, "done")
        ids.append(extra["id"])

        finished = [j for j in server._jobs.values()
                    if j.get("state") not in server._JOB_ACTIVE]
        assert len(finished) <= server._JOBS_KEEP
        assert ids[0] not in server._jobs          # oldest pruned
        assert ids[-1] in server._jobs             # newest kept
        assert running["id"] in server._jobs       # active never pruned
        assert running["id"] not in [j["id"] for j in finished]
    finally:
        _finish(running["id"])


def test_snapshot_never_contains_credentials():
    job = {
        "build_id": "secretjob001",
        "pdf": "C:/somewhere/book.pdf",
        "cfg": {"claude_key": "sk-SECRET-CLAUDE", "aws_secret": "AWS-SECRET",
                "mistral_key": "MISTRAL-SECRET"},
        "prompt": "system prompt text that must not persist",
    }
    server._job_track(job, "ocr")
    server._job_transition(job, "done")
    raw = server.JOBS_PATH.read_text(encoding="utf-8")
    for secret in ("sk-SECRET-CLAUDE", "AWS-SECRET", "MISTRAL-SECRET",
                   "system prompt text", "cfg", "book.pdf"):
        assert secret not in raw
    snap = json.loads(raw)
    assert set(snap[job["id"]]) <= set(server._JOB_FIELDS)


def test_live_progress_checkpoint_is_persisted():
    job = {"build_id": "checkpoint001", "done": 0, "total": 300}
    server._job_track(job, "ocr", label="Large OCR batch")
    try:
        job["done"] = 137
        job["note"] = "page 137"
        server._job_checkpoint(job, force=True)

        snap = json.loads(server.JOBS_PATH.read_text(encoding="utf-8"))
        assert snap[job["id"]]["done"] == 137
        assert snap[job["id"]]["total"] == 300
        assert snap[job["id"]]["note"] == "page 137"
        assert "_checkpoint_at" not in snap[job["id"]]
    finally:
        _finish(job["id"])


# --- unified endpoints -----------------------------------------------------------

def test_jobs_listing_includes_downloads_and_sync(client):
    job = {"build_id": "listing00001"}
    server._job_track(job, "annotate", label="Listed Work")
    server._downloads["listing-ident"] = {"status": "downloading",
                                          "bytes": 10, "total": 100}
    server._cloudsync["running"] = True
    try:
        data = client.get("/api/jobs").get_json()
        assert data["ok"] is True
        rows = {r.get("id"): r for r in data["jobs"] if r.get("id")}
        assert rows[job["id"]]["state"] == "running"
        assert rows[job["id"]]["label"] == "Listed Work"
        kinds = [r["kind"] for r in data["jobs"]]
        assert "download" in kinds and "cloudsync" in kinds
        dl = next(r for r in data["jobs"] if r["kind"] == "download")
        assert dl == {"kind": "download", "label": "listing-ident",
                      "state": "running", "done": 10, "total": 100}
        assert data["active"] >= 3

        active = client.get("/api/jobs/active").get_json()
        assert active["count"] >= 2
        by_kind = {j["kind"]: j for j in active["jobs"]}
        assert by_kind["annotate"]["cancellable"] is True
        assert by_kind["download"]["cancellable"] is False
        assert "cloudsync" not in by_kind      # converges on its next run
        assert any("Listed Work" in lbl for lbl in active["labels"])
    finally:
        server._downloads.pop("listing-ident", None)
        server._cloudsync["running"] = False
        _finish(job["id"])


def test_unified_cancel_endpoint_flags_ocr_job(client):
    job = {"build_id": "unicancel001", "pdf": "unused.pdf", "cfg": {},
           "pages": [{"page": 1, "service": "tesseract", "status": "queued"}],
           "cancel_requested": False, "cancelled": 0}
    ev = server._job_track(job, "ocr")
    server._ocr_jobs[job["id"]] = job
    try:
        data = client.post(f"/api/jobs/{job['id']}/cancel").get_json()
        assert data["ok"] is True
        assert data["job"]["state"] == "cancelling"
        assert ev.is_set()
        assert job["cancel_requested"] is True   # the page loop's own flag

        # idempotent once finished
        server._job_transition(job, "cancelled")
        again = client.post(f"/api/jobs/{job['id']}/cancel").get_json()
        assert again["ok"] is True
        assert again["job"]["state"] == "cancelled"

        assert client.post("/api/jobs/nosuchjob0000/cancel").status_code == 404
    finally:
        server._ocr_jobs.pop(job["id"], None)
        _finish(job["id"])


def test_versioned_job_transport_filters_and_cancels(client):
    first = {"build_id": "v1jobs-book", "status": "running"}
    second = {"build_id": "other-book", "status": "running"}
    first_event = server._job_track(first, "ocr", label="V1 OCR")
    server._job_track(second, "summarize", label="Other")
    try:
        listing = client.get(
            "/api/v1/jobs?state=active&kind=ocr&item_id=v1jobs-book"
        ).get_json()
        assert listing["ok"] is True
        assert [row["id"] for row in listing["jobs"]] == [first["id"]]
        assert listing["active"] == 1

        fetched = client.get(f"/api/v1/jobs/{first['id']}").get_json()
        assert fetched["job"]["subject"]["item_id"] == "v1jobs-book"
        assert fetched["job"]["progress"] == {
            "completed": 0, "total": 0, "unit": "", "phase": ""}
        assert client.get("/api/v1/jobs/no-such-job").status_code == 404

        cancelled = client.post(
            f"/api/v1/jobs/{first['id']}/cancel"
        ).get_json()
        assert cancelled["job"]["state"] == "cancelling"
        assert first_event.is_set()
        events = client.get("/api/v1/job-events?after=0&limit=500").get_json()
        matching = [event for event in events["events"]
                    if event["job"]["id"] == first["id"]]
        assert [event["type"] for event in matching][-2:] == [
            "created", "cancel-requested"]
        assert events["cursor"] >= matching[-1]["sequence"]
        assert client.post(
            "/api/v1/jobs/no-such-job/cancel"
        ).status_code == 404
    finally:
        _finish(first["id"])
        _finish(second["id"])


def test_cancel_race_cannot_overwrite_worker_terminal_state(client):
    """A worker finishing during Event.set must win after cancellation.

    Before the state check/transition became one registry-lock transaction,
    this interleaving left the job permanently ``cancelling``.
    """
    def exercise(kind: str, legacy: bool) -> None:
        job = {"build_id": f"race-{kind}", "status": "running"}
        real_event = server._job_track(job, kind, label="Race")
        if legacy:
            server._ocr_jobs[job["id"]] = job
        go = threading.Event()
        attempted = threading.Event()

        class FinishWhileCancelling:
            def set(self):
                real_event.set()
                go.set()
                assert attempted.wait(timeout=2)

            def is_set(self):
                return real_event.is_set()

        server._jobs_events[job["id"]] = FinishWhileCancelling()

        def finish():
            assert go.wait(timeout=2)
            attempted.set()              # transition blocks on _jobs_lock
            server._job_transition(job, "done")

        worker = threading.Thread(target=finish)
        worker.start()
        try:
            path = (f"/api/ocr/job/{job['id']}/cancel" if legacy
                    else f"/api/jobs/{job['id']}/cancel")
            assert client.post(path).status_code == 200
            worker.join(timeout=2)
            assert not worker.is_alive()
            assert job["state"] == "done"
            assert job["state"] not in server._JOB_ACTIVE
        finally:
            if legacy:
                server._ocr_jobs.pop(job["id"], None)
            server._jobs_events[job["id"]] = real_event
            _finish(job["id"])

    exercise("summarize", False)
    exercise("ocr", True)


# --- cooperative cancellation of an analyze job ----------------------------------

def test_cancel_translate_between_pages_keeps_saved_pages(client, monkeypatch):
    build = _ready_build(client, "Cancelled Translation")
    ocr_dir = server._entry_dir(build["id"]) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "compiled.txt").write_text(
        "--- page 1 ---\nFirst page text.\n\n"
        "--- page 2 ---\nSecond page text.\n",
        encoding="utf-8",
    )

    started: list[dict] = []
    calls: list[str] = []

    def run_inline(bid, kind, total, target, decorate=None):
        job = server._an_job_new(bid, kind, total)
        if decorate is not None:
            decorate(job)
        started.append(job)
        target(job)
        return job

    def fake_ai_chat(_cfg, messages, **_kwargs):
        calls.append(str(messages[-1].get("content") or ""))
        # a cancel that lands while page 1 is in flight: the loop must
        # notice it before page 2 goes out
        server._jobs_events[started[-1]["id"]].set()
        return "TRANSLATED PAGE ONE"

    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://example.test/v1", "key": "k", "model": "m"})
    monkeypatch.setattr(server, "_ai_chat", fake_ai_chat)
    monkeypatch.setattr(server, "_an_job_start", run_inline)

    data = client.post("/api/analyze/translate", json={
        "build_id": build["id"], "lang": "en"}).get_json()
    assert data["ok"] is True

    job = client.get(f"/api/analyze/job/{data['job']}").get_json()
    assert job["status"] == "cancelled"
    assert job["state"] == "cancelled"
    assert "saved pages kept" in job["note"]
    assert job["done"] == 1
    assert len(calls) == 1                       # page 2 never went out

    saved = server._read_entry_text(build["id"], "translations/en.txt")
    assert "TRANSLATED PAGE ONE" in saved        # progressive save survived
    assert "page 2" not in saved

    unified = client.get("/api/jobs").get_json()["jobs"]
    row = next(r for r in unified if r.get("id") == data["job"])
    assert row["state"] == "cancelled"


def test_analyze_job_registration_uses_canonical_item_gate(
        tmp_path, monkeypatch):
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({
        "analyze-gate": {"id": "analyze-gate", "title": "Analyze Herbal"},
    }), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)

    job = server._an_job_new("analyze-gate", "summarize", 3)
    try:
        assert job["build_id"] == "analyze-gate"
        assert job["subject"]["item_id"] == "analyze-gate"
        assert server._jobs[job["id"]] is job
        assert server._jobs[job["id"]]["label"] == "Analyze Herbal"
    finally:
        with server._an_jobs_lock:
            server._an_jobs.pop(job["id"], None)
        _finish(job["id"])


# --- restart: persisted jobs come back as interrupted -----------------------------

def test_restart_marks_active_jobs_interrupted(client):
    snap = {
        "restarta0001": {"id": "restarta0001", "kind": "translate:en",
                         "build_id": "b1", "label": "Herbal",
                         "state": "running", "status": "running",
                         "done": 3, "total": 9,
                         "created_at": "2026-01-01T00:00:00+00:00"},
        "restartb0002": {"id": "restartb0002", "kind": "ocr",
                         "state": "cancelling", "status": "cancelling"},
        "restartc0003": {"id": "restartc0003", "kind": "publish",
                         "state": "queued", "status": "queued"},
        "restartd0004": {"id": "restartd0004", "kind": "summarize",
                         "state": "done", "status": "done",
                         "finished_at": "2026-01-01T00:00:01+00:00"},
    }
    existing = json.loads(server.JOBS_PATH.read_text(encoding="utf-8")) \
        if server.JOBS_PATH.is_file() else {}
    server.lib.save_json(server.JOBS_PATH, dict(existing, **snap))

    server._jobs_load()                          # what startup runs

    a = server._jobs["restarta0001"]
    assert a["state"] == a["status"] == "interrupted"
    assert a["note"] == "interrupted by restart — progressive output kept"
    assert a["done"] == 3 and a["total"] == 9    # progress survives
    assert server._jobs["restartb0002"]["state"] == "interrupted"
    assert server._jobs["restartc0003"]["note"] == \
        "interrupted by restart — not applied"
    assert server._jobs["restartd0004"]["state"] == "done"   # untouched

    # pollers get the honest answer instead of a 404
    an = client.get("/api/analyze/job/restarta0001")
    assert an.status_code == 200
    assert an.get_json()["status"] == "interrupted"
    ocr = client.get("/api/ocr/job/restartb0002")
    assert ocr.status_code == 200
    assert ocr.get_json()["job"]["status"] == "interrupted"
    assert ocr.get_json()["job"]["pages"] == []
    # a cancel of interrupted work is a no-op, not an error
    assert client.post("/api/jobs/restarta0001/cancel").get_json()["ok"] is True


# --- publish: stage-boundary cancellation with rollback ---------------------------

def test_publish_job_registration_uses_canonical_item_gate(
        client, tmp_path, monkeypatch):
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({
        "publish-gate": {
            "id": "publish-gate",
            "title": "Publish Herbal",
            "status": "ready",
            "rights": "public-domain",
        },
    }), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "_cloud_cfg", lambda: {"configured": True})

    started: list[tuple] = []

    class DeferredThread:
        def __init__(self, *, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr(server.threading, "Thread", DeferredThread)
    with server._publish_lock:
        server._publish["running"] = False

    response = client.post(
        "/api/volumes/publish", json={"build_id": "publish-gate"},
    )
    assert response.status_code == 200
    job_id = response.get_json()["job"]
    try:
        job = server._jobs[job_id]
        assert job["build_id"] == "publish-gate"
        assert job["subject"]["item_id"] == "publish-gate"
        assert job["label"] == "Publish Herbal"
        assert started and started[0][1][0] == "publish-gate"
    finally:
        _finish(job_id)
        with server._publish_lock:
            server._publish["running"] = False
            server._publish["job"] = ""

def test_publish_cancel_rolls_back_uploaded_objects(tmp_path, monkeypatch):
    pdf = tmp_path / "volume.pdf"
    pdf.write_bytes(b"%PDF-cancel-me")
    builds_path = tmp_path / "builds.json"
    builds_path.write_text(json.dumps({"pubcxbuild01": {
        "id": "pubcxbuild01", "status": "ready", "title": "Cancel Me",
        "year": "1700", "pdf_file": str(pdf), "bundle": {},
    }}), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "_cloud_cfg",
                        lambda: {"url": "https://cloud", "key": "svc"})
    monkeypatch.setattr(server.r2, "configured", lambda cfg: False)
    monkeypatch.setattr(server.sbase, "_rest", lambda *a, **k: [])

    job = {"id": "pubcancel001", "build_id": "pubcxbuild01",
           "kind": "publish", "status": "running"}
    ev = server._job_track(job, "publish", label="Cancel Me")

    uploaded, deleted = [], []

    def fake_upload(cloud, bucket, name, data, mime):
        uploaded.append(name)
        ev.set()                # cancel arrives while the PDF is in flight
    monkeypatch.setattr(server.sbase, "upload_object", fake_upload)
    monkeypatch.setattr(server.sbase, "public_url",
                        lambda cloud, bucket, name: f"https://cloud/{bucket}/{name}")
    monkeypatch.setattr(server.sbase, "delete_objects",
                        lambda cloud, bucket, paths: deleted.extend(paths))
    monkeypatch.setattr(
        server.sbase, "upsert_volume",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a cancelled publish must never record a row")))

    server._publish_run("pubcxbuild01", "tester", job)

    assert job["state"] == "cancelled"
    assert "rolled back" in job["note"]
    assert uploaded == ["cancel-me-1700.pdf"]
    assert deleted == ["cancel-me-1700.pdf"]     # the orphan came back down
    assert server._publish["stage"] == "cancelled"
    assert server._publish["running"] is False
    fresh = json.loads(builds_path.read_text(encoding="utf-8"))
    assert fresh["pubcxbuild01"]["status"] == "ready"   # not marked uploaded


# --- UI + shell contracts ----------------------------------------------------------

def test_queue_table_renders_unified_registry_rows():
    jobs = _between(APP, "function renderOcrQueue()", "async function cancelOcrJob")
    assert "jobsState.rows" in jobs
    assert "data-job-cancel" in jobs
    assert "<td>OCR</td>" in jobs                # session-local fallback rows
    assert "<td>Text analysis</td>" in jobs
    assert 'fetch("/api/jobs")' in APP
    assert "function pollJobs" in APP
    assert 'fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`' in APP
    # legacy pollers now treat the new terminal states as finished
    polling = _between(APP, "function anEnsurePolling()", "async function loadAnOverview")
    assert 'job.status === "cancelled"' in polling
    assert 'job.status === "interrupted"' in polling


def test_footer_jobs_marker_is_wired_and_understated():
    assert 'id="status-jobs"' in TEMPLATE
    assert ".foot-jobs" in STYLE
    assert "renderJobsFooter" in APP
    assert "jobs running" in APP


def test_desktop_quit_guard_asks_about_active_jobs():
    assert "/api/jobs/active" in DESKTOP_MAIN
    assert "showMessageBoxSync" in DESKTOP_MAIN
    for button in ("Wait", "Cancel all and quit", "Quit anyway"):
        assert button in DESKTOP_MAIN
    assert "confirmCloseWithJobs" in DESKTOP_MAIN
