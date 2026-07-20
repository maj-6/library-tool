"""Corrected OCR pages must be re-translatable and annotation anchors must
survive text edits visibly (issue #136): translations record which source
text (by hash) each page came from, staleness is reported, and an OCR edit
flags notes whose quote no longer matches instead of failing silently."""
from __future__ import annotations

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
    """Echo translator: replies with the page text it was asked to translate,
    prefixed, so tests can tell which source text produced each page."""
    calls: list = []

    def fake_ai_chat(_cfg, messages, **_kwargs):
        calls.append(messages)
        return "T::" + messages[-1]["content"].split("\n\n", 1)[1]

    def run_inline(bid, kind, total, target, decorate=None):
        job = server._an_job_new(bid, kind, total)
        if decorate is not None:
            decorate(job)
        target(job)
        return job

    monkeypatch.setattr(server, "_ai_cfg", lambda: {
        "base": "https://example.test/v1", "model": "test-model"})
    monkeypatch.setattr(server, "_secret_is_configured",
                        lambda key: key == "aiKey")
    monkeypatch.setattr(server, "_ai_chat", fake_ai_chat)
    monkeypatch.setattr(server, "_an_job_start", run_inline)
    return calls


def _translated_pages(bid: str, lang: str) -> dict[int, str]:
    return server._an_pages(server._read_entry_text(
        bid, f"translations/{lang}.txt"))


def test_translate_records_source_hashes_and_reports_current(client, monkeypatch):
    build = _ready_build(client, "Hash recording")
    _write_compiled(build["id"], {1: "Alpha original.", 2: "Beta original."})
    _install_inline_ai(monkeypatch)

    r = client.post("/api/analyze/translate",
                    json={"build_id": build["id"], "lang": "es"})
    assert r.status_code == 200

    meta = server._load_translation_meta(build["id"], "es")
    assert meta["src"] == "compiled.txt"
    assert meta["model"] == "test-model"
    assert set(meta["pages"]) == {"1", "2"}
    assert meta["pages"]["1"]["sha1"] == server._page_sha("Alpha original.")

    info = client.get(
        f"/api/builds/{build['id']}/translations").get_json()["translations"]
    assert info == [{"lang": "es", "pages": 2, "size": info[0]["size"],
                     "stale": 0, "untracked": 0}]

    # nothing to do: both the default run and the stale mode say so
    again = client.post("/api/analyze/translate",
                        json={"build_id": build["id"], "lang": "es"})
    assert again.status_code == 400
    assert "already translated" in again.get_json()["error"]
    stale = client.post("/api/analyze/translate",
                        json={"build_id": build["id"], "lang": "es",
                              "mode": "stale"})
    assert stale.status_code == 400
    assert "current" in stale.get_json()["error"]


def test_corrected_page_reports_stale_and_updates_only_that_page(
        client, monkeypatch):
    build = _ready_build(client, "Stale refresh")
    _write_compiled(build["id"], {1: "Alpha original.", 2: "Beta original."})
    calls = _install_inline_ai(monkeypatch)
    client.post("/api/analyze/translate",
                json={"build_id": build["id"], "lang": "es"})
    assert _translated_pages(build["id"], "es") == {
        1: "T::Alpha original.", 2: "T::Beta original."}

    # correct page 1 through the OCR editor's save route
    r = client.post(f"/api/builds/{build['id']}/ocr", json={
        "name": "compiled.txt",
        "text": "--- page 1 ---\nAlpha corrected reading.\n\n"
                "--- page 2 ---\nBeta original.\n"})
    assert r.status_code == 200

    info = client.get(
        f"/api/builds/{build['id']}/translations").get_json()["translations"]
    assert info[0]["stale"] == 1

    calls.clear()
    r = client.post("/api/analyze/translate",
                    json={"build_id": build["id"], "lang": "es",
                          "mode": "stale"})
    assert r.status_code == 200
    assert len(calls) == 1                       # only the corrected page
    assert _translated_pages(build["id"], "es") == {
        1: "T::Alpha corrected reading.", 2: "T::Beta original."}
    info = client.get(
        f"/api/builds/{build['id']}/translations").get_json()["translations"]
    assert info[0]["stale"] == 0


def test_explicit_page_list_retranslates_current_pages(client, monkeypatch):
    build = _ready_build(client, "Explicit pages")
    _write_compiled(build["id"], {1: "Alpha original.", 2: "Beta original."})
    calls = _install_inline_ai(monkeypatch)
    client.post("/api/analyze/translate",
                json={"build_id": build["id"], "lang": "es"})

    calls.clear()
    r = client.post("/api/analyze/translate",
                    json={"build_id": build["id"], "lang": "es",
                          "pages": [2]})
    assert r.status_code == 200
    assert len(calls) == 1
    assert "Beta original." in calls[0][-1]["content"]

    r = client.post("/api/analyze/translate",
                    json={"build_id": build["id"], "lang": "es",
                          "pages": [99]})
    assert r.status_code == 400
    r = client.post("/api/analyze/translate",
                    json={"build_id": build["id"], "lang": "es",
                          "pages": ["x"]})
    assert r.status_code == 400


def test_legacy_translation_without_meta_is_untracked_not_stale(client):
    build = _ready_build(client, "Legacy translation")
    _write_compiled(build["id"], {1: "Alpha original."})
    d = server._entry_dir(build["id"]) / "translations"
    d.mkdir(parents=True, exist_ok=True)
    (d / "es.txt").write_text("--- page 1 ---\nVieja.\n", encoding="utf-8")

    info = client.get(
        f"/api/builds/{build['id']}/translations").get_json()["translations"]
    assert info[0]["stale"] == 0
    assert info[0]["untracked"] == 1

    refresh = client.post("/api/analyze/translate", json={
        "build_id": build["id"], "lang": "es", "mode": "stale"})
    assert refresh.status_code == 409
    assert refresh.get_json()["untracked"] == [1]


def test_translation_hash_preserves_paragraph_semantics():
    wrapped = server._page_source_hash("alpha\nline\n\nbeta")
    assert wrapped == server._page_source_hash("alpha line\n\nbeta")
    assert wrapped != server._page_source_hash("alpha line beta")


def test_source_layer_change_marks_every_tracked_page_stale():
    text = "same words"
    meta = {"src": "compiled.txt", "pages": {"1": {
        "source_hash": server._page_source_hash(text)}}}

    assert server._stale_translation_pages(
        meta, {1: text}, "normalized.txt") == [1]


def test_imported_translation_drops_inherited_local_provenance(client):
    build = _ready_build(client, "Imported translation provenance")
    _write_compiled(build["id"], {1: "Alpha original.", 2: "Beta original."})
    d = server._entry_dir(build["id"]) / "translations"
    d.mkdir(parents=True, exist_ok=True)
    (d / "es.txt").write_text(
        "--- page 1 ---\nUno.\n\n--- page 2 ---\nDos.\n", encoding="utf-8")
    server.lib.save_json(server._translation_meta_path(build["id"], "es"), {
        "version": 2, "src": "compiled.txt", "model": "local-model",
        "pages": {
            "1": {"source_hash": server._page_source_hash("Alpha original.")},
            "2": {"source_hash": server._page_source_hash("Beta original.")},
        }})

    assert server._lib_apply_translations(
        build["id"], {"es": {1: "Importado."}}, overwrite=True) == ["es"]
    meta = server._load_translation_meta(build["id"], "es")
    assert "1" not in meta["pages"]
    assert "2" in meta["pages"]
    info = client.get(
        f"/api/builds/{build['id']}/translations").get_json()["translations"][0]
    assert info["untracked"] == 1


def test_deleting_a_translation_removes_its_meta(client, monkeypatch):
    build = _ready_build(client, "Delete meta")
    _write_compiled(build["id"], {1: "Alpha original."})
    _install_inline_ai(monkeypatch)
    client.post("/api/analyze/translate",
                json={"build_id": build["id"], "lang": "es"})
    assert server._translation_meta_path(build["id"], "es").is_file()

    r = client.delete(f"/api/builds/{build['id']}/translations/es")
    assert r.status_code == 200
    assert not server._translation_meta_path(build["id"], "es").is_file()


def test_ocr_edit_flags_and_recovers_orphaned_note_anchors(client):
    build = _ready_build(client, "Anchor orphans")
    _write_compiled(build["id"], {1: "The rose is red indeed."})
    server.lib.save_json(server._entry_dir(build["id"]) / "annotations.json", {
        "version": 1, "notes": [{
            "id": "n1", "page": 1, "quote": "rose is red", "kind": "context",
            "body": "colour note", "status": "suggested"}]})

    # an edit that removes the quoted text orphans the anchor
    client.post(f"/api/builds/{build['id']}/ocr", json={
        "name": "compiled.txt",
        "text": "--- page 1 ---\nThe tulip is yellow indeed.\n"})
    notes = client.get(
        f"/api/builds/{build['id']}/annotations").get_json()["doc"]["notes"]
    assert notes[0]["anchor"] == "orphaned"

    # restoring the text recovers it
    client.post(f"/api/builds/{build['id']}/ocr", json={
        "name": "compiled.txt",
        "text": "--- page 1 ---\nThe rose is red indeed.\n"})
    notes = client.get(
        f"/api/builds/{build['id']}/annotations").get_json()["doc"]["notes"]
    assert notes[0]["anchor"] == "ok"

    # saving some other document never touches the anchors
    client.post(f"/api/builds/{build['id']}/ocr", json={
        "name": "draft.txt", "text": "unrelated scratch text"})
    notes = client.get(
        f"/api/builds/{build['id']}/annotations").get_json()["doc"]["notes"]
    assert notes[0]["anchor"] == "ok"
