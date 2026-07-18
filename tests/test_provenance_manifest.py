"""Per-entry artifact provenance (issue #135): manifest.json records each
derived artifact's content hash, producer, and input hashes at job
completion; staleness is a hash comparison surfaced by the folder info; the
verbatim OCR reading is snapshotted before the first manual correction.

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so nothing here touches live data.
"""
from __future__ import annotations

import store_sync as ss

import server


def _ready_build(client, title: str) -> dict:
    response = client.post("/api/builds", json={"build": {
        "title": title,
        "status": "ready",
    }})
    assert response.status_code == 200
    return response.get_json()["build"]


def _write_compiled(bid: str, pages: dict[int, str]) -> None:
    d = server._entry_dir(bid) / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    (d / "compiled.txt").write_text(
        "\n\n".join(f"--- page {n} ---\n{pages[n]}" for n in sorted(pages)),
        encoding="utf-8")


def _install_inline_ai(monkeypatch) -> list:
    """Echo model: replies with a digest of what it was asked, runs the job
    on the calling thread so completion has happened when the POST returns."""
    calls: list = []

    def fake_ai_chat(_cfg, messages, **_kwargs):
        calls.append(messages)
        return "OUT::" + messages[-1]["content"].split("\n\n", 1)[1][:60]

    def run_inline(bid, kind, total, target, decorate=None):
        job = server._an_job_new(bid, kind, total)
        if decorate is not None:
            decorate(job)
        target(job)
        return job

    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://example.test/v1", "key": "k", "model": "test-model"})
    monkeypatch.setattr(server, "_ai_chat", fake_ai_chat)
    monkeypatch.setattr(server, "_an_job_start", run_inline)
    return calls


def _row(bid: str, rel: str) -> dict:
    return server._load_manifest(bid)["artifacts"][rel]


# --- analyze jobs record provenance; staleness round-trips through the UI feed ---

def test_summarize_records_provenance_and_ocr_edit_marks_stale(
        client, monkeypatch):
    build = _ready_build(client, "Provenance summary")
    bid = build["id"]
    _write_compiled(bid, {1: "Alpha original.", 2: "Beta original."})
    src_sha = server._file_sha256(server._entry_dir(bid) / "ocr" / "compiled.txt")
    _install_inline_ai(monkeypatch)

    r = client.post("/api/analyze/summarize", json={"build_id": bid})
    assert r.status_code == 200

    row = _row(bid, "summary.md")
    assert row["produced_by"] == {"kind": "summarize", "model": "test-model"}
    assert row["inputs"] == [{"artifact": "ocr/compiled.txt",
                              "sha256": src_sha}]
    assert row["sha256"] == server._file_sha256(
        server._entry_dir(bid) / "summary.md")
    assert row["created_at"] and row["updated_at"]

    entries = client.get("/api/entries").get_json()["entries"]
    assert entries[bid]["summary"]["stale"] is False
    assert entries[bid]["summary"]["produced_by"]["kind"] == "summarize"

    # correct the OCR text through the editor's save route -> summary stale
    r = client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt",
        "text": "--- page 1 ---\nAlpha corrected.\n\n"
                "--- page 2 ---\nBeta original.\n"})
    assert r.status_code == 200
    entries = client.get("/api/entries").get_json()["entries"]
    assert entries[bid]["summary"]["stale"] is True
    assert server._manifest_inputs_stale(bid, "summary.md") == \
        ["ocr/compiled.txt"]

    # regenerating consumes the corrected text -> fresh again
    r = client.post("/api/analyze/summarize", json={"build_id": bid})
    assert r.status_code == 200
    entries = client.get("/api/entries").get_json()["entries"]
    assert entries[bid]["summary"]["stale"] is False


def test_translate_records_doc_level_input_beside_per_page_meta(
        client, monkeypatch):
    build = _ready_build(client, "Provenance translate")
    bid = build["id"]
    _write_compiled(bid, {1: "Uno source.", 2: "Dos source."})
    src_sha = server._file_sha256(server._entry_dir(bid) / "ocr" / "compiled.txt")
    _install_inline_ai(monkeypatch)

    r = client.post("/api/analyze/translate",
                    json={"build_id": bid, "lang": "es"})
    assert r.status_code == 200

    row = _row(bid, "translations/es.txt")
    assert row["produced_by"] == {"kind": "translate", "model": "test-model"}
    assert row["inputs"] == [{"artifact": "ocr/compiled.txt",
                              "sha256": src_sha}]
    # the finer per-page sidecar (#136) still records page hashes untouched
    meta = server._load_translation_meta(bid, "es")
    assert set(meta["pages"]) == {"1", "2"}

    info = client.get(f"/api/builds/{bid}/folder").get_json()
    assert info["translations"][0]["stale"] is False

    r = client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt",
        "text": "--- page 1 ---\nUno corrected.\n\n"
                "--- page 2 ---\nDos source.\n"})
    assert r.status_code == 200
    info = client.get(f"/api/builds/{bid}/folder").get_json()
    assert info["translations"][0]["stale"] is True


# --- the verbatim layer -----------------------------------------------------------

def test_manual_ocr_edit_snapshots_verbatim_exactly_once(client):
    build = _ready_build(client, "Verbatim snapshot")
    bid = build["id"]
    verbatim = server._entry_dir(bid) / "ocr" / "verbatim" / "compiled.txt"

    # first PUT creates the file: nothing existed to snapshot
    r = client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt", "text": "--- page 1 ---\nORIGINAL OCR"})
    assert r.status_code == 200
    assert not verbatim.exists()

    # first overwrite of an existing file snapshots the pre-edit reading
    client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt", "text": "--- page 1 ---\nFIRST EDIT"})
    assert verbatim.read_text(encoding="utf-8") == \
        "--- page 1 ---\nORIGINAL OCR"

    # the snapshot is never updated after
    client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt", "text": "--- page 1 ---\nSECOND EDIT"})
    assert verbatim.read_text(encoding="utf-8") == \
        "--- page 1 ---\nORIGINAL OCR"

    # served read-only through the artifact GET family
    r = client.get(f"/api/builds/{bid}/artifact/verbatim/compiled.txt")
    assert r.status_code == 200
    assert r.get_json()["text"] == "--- page 1 ---\nORIGINAL OCR"

    assert _row(bid, "ocr/compiled.txt")["produced_by"] == \
        {"kind": "manual-edit"}


# --- OCR jobs ----------------------------------------------------------------------

def test_ocr_job_completion_records_doc_with_engine(
        client, data_root, monkeypatch):
    build = _ready_build(client, "Provenance OCR job")
    bid = build["id"]
    pdf = data_root / "downloads" / "provjob.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-fake provenance job source")
    job_id = "provocr001"
    job = {
        "id": job_id, "cfg": {}, "pdf": str(pdf),
        "pages": [{"page": 1, "service": "tesseract", "status": "queued"}],
        "width": 1400, "build_id": bid, "target": "compiled.txt",
        "src_key": "primary", "done": 0, "errors": 0, "cancelled": 0,
        "cancel_requested": False, "status": "running",
    }
    monkeypatch.setattr(server, "_ocr_page_png", lambda *_: b"png")
    monkeypatch.setitem(server._OCR_SERVICES, "tesseract",
                        lambda _png, _cfg: "recognized text")
    server._ocr_jobs[job_id] = job
    try:
        server._ocr_job_run(job_id)
    finally:
        server._ocr_jobs.pop(job_id, None)

    row = _row(bid, "ocr/compiled.txt")
    assert row["produced_by"] == {"kind": "ocr", "engine": "tesseract"}
    assert row["inputs"] == [{"artifact": "pdf:primary",
                              "path": "downloads/provjob.pdf",
                              "sha256": server._file_sha256(pdf)}]
    assert server._manifest_inputs_stale(bid, "ocr/compiled.txt") == []


# --- page deletion -------------------------------------------------------------------

def test_page_deletion_updates_manifest_entries(client, data_root):
    import fitz
    build = _ready_build(client, "Provenance deletion")
    bid = build["id"]
    pdf = data_root / "downloads" / "provdel" / "book.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(3):
        pg = doc.new_page(width=200, height=200)
        pg.insert_text((50, 100), f"PAGE {i + 1}")
    doc.save(str(pdf))
    doc.close()

    _write_compiled(bid, {1: "alpha", 2: "bravo", 3: "charlie"})
    tdir = server._entry_dir(bid) / "translations"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "es.txt").write_text(
        "--- page 1 ---\nuno\n\n--- page 2 ---\ndos\n\n--- page 3 ---\ntres",
        encoding="utf-8")
    server.lib.save_json(tdir / "es.meta.json", {
        "version": 1, "src": "compiled.txt", "model": "m", "pages": {}})
    server._write_entry_text(bid, "summary.md", "covers three pages\n")

    server._manifest_record(
        bid, "ocr/compiled.txt", {"kind": "ocr", "engine": "tesseract"},
        [server._manifest_input(bid, "pdf:primary", path=pdf)])
    server._manifest_record(
        bid, "translations/es.txt", {"kind": "translate", "model": "m"},
        [server._manifest_input(bid, "ocr/compiled.txt")])
    server._manifest_record(
        bid, "summary.md", {"kind": "summarize", "model": "m"},
        [server._manifest_input(bid, "ocr/compiled.txt")])

    builds = server.lib.load_json(server.BUILDS_PATH, {})
    result = server._apply_page_deletion(bid, builds, pdf, [2])
    assert result["deleted"] == [2]

    # the renumbered OCR doc is a manual edit, refreshed against the new PDF
    row = _row(bid, "ocr/compiled.txt")
    assert row["produced_by"] == {"kind": "manual-edit"}
    assert row["sha256"] == server._file_sha256(
        server._entry_dir(bid) / "ocr" / "compiled.txt")
    assert server._manifest_inputs_stale(bid, "ocr/compiled.txt") == []

    # translations moved in the same lockstep pass: producer kept, not stale
    row = _row(bid, "translations/es.txt")
    assert row["produced_by"] == {"kind": "translate", "model": "m"}
    assert server._manifest_inputs_stale(bid, "translations/es.txt") == []

    # the summary was NOT renumbered: its recorded input hash honestly stales
    assert server._manifest_inputs_stale(bid, "summary.md") == \
        ["ocr/compiled.txt"]


# --- degradation and the hash cap ---------------------------------------------------

def test_legacy_entry_without_manifest_reports_null_stale(client):
    build = _ready_build(client, "Legacy no manifest")
    bid = build["id"]
    _write_compiled(bid, {1: "old text"})
    server._write_entry_text(bid, "summary.md", "an old summary\n")
    assert not server._manifest_path(bid).is_file()

    entries = client.get("/api/entries").get_json()["entries"]
    assert entries[bid]["summary"]["stale"] is None
    assert entries[bid]["summary"]["produced_by"] is None
    assert entries[bid]["ocr"][0]["stale"] is None
    assert client.get(f"/api/builds/{bid}/folder").status_code == 200
    assert server._manifest_inputs_stale(bid, "summary.md") is None


def test_files_above_cap_record_size_and_mtime_not_hash(
        client, data_root, monkeypatch):
    build = _ready_build(client, "Hash cap")
    bid = build["id"]
    big = data_root / "downloads" / "big-scan.pdf"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * 100)
    monkeypatch.setattr(server, "_MANIFEST_HASH_CAP", 50)

    def no_hashing(_path):  # pragma: no cover — failure branch
        raise AssertionError("a file above the cap must not be hashed")
    monkeypatch.setattr(server, "_file_sha256", no_hashing)

    ref = server._manifest_input(bid, "pdf:primary", path=big)
    assert "sha256" not in ref
    assert ref["size"] == 100 and "mtime" in ref

    _write_compiled(bid, {1: "y" * 80})    # above the lowered cap too
    server._manifest_record(bid, "ocr/compiled.txt",
                            {"kind": "ocr", "engine": "tesseract"}, [ref])
    row = _row(bid, "ocr/compiled.txt")
    assert "sha256" not in row and row["size"] > 50
    assert server._manifest_inputs_stale(bid, "ocr/compiled.txt") == []

    big.write_bytes(b"x" * 120)            # content changed: size differs
    assert server._manifest_inputs_stale(bid, "ocr/compiled.txt") == \
        ["pdf:primary"]


# --- sync --------------------------------------------------------------------------

def test_manifest_and_verbatim_ride_entry_file_sync(client):
    build = _ready_build(client, "Sync coverage")
    bid = build["id"]
    client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt", "text": "one"})
    client.post(f"/api/builds/{bid}/ocr", json={
        "name": "compiled.txt", "text": "two"})   # creates the verbatim copy
    local = ss.local_entry_files()
    assert f"{bid}/manifest.json" in local
    assert f"{bid}/ocr/verbatim/compiled.txt" in local
