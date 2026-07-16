"""Smart check: OCR a book's own PDF front matter and extract real metadata.

The pipeline (fetch -> scan -> Mistral OCR -> DeepSeek extraction) runs on a
background thread in production; these tests replace `_sc_job_start` with an
inline runner and stub the render/OCR/AI seams, mirroring how the analyze and
OCR suites fake their external calls.
"""
from __future__ import annotations

import json
from pathlib import Path

import capture_pipeline as capture
import server


# --- harness -----------------------------------------------------------------

def _install_inline_sc(monkeypatch):
    """Run smart-check workers synchronously on the calling thread."""
    def start(target, label, run):
        job = server._sc_job_new(target, label)
        run(job)
        return job
    monkeypatch.setattr(server, "_sc_job_start", start)


def _fake_pipeline(monkeypatch, texts, ai_reply, scan_pages=(1, 2, 3, 4, 5)):
    """Stub the render/OCR/extraction seams; returns the call recorder."""
    calls = {"ocr": [], "prompts": []}
    monkeypatch.setattr(server, "_sc_scan_pages", lambda pdf: list(scan_pages))

    def ocr(pdf, page, key):
        assert key == "mk"
        calls["ocr"].append(page)
        return texts.get(page, "")
    monkeypatch.setattr(server, "_sc_ocr_page", ocr)
    monkeypatch.setattr(server, "_client_settings", lambda: {"mistralKey": "mk"})
    if ai_reply is not None:
        monkeypatch.setattr(server, "_ai_cfg", lambda: {
            "base": "https://api.deepseek.com", "model": "deepseek-chat",
            "key": "dk", "instructions": "", "temperature": "", "timeout": ""})

        def ai_json(cfg, messages, temperature=0.2):
            calls["prompts"].append(messages[0]["content"])
            return ai_reply
        monkeypatch.setattr(server, "_ai_json", ai_json)
    return calls


def _dummy_pdf(data_root: Path, name: str = "book.pdf") -> str:
    p = data_root / name
    p.write_bytes(b"%PDF-dummy")
    return name


def _make_build(client, title="Untitled herbal") -> str:
    r = client.post("/api/builds", json={"build": {"title": title}})
    assert r.status_code == 200 and r.get_json()["ok"]
    return r.get_json()["build"]["id"]


# OCR pages: an empty cover, a title page (imprint + year), a copyright page.
_TEXTS = {
    1: "",
    2: "# THE ENGLISH PHYSICIAN\n\nLondon: Printed for the Author, 1652",
    3: "Copyright 1652 — All rights reserved",
    4: "TABLE OF CONTENTS",
    5: "Chapter one begins here",
}

_AI_REPLY = {
    "title": "The English Physician", "subtitle": "", "author": "Nicholas Culpeper",
    "volume": "", "edition": "", "publisher": "Printed for the Author",
    "year": "1652", "city": "London", "language": "english",
    "extra": {"printer": "Peter Cole"},
}


# --- the pipeline ------------------------------------------------------------

def test_smart_check_extracts_and_holds_a_pending_record(client, data_root,
                                                         monkeypatch):
    _install_inline_sc(monkeypatch)
    calls = _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    bid = _make_build(client, "Volunteer mess title")
    pdf = _dummy_pdf(data_root)

    r = client.post("/api/smartcheck/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    assert r.status_code == 200
    job = r.get_json()["job"]
    assert job["status"] == "done" and job["state"] == "done"
    assert job["done"] == job["total"]

    # early stop: once a title-page signal AND a copyright signal are in hand
    # (pages 2 and 3), pages 4-5 are never OCRed
    assert calls["ocr"] == [1, 2, 3]
    # the extraction prompt carries the page-marked OCR text
    assert "--- page 2 ---" in calls["prompts"][0]

    r = client.get("/api/smartcheck")
    pending = r.get_json()["pending"]
    rec = pending[f"build:{bid}"]
    # raw extraction vocabulary is kept verbatim...
    assert rec["fields"]["author"] == "Nicholas Culpeper"
    assert rec["extra"] == {"printer": "Peter Cole"}
    # ...and mapped into the build store's field names
    assert rec["mapped"]["authors"] == "Nicholas Culpeper"
    assert rec["mapped"]["publisher_city"] == "London"
    assert "city" not in rec["mapped"]
    assert rec["engine"] == {"ocr": capture.OCR_MODEL, "extract": "deepseek-chat"}
    assert rec["pdf"] == {"path": pdf}
    assert rec["pages_ocred"] == [2, 3]
    assert rec["label"] == "Volunteer mess title"

    # the pending overlay never touches the record itself
    b = json.loads((data_root / "output" / "whl_builds.json")
                   .read_text(encoding="utf-8"))[bid]
    assert b["title"] == "Volunteer mess title"
    assert b.get("authors", "") == ""

    # the job endpoint answers like the analyze one
    r = client.get(f"/api/smartcheck/job/{job['id']}")
    assert r.get_json()["status"] == "done"


def test_smart_check_resolve_bake_moves_record_to_audit_trail(client, data_root,
                                                              monkeypatch):
    _install_inline_sc(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    client.post("/api/smartcheck/run", json={"target": f"build:{bid}", "pdf": pdf})

    r = client.post("/api/smartcheck/resolve", json={
        "target": f"build:{bid}", "action": "baked",
        "applied": {"authors": "Nicholas Culpeper"}})
    assert r.get_json()["ok"]

    assert (f"build:{bid}"
            not in client.get("/api/smartcheck").get_json()["pending"])
    doc = json.loads((data_root / "output" / "smart_checks.json")
                     .read_text(encoding="utf-8"))
    trail = [x for x in doc["resolved"] if x["target"] == f"build:{bid}"]
    assert trail and trail[-1]["resolved"]["action"] == "baked"
    assert trail[-1]["resolved"]["applied"] == {"authors": "Nicholas Culpeper"}

    # resolving twice is a 404 — the record already left pending
    r = client.post("/api/smartcheck/resolve",
                    json={"target": f"build:{bid}", "action": "baked"})
    assert r.status_code == 404


def test_smart_check_falls_back_to_mistral_extraction(client, data_root,
                                                      monkeypatch):
    """No AI key configured: extraction uses Mistral, like the phone app."""
    _install_inline_sc(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, ai_reply=None)   # real _ai_cfg -> no key
    seen = {}

    def fake_extract(text, key, timeout=60.0):
        seen["key"] = key
        out = capture.empty_bibliography()
        out["title"] = "Fallback Title"
        return out
    monkeypatch.setattr(server.capture, "extract_bibliography", fake_extract)

    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    r = client.post("/api/smartcheck/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    assert r.get_json()["job"]["status"] == "done"
    assert seen["key"] == "mk"
    rec = client.get("/api/smartcheck").get_json()["pending"][f"build:{bid}"]
    assert rec["engine"]["extract"] == capture.EXTRACT_MODEL
    assert rec["mapped"] == {"title": "Fallback Title"}
    client.post("/api/smartcheck/resolve",
                json={"target": f"build:{bid}", "action": "dismissed"})


def test_smart_check_remote_url_is_fetched_on_the_worker(client, data_root,
                                                         monkeypatch):
    _install_inline_sc(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    fetched = {}

    def fake_cache(url):
        fetched["url"] = url
        p = data_root / "cached.pdf"
        p.write_bytes(b"%PDF-dummy")
        return p
    monkeypatch.setattr(server, "_remote_pdf_cache", fake_cache)

    bid = _make_build(client)
    url = "https://archive.org/download/x/x.pdf"
    r = client.post("/api/smartcheck/run",
                    json={"target": f"build:{bid}", "url": url})
    assert r.get_json()["job"]["status"] == "done"
    assert fetched["url"] == url
    rec = client.get("/api/smartcheck").get_json()["pending"][f"build:{bid}"]
    assert rec["pdf"] == {"url": url}
    client.post("/api/smartcheck/resolve",
                json={"target": f"build:{bid}", "action": "dismissed"})


def test_smart_check_job_fails_cleanly_without_readable_pages(client, data_root,
                                                              monkeypatch):
    _install_inline_sc(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY, scan_pages=())
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    r = client.post("/api/smartcheck/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    job = r.get_json()["job"]
    assert job["status"] == "error" and "no readable pages" in job["error"]
    assert f"build:{bid}" not in client.get("/api/smartcheck").get_json()["pending"]


def test_smart_check_validation(client, data_root, monkeypatch):
    _install_inline_sc(monkeypatch)
    pdf = _dummy_pdf(data_root)
    bid = _make_build(client)

    assert client.post("/api/smartcheck/run", json={}).status_code == 400
    assert client.post("/api/smartcheck/run", json={
        "target": "bogus:1", "pdf": pdf}).status_code == 400
    assert client.post("/api/smartcheck/run", json={
        "target": "build:nope", "pdf": pdf}).status_code == 404
    assert client.post("/api/smartcheck/run", json={
        "target": "manual:nope", "pdf": pdf}).status_code == 404
    assert client.post("/api/smartcheck/run", json={
        "target": "whl:notanumber", "pdf": pdf}).status_code == 400
    assert client.post("/api/smartcheck/run", json={
        "target": f"build:{bid}"}).status_code == 400            # no pdf/url
    assert client.post("/api/smartcheck/run", json={
        "target": f"build:{bid}", "pdf": "missing.pdf"}).status_code == 404
    assert client.post("/api/smartcheck/run", json={
        "target": f"build:{bid}", "url": "ftp://x/y.pdf"}).status_code == 400
    assert client.post("/api/smartcheck/resolve", json={
        "target": "whl:1", "action": "shredded"}).status_code == 400


def test_smart_check_duplicate_run_joins_the_live_job(client, data_root,
                                                      monkeypatch):
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    target = f"build:{bid}"
    live = {"id": "sc-live-1", "kind": "smartcheck", "target": target,
            "state": "running", "status": "running", "label": "x"}
    with server._jobs_lock:
        server._jobs["sc-live-1"] = live
    try:
        r = client.post("/api/smartcheck/run", json={"target": target, "pdf": pdf})
        data = r.get_json()
        assert data["ok"] and data.get("already") is True
        assert data["job"]["id"] == "sc-live-1"
        assert data["job"]["target"] == target
    finally:
        with server._jobs_lock:
            server._jobs.pop("sc-live-1", None)


def test_smart_check_whl_mapping_drops_blanks_and_unknown_fields():
    got = server._sc_map_fields("whl", {
        "title": "T", "subtitle": "", "author": "A", "year": "1900",
        "publisher": " P ", "city": "München", "edition": "2nd",
        "volume": "", "language": "german"})
    # city/edition/volume have no WHL columns; blanks never map
    assert got == {"title": "T", "authors": "A", "year": "1900",
                   "publisher": "P", "language": "german"}


def test_normalize_bibliography_contract():
    assert capture.normalize_bibliography("junk") == capture.empty_bibliography()
    out = capture.normalize_bibliography({
        "title": " X ", "year": 1652,
        "extra": {"a": ["b"], "empty": "", "keep": "v"}})
    assert out["title"] == "X" and out["year"] == "1652"
    assert out["extra"] == {"a": '["b"]', "keep": "v"}


# --- UI contract ---------------------------------------------------------------
# The client pieces the feature depends on; mirrors the asset assertions the
# other suites make so a refactor can't silently orphan the endpoints.

def test_smart_check_ui_contract():
    root = Path(__file__).resolve().parents[1] / "tools" / "whl_explorer"
    app_js = (root / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    css = (root / "static" / "style.css").read_text(encoding="utf-8")

    for needle in ('boot("smart check", initSmartCheck)', "data-sc-run",
                   "/api/smartcheck/run", "/api/smartcheck/resolve",
                   'case "smartbake"', "scDecorateWhlRow(tr, r, mode)",
                   "scDecorateCheckedRow(tr, row)", "scApplyBuildOverlay()",
                   "bake: \"edit\"", "wand:"):
        assert needle in app_js, needle
    assert 'id="b-smartcheck"' in html
    for needle in ("td.sc-prov", ".sc-wand", "sc-pulse"):
        assert needle in css, needle
