"""Smart Scan: OCR a book's own PDF front matter and stage extracted metadata.

Process mode's Smart Scan runs the smart-check ENGINE (fetch -> blank-page scan
-> Mistral OCR -> DeepSeek extraction) on a background thread and stages the
result as a "smartscan" alternative in staged_alts.json — the record itself is
never touched until the user Marks Primary. These tests replace `_ss_job_start`
with an inline runner and stub the render/OCR/AI seams, mirroring how the
analyze and OCR suites fake their external calls. (Ported from the retired
wand-overlay smart-check suite; the engine and its coverage carry over.)
"""
from __future__ import annotations

import json
from pathlib import Path

import capture_pipeline as capture
import server


# --- harness -----------------------------------------------------------------

def _install_inline_ss(monkeypatch):
    """Run Smart Scan workers synchronously on the calling thread."""
    def start(target, label, run):
        job = server._ss_job_new(target, label)
        run(job)
        return job
    monkeypatch.setattr(server, "_ss_job_start", start)


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


def _staged_entry(client, target):
    return client.get("/api/staged").get_json()["entries"].get(target)


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

def test_smart_scan_extracts_and_stages_an_alternative(client, data_root,
                                                       monkeypatch):
    _install_inline_ss(monkeypatch)
    calls = _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    bid = _make_build(client, "Volunteer mess title")
    pdf = _dummy_pdf(data_root)

    r = client.post("/api/process/smartscan/run",
                    json={"target": f"build:{bid}", "pdf": pdf,
                          "label": "Volunteer mess title"})
    assert r.status_code == 200
    job = r.get_json()["job"]
    assert job["status"] == "done" and job["state"] == "done"
    assert job["done"] == job["total"]

    # early stop: once a title-page signal AND a copyright signal are in hand
    # (pages 2 and 3), pages 4-5 are never OCRed
    assert calls["ocr"] == [1, 2, 3]
    # the extraction prompt carries the page-marked OCR text
    assert "--- page 2 ---" in calls["prompts"][0]

    e = _staged_entry(client, f"build:{bid}")
    assert e and e["kind"] == "build" and e["label"] == "Volunteer mess title"
    alt = e["alts"][0]
    assert alt["source"] == "smartscan"
    # extraction vocabulary mapped into the build store's field names
    assert alt["fields"]["authors"] == "Nicholas Culpeper"
    assert alt["fields"]["publisher_city"] == "London"
    assert "city" not in alt["fields"]
    # provenance travels in the note: which pages, which model
    assert "[2, 3]" in alt["note"] and "deepseek-chat" in alt["note"]

    # staging never touches the record itself
    b = json.loads((data_root / "output" / "whl_builds.json")
                   .read_text(encoding="utf-8"))[bid]
    assert b["title"] == "Volunteer mess title"
    assert b.get("authors", "") == ""

    # the job endpoint answers like the analyze one
    r = client.get(f"/api/process/smartscan/job/{job['id']}")
    assert r.get_json()["status"] == "done"


def test_smart_scan_stops_after_title_page_without_copyright_signal(
        client, data_root, monkeypatch):
    """Books that never print "copyright" (pre-1900, non-English) must not
    burn the whole OCR budget hunting a signal that never fires."""
    _install_inline_ss(monkeypatch)
    texts = {
        1: "",
        2: "HERBARIUM VIVUM\n\nApud Christophorum Plantinum, 1581",
        3: "Ad lectorem praefatio",
        4: "Index plantarum",
        5: "Caput primum",
        6: "More front matter", 7: "Even more", 8: "And more",
    }
    calls = _fake_pipeline(monkeypatch, texts, _AI_REPLY,
                           scan_pages=(1, 2, 3, 4, 5, 6, 7, 8))
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    r = client.post("/api/process/smartscan/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    assert r.get_json()["job"]["status"] == "done"
    # title-ish seen on page 2; the scan stops once four pages have text
    assert calls["ocr"] == [1, 2, 3, 4, 5]


def test_smart_scan_mark_primary_swap_files_the_displaced_original(client,
                                                                   data_root,
                                                                   monkeypatch):
    """The staged/swap endpoint (Mark Primary's server half) removes the applied
    alt and re-files the displaced values as a superseded alternative."""
    _install_inline_ss(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    client.post("/api/process/smartscan/run",
                json={"target": f"build:{bid}", "pdf": pdf})
    target = f"build:{bid}"
    alt = _staged_entry(client, target)["alts"][0]

    r = client.post("/api/staged/swap", json={
        "target": target, "altId": alt["id"],
        "displaced": {"source": "superseded",
                      "fields": {"authors": "", "title": "Volunteer mess title"},
                      "note": "was Smart Scan"}})
    assert r.get_json()["ok"]
    e = _staged_entry(client, target)
    assert [a["source"] for a in e["alts"]] == ["superseded"]
    assert e["alts"][0]["fields"]["title"] == "Volunteer mess title"


def test_smart_scan_falls_back_to_mistral_extraction(client, data_root,
                                                     monkeypatch):
    """No AI key configured: extraction uses Mistral, like the phone app."""
    _install_inline_ss(monkeypatch)
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
    r = client.post("/api/process/smartscan/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    assert r.get_json()["job"]["status"] == "done"
    assert seen["key"] == "mk"
    alt = _staged_entry(client, f"build:{bid}")["alts"][0]
    assert alt["fields"] == {"title": "Fallback Title"}
    assert capture.EXTRACT_MODEL in alt["note"]


def test_smart_scan_remote_url_is_fetched_on_the_worker(client, data_root,
                                                        monkeypatch):
    _install_inline_ss(monkeypatch)
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
    r = client.post("/api/process/smartscan/run",
                    json={"target": f"build:{bid}", "url": url})
    assert r.get_json()["job"]["status"] == "done"
    assert fetched["url"] == url
    assert _staged_entry(client, f"build:{bid}") is not None


def test_smart_scan_job_fails_cleanly_without_readable_pages(client, data_root,
                                                             monkeypatch):
    _install_inline_ss(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY, scan_pages=())
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    r = client.post("/api/process/smartscan/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    job = r.get_json()["job"]
    assert job["status"] == "error" and "no readable pages" in job["error"]
    assert _staged_entry(client, f"build:{bid}") is None


def test_smart_scan_validation(client, data_root, monkeypatch):
    _install_inline_ss(monkeypatch)
    pdf = _dummy_pdf(data_root)
    bid = _make_build(client)

    assert client.post("/api/process/smartscan/run", json={}).status_code == 400
    assert client.post("/api/process/smartscan/run", json={
        "target": "bogus:1", "pdf": pdf}).status_code == 400
    assert client.post("/api/process/smartscan/run", json={
        "target": f"build:{bid}"}).status_code == 400            # no pdf/url
    assert client.post("/api/process/smartscan/run", json={
        "target": f"build:{bid}", "pdf": "missing.pdf"}).status_code == 404
    assert client.post("/api/process/smartscan/run", json={
        "target": f"build:{bid}", "url": "ftp://x/y.pdf"}).status_code == 400
    assert client.get("/api/process/smartscan/job/nope").status_code == 404


def test_smart_scan_empty_extraction_fails_the_job(client, data_root,
                                                   monkeypatch):
    """A parse failure ({} from the AI) must not stage an all-blank alternative
    — that would render as 'nothing to change'."""
    _install_inline_ss(monkeypatch)
    _fake_pipeline(monkeypatch, _TEXTS, ai_reply={})
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    r = client.post("/api/process/smartscan/run",
                    json={"target": f"build:{bid}", "pdf": pdf})
    job = r.get_json()["job"]
    assert job["status"] == "error"
    assert "no usable fields" in job["error"]
    assert _staged_entry(client, f"build:{bid}") is None


def test_smart_scan_duplicate_run_joins_the_live_job(client, data_root,
                                                     monkeypatch):
    _fake_pipeline(monkeypatch, _TEXTS, _AI_REPLY)
    bid = _make_build(client)
    pdf = _dummy_pdf(data_root)
    target = f"build:{bid}"
    live = {"id": "ss-live-1", "kind": "smartscan", "target": target,
            "state": "running", "status": "running", "label": "x"}
    with server._jobs_lock:
        server._jobs["ss-live-1"] = live
    try:
        r = client.post("/api/process/smartscan/run",
                        json={"target": target, "pdf": pdf})
        data = r.get_json()
        assert data["ok"] and data.get("already") is True
        assert data["job"]["id"] == "ss-live-1"
    finally:
        with server._jobs_lock:
            server._jobs.pop("ss-live-1", None)


# --- the engine helpers ------------------------------------------------------

def test_smart_scan_whl_mapping_drops_blanks_and_unknown_fields():
    got = server._sc_map_fields("whl", {
        "title": "A Title", "author": "  ", "publisher": "P",
        "year": "1900", "bogus": "x", "city": "London"})
    # whl rows have no city column; blanks never map
    assert got == {"title": "A Title", "publisher": "P", "year": "1900"}


def test_normalize_bibliography_contract():
    out = capture.normalize_bibliography({
        "title": " T ", "extra": {"printer": "P", "empty": "", "n": 3,
                                  "deep": {"a": 1}}})
    assert out["title"] == "T"
    assert out["author"] == ""
    assert out["extra"] == {"printer": "P", "n": "3", "deep": '{"a": 1}'}
    assert capture.normalize_bibliography("nonsense")["extra"] == {}


# --- the wand UI is retired --------------------------------------------------

def test_wand_ui_is_retired_and_process_surface_present():
    """The overlay smart-check front-end is gone; Process mode's Smart Scan is
    the one surface. History revert for old baked entries must survive."""
    root = Path(__file__).resolve().parents[1] / "tools" / "whl_explorer"
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    py = (root / "server.py").read_text(encoding="utf-8")

    for gone in ("scWandHtml", "data-sc-run", "onSmartKey", "initSmartCheck",
                 "scBakeTarget", "scDecorateWhlRow"):
        assert gone not in js, gone
    for gone in ('id="b-smartcheck"', 'id="status-smart"'):
        assert gone not in html, gone
    for gone in ("/api/smartcheck", "SMART_CHECKS_PATH"):
        assert gone not in py, gone

    # the Process-mode surface and the History revert shim remain
    assert "/api/process/smartscan/run" in js
    assert "procRunSmartScan" in js
    assert "scRevertItems" in js
