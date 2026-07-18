"""Trash store: page deletion writes a recoverable item, restore puts it back.

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so importing server never touches live data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import server


T3 = "--- page 1 ---\nalpha\n\n--- page 2 ---\nbravo\n\n--- page 3 ---\ncharlie"


def _make_pdf(path: Path, n_pages: int) -> None:
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(n_pages):
        pg = doc.new_page(width=200, height=200)
        pg.insert_text((50, 100), f"PAGE {i + 1}")
    doc.save(str(path))
    doc.close()


def _page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def _page_text(path: Path, index: int) -> str:
    import fitz

    doc = fitz.open(str(path))
    try:
        return doc[index].get_text().strip()
    finally:
        doc.close()


def _seed(bid: str, data_root: Path, n_pages: int = 3) -> Path:
    """A book with a PDF, OCR text and a layout sidecar, registered on disk."""
    pdf = data_root / "downloads" / "ia" / bid / "book.pdf"
    _make_pdf(pdf, n_pages)
    ocr_dir = server._entry_dir(bid) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "compiled.txt").write_text(T3, encoding="utf-8")
    server.lib.save_json(ocr_dir / "layout.json", {
        "words": {"primary": {"1": ["one"], "2": ["two"], "3": ["three"]}},
    })
    builds = {bid: {"title": "Trashy", "title_pages": "1,3",
                    "pdf_file": str(pdf)}}
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")
    return pdf


@pytest.fixture()
def client():
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def test_delete_writes_a_restorable_item(data_root):
    """The deleted page, a full pre-image and whole copies of every rewritten
    collateral file land in ONE trash item, and the legacy .bak siblings are
    gone rather than duplicated."""
    bid = "trash001"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    result = server._apply_page_deletion(bid, builds, pdf, [2])
    tid = result["trash_id"]
    tdir = server.TRASH_DIR / tid

    assert _page_count(pdf) == 2
    assert _page_count(tdir / "pages.pdf") == 1
    assert "PAGE 2" in _page_text(tdir / "pages.pdf", 0)
    assert _page_count(tdir / "original.pdf") == 3
    assert (tdir / "ocr" / "compiled.txt").read_text(encoding="utf-8") == T3
    assert (tdir / "ocr" / "layout.json").is_file()
    # the five ad-hoc backups are RETIRED, not supplemented
    assert not pdf.with_suffix(".bak.pdf").exists()
    assert not (server._entry_dir(bid) / "ocr" / "compiled.txt.bak").exists()

    index = server.lib.load_json(server.TRASH_PATH, {})
    rec = index["items"][tid]
    assert rec["kind"] == "pdf_pages"
    assert rec["restore"]["pages"] == [2]
    assert rec["restore"]["pages_before"] == 3
    assert rec["restore"]["pages_after"] == 2
    assert rec["restore"]["title_pages_before"] == "1,3"
    assert rec["bytes"] > 0
    assert "2 page" not in rec["label"] and "1 page" in rec["label"]


def test_restore_puts_the_page_back_in_position(data_root, client):
    """Restore reinserts at the ORIGINAL index (not appended at the end) and
    writes the collateral snapshots back verbatim."""
    bid = "trash002"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]

    ocr = server._entry_dir(bid) / "ocr" / "compiled.txt"
    assert ocr.read_text(encoding="utf-8") != T3          # renumbered by delete

    r = client.post("/api/trash/restore", json={"id": tid})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] and body["pages"] == 3

    assert _page_count(pdf) == 3
    assert "PAGE 2" in _page_text(pdf, 1)                 # back in the MIDDLE
    assert ocr.read_text(encoding="utf-8") == T3          # verbatim write-back
    assert server.lib.load_json(server.BUILDS_PATH, {})[bid]["title_pages"] == "1,3"

    index = server.lib.load_json(server.TRASH_PATH, {})
    assert index["items"][tid]["restored_at"]             # row kept, marked


def test_restore_refuses_when_the_pdf_moved_on(data_root, client):
    """The recorded indices only mean anything against the exact post-delete
    page count — a second delete must make restore refuse, not scramble."""
    bid = "trash003"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    server._apply_page_deletion(bid, builds, pdf, [1])     # now 1 page, not 2

    r = client.post("/api/trash/restore", json={"id": tid})
    assert r.status_code == 409
    assert "changed since" in r.get_json()["error"]
    assert _page_count(pdf) == 1                           # untouched by refusal
    # the payload is still downloadable so the pages are not lost
    d = client.get(f"/api/trash/{tid}/payload/pages.pdf")
    assert d.status_code == 200 and d.data[:4] == b"%PDF"


def test_restore_keeps_a_file_edited_since_the_delete(data_root, client):
    """A snapshot is written back only when the live file is untouched;
    otherwise it is reported as skipped rather than silently overwritten."""
    bid = "trash004"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]

    ocr = server._entry_dir(bid) / "ocr" / "compiled.txt"
    ocr.write_text("my later edit", encoding="utf-8")

    body = client.post("/api/trash/restore", json={"id": tid}).get_json()
    assert body["ok"]
    assert ocr.read_text(encoding="utf-8") == "my later edit"    # NOT clobbered
    assert any(s["file"] == "ocr/compiled.txt" for s in body["skipped"])


def test_payload_download_refuses_traversal(data_root, client):
    bid = "trash005"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]

    for rel in ("../../whl_builds.json", "..%2F..%2Fwhl_builds.json",
                "../index.json"):
        assert client.get(f"/api/trash/{tid}/payload/{rel}").status_code == 404
    assert client.post("/api/trash/restore",
                       json={"id": "../../etc"}).status_code == 400


def test_list_and_forget(data_root, client):
    bid = "trash006"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]

    listing = client.get("/api/trash").get_json()
    assert listing["ok"] and any(i["id"] == tid for i in listing["items"])
    assert listing["summary"]["count"] >= 1
    assert listing["summary"]["keep_days"] == server._TRASH_KEEP_DAYS

    assert client.post("/api/trash/forget", json={"id": tid}).get_json()["ok"]
    assert not (server.TRASH_DIR / tid).exists()
    assert tid not in server.lib.load_json(server.TRASH_PATH, {})["items"]


def test_prune_drops_old_items_but_never_the_fresh_one(data_root):
    """Age/count caps prune, but an item younger than the floor survives even
    when it alone blows a cap — the 'oh no' undo must always work."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(
        timespec="seconds")
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    doc = {"version": 1, "items": {
        "aaa": {"id": "aaa", "created": old, "bytes": 10},
        "bbb": {"id": "bbb", "created": fresh, "bytes": 5 << 30},   # > cap alone
    }}
    server.TRASH_DIR.mkdir(parents=True, exist_ok=True)
    server._trash_prune_locked(doc)
    assert "aaa" not in doc["items"]          # past the age cap
    assert "bbb" in doc["items"]              # inside the floor, kept regardless


# --- other adopters: records and translations --------------------------------

def test_build_delete_is_recoverable(data_root, client):
    """Deleting an entry trashes its record; restoring reinserts it verbatim,
    and refuses if something has taken the id back."""
    bid = "trashb01"
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(
        json.dumps({bid: {"id": bid, "title": "Doomed", "rights": "public-domain"}}),
        encoding="utf-8")

    assert client.delete(f"/api/builds/{bid}").status_code == 200
    assert bid not in server.lib.load_json(server.BUILDS_PATH, {})

    items = client.get("/api/trash").get_json()["items"]
    rec = next(i for i in items
               if i["kind"] == "build" and i["origin"].get("build_id") == bid)
    assert "Doomed" in rec["label"]

    r = client.post("/api/trash/restore", json={"id": rec["id"]})
    assert r.status_code == 200, r.get_json()
    back = server.lib.load_json(server.BUILDS_PATH, {})[bid]
    assert back["title"] == "Doomed" and back["rights"] == "public-domain"

    # a second restore must not overwrite the entry now living at that id
    again = client.post("/api/trash/restore", json={"id": rec["id"]})
    assert again.status_code == 409
    assert "exists again" in again.get_json()["error"]


def test_manual_delete_is_recoverable(data_root, client):
    eid = "trashm01"
    server.lib.MANUAL_ENTRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH,
                         {eid: {"id": eid, "title": "Hand typed"}})

    assert client.delete(f"/api/manual/{eid}").status_code == 200
    rec = next(i for i in client.get("/api/trash").get_json()["items"]
               if i["kind"] == "manual_entry"
               and i["origin"].get("entry_id") == eid)
    assert client.post("/api/trash/restore", json={"id": rec["id"]}).status_code == 200
    assert server.lib.load_json(server.lib.MANUAL_ENTRIES_PATH, {})[eid]["title"] \
        == "Hand typed"


def test_translation_delete_is_recoverable(data_root, client):
    """A translation costs a paid model run to regenerate, so deleting one
    keeps the text; restore refuses to overwrite a newer translation."""
    bid = "trasht01"
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(
        # _an_gate only lets verified entries through
        json.dumps({bid: {"id": bid, "title": "Translated", "status": "ready"}}),
        encoding="utf-8")
    tdir = server._entry_dir(bid) / "translations"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "fr.txt").write_text("--- page 1 ---\nbonjour", encoding="utf-8")

    assert client.delete(f"/api/builds/{bid}/translations/fr").status_code == 200
    assert not (tdir / "fr.txt").exists()

    rec = next(i for i in client.get("/api/trash").get_json()["items"]
               if i["kind"] == "translation"
               and i["origin"].get("build_id") == bid)
    assert client.post("/api/trash/restore", json={"id": rec["id"]}).status_code == 200
    assert (tdir / "fr.txt").read_text(encoding="utf-8") == "--- page 1 ---\nbonjour"

    # a newer translation in place is never clobbered
    (tdir / "fr.txt").write_text("newer", encoding="utf-8")
    again = client.post("/api/trash/restore", json={"id": rec["id"]})
    assert again.status_code == 409
    assert (tdir / "fr.txt").read_text(encoding="utf-8") == "newer"


def test_secondary_source_deletion_targets_that_source(data_root, client):
    """A deletion applied to a SECONDARY scan must record that scan, not the
    build's primary. Recording the primary made restore splice the secondary's
    held pages into the primary and write the result over it — silent
    corruption of the file the user never touched."""
    bid = "trash009"
    primary = _seed(bid, data_root)
    secondary = data_root / "downloads" / "ia" / bid / "second.pdf"
    _make_pdf(secondary, 4)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds[bid]["pdf_sources"] = [{"id": "s2", "path": str(secondary)}]
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    result = server._apply_page_deletion(bid, builds, secondary, [2])
    tid = result["trash_id"]
    item = server.lib.load_json(server.TRASH_PATH, {})["items"][tid]
    assert item["origin"]["src_key"] == "s2"
    assert Path(item["origin"]["pdf"]).resolve() == secondary.resolve()

    # the primary is untouched by both the delete and the restore
    assert _page_count(primary) == 3
    assert _page_count(secondary) == 3
    assert client.post("/api/trash/restore", json={"id": tid}).get_json()["ok"]
    assert _page_count(primary) == 3
    assert _page_count(secondary) == 4
    assert "PAGE 2" in _page_text(secondary, 1)


def test_restore_puts_translations_back(data_root, client):
    """Translations renumber with the PDF, so they must round-trip with it.
    Without a snapshot the restore rebuilt the pages and left every translated
    page past the deletion shifted by one, reporting ok with nothing skipped."""
    bid = "trash010"
    pdf = _seed(bid, data_root)
    tdir = server._entry_dir(bid) / "translations"
    tdir.mkdir(parents=True, exist_ok=True)
    before = "--- page 1 ---\nuno\n\n--- page 2 ---\ndos\n\n--- page 3 ---\ntres"
    (tdir / "es.txt").write_text(before, encoding="utf-8")
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    assert (tdir / "es.txt").read_text(encoding="utf-8") == \
        "--- page 1 ---\nuno\n\n--- page 2 ---\ntres"

    body = client.post("/api/trash/restore", json={"id": tid}).get_json()
    assert body["ok"], body
    assert "translations/es.txt" in body["restored"]
    assert (tdir / "es.txt").read_text(encoding="utf-8") == before


def test_restore_does_not_clobber_a_later_title_page_edit(data_root, client):
    """title_pages is only written back when it still holds what the delete
    left. A hand edit afterwards is reported, not overwritten."""
    bid = "trash011"
    pdf = _seed(bid, data_root)          # title_pages "1,3"
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    assert server.lib.load_json(server.BUILDS_PATH, {})[bid]["title_pages"] == "1,2"

    server._builds_apply(bid, {"title_pages": "2"})
    body = client.post("/api/trash/restore", json={"id": tid}).get_json()
    assert body["ok"]
    assert server.lib.load_json(server.BUILDS_PATH, {})[bid]["title_pages"] == "2"
    assert any(s["file"] == "title_pages" for s in body["skipped"])


def test_retired_item_is_download_only(data_root, client):
    """When the file a row would restore INTO is gone, the row keeps the
    deleted pages but drops the pre-image and refuses to restore, rather than
    sitting there promising an undo that can only fail."""
    bid = "trash012"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    assert (server.TRASH_DIR / tid / "original.pdf").is_file()

    server._trash_retire(tid, "the original was a temporary download")
    item = server.lib.load_json(server.TRASH_PATH, {})["items"][tid]
    assert item["restorable"] is False
    assert "original.pdf" not in item["files"]
    assert not (server.TRASH_DIR / tid / "original.pdf").exists()
    # bytes re-summed, so a dead row stops holding the cap hostage
    assert item["bytes"] == server._trash_dir_bytes(server.TRASH_DIR / tid)

    res = client.post("/api/trash/restore", json={"id": tid})
    assert res.status_code == 409
    # the pages themselves are still there to download
    assert client.get(f"/api/trash/{tid}/payload/pages.pdf").status_code == 200


def test_forget_leaves_an_in_flight_restore_alone(data_root, client):
    """Empty-trash must not pull a payload out from under a restore that is
    mid-rewrite — that leaves the PDF restored but the OCR still renumbered,
    reported as a success."""
    bid = "trash013"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]

    server._trash_restoring.add(tid)
    try:
        body = client.post("/api/trash/forget", json={"all": True}).get_json()
    finally:
        server._trash_restoring.discard(tid)
    assert body["busy"] >= 1
    assert (server.TRASH_DIR / tid / "pages.pdf").is_file()
    assert tid in server.lib.load_json(server.TRASH_PATH, {})["items"]


def test_restore_brings_a_deleted_pages_figure_back(data_root, client):
    """A figure extracted from a deleted page must come back WITH the layout
    metadata that names it. It used to be copied to an ocr/images/
    .page-delete-backup dead-drop nothing read, so a restore put layout.json
    back pointing at a file that was no longer there."""
    bid = "trash014"
    pdf = _seed(bid, data_root)
    images = server._entry_dir(bid) / "ocr" / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "p2-fig.jpeg").write_bytes(b"\xff\xd8fig")
    (images / "p3-fig.jpeg").write_bytes(b"\xff\xd8keep")
    layout = server.lib.load_json(server._entry_dir(bid) / "ocr" / "layout.json", {})
    layout["images"] = {"p2-fig.jpeg": {"page": 2}, "p3-fig.jpeg": {"page": 3}}
    server.lib.save_json(server._entry_dir(bid) / "ocr" / "layout.json", layout)
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    assert not (images / "p2-fig.jpeg").exists()
    assert (server.TRASH_DIR / tid / "ocr" / "images" / "p2-fig.jpeg").is_file()
    after = server.lib.load_json(server._entry_dir(bid) / "ocr" / "layout.json", {})
    assert "p2-fig.jpeg" not in after["images"]
    assert after["images"]["p3-fig.jpeg"]["page"] == 2

    assert client.post("/api/trash/restore", json={"id": tid}).get_json()["ok"]
    assert (images / "p2-fig.jpeg").read_bytes() == b"\xff\xd8fig"
    back = server.lib.load_json(server._entry_dir(bid) / "ocr" / "layout.json", {})
    assert back["images"]["p2-fig.jpeg"]["page"] == 2     # metadata agrees again


def test_a_failing_tail_still_leaves_an_honest_row(data_root, client, monkeypatch):
    """The pages are gone from disk the moment the PDF is rewritten, so the row
    must describe everything the payload holds even if the collateral tail dies
    partway. A row frozen at the early commit made restore iterate a short
    files list, write nothing back, and report ok with an EMPTY skipped list —
    a book with its pages back and its OCR still renumbered, presented as a
    working undo."""
    bid = "trash015"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    def boom(*a, **k):
        raise RuntimeError("sidecar is corrupt")

    # the attention remap is the one derivative step with no try/except of its
    # own, and it runs last — so this is the failure that actually escapes,
    # after every snapshot has been taken
    monkeypatch.setattr(server, "_remap_page_attention_references", boom)
    with pytest.raises(RuntimeError):
        server._apply_page_deletion(bid, builds, pdf, [2])

    index = server.lib.load_json(server.TRASH_PATH, {})
    tid = next(k for k, v in index["items"].items()
               if (v.get("origin") or {}).get("build_id") == bid)
    rec = index["items"][tid]
    # the snapshots taken before the failure are ON the row, not just on disk
    assert "ocr/compiled.txt" in rec["files"]
    assert "ocr/layout.json" in rec["files"]
    # and the OCR guard sees the shorter PDF even though the call raised
    assert server._page_structure_revision.get(bid, 0) > 0

    ocr = server._entry_dir(bid) / "ocr" / "compiled.txt"
    assert ocr.read_text(encoding="utf-8") != T3        # renumbered by the delete
    body = client.post("/api/trash/restore", json={"id": tid}).get_json()
    assert body["ok"], body
    assert _page_count(pdf) == 3
    assert ocr.read_text(encoding="utf-8") == T3        # and put back, not stranded


def test_restore_will_not_overwrite_a_figure_added_after_the_delete(
        data_root, client):
    """A figure the delete REMOVED gets an empty stamp, which means 'absent at
    delete time'. If something now occupies that path it arrived afterwards —
    a re-run of figure extraction, say — and must be reported, not clobbered."""
    bid = "trash016"
    pdf = _seed(bid, data_root)
    images = server._entry_dir(bid) / "ocr" / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "p2-fig.jpeg").write_bytes(b"\xff\xd8original")
    lp = server._entry_dir(bid) / "ocr" / "layout.json"
    layout = server.lib.load_json(lp, {})
    layout["images"] = {"p2-fig.jpeg": {"page": 2}}
    server.lib.save_json(lp, layout)
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    (images / "p2-fig.jpeg").write_bytes(b"\xff\xd8a DIFFERENT figure")

    body = client.post("/api/trash/restore", json={"id": tid}).get_json()
    assert body["ok"]
    assert (images / "p2-fig.jpeg").read_bytes() == b"\xff\xd8a DIFFERENT figure"
    assert any(s["file"] == "ocr/images/p2-fig.jpeg" for s in body["skipped"])


def test_delete_refuses_a_pdf_from_another_entry(data_root, client):
    """Nothing checked that the posted PDF belongs to the posted entry, so a
    mismatched pair renumbered A's OCR against B's pages and wrote a trash row
    pointing at A's primary — restoring it spliced B's pages into A."""
    a, b = "trash017a", "trash017b"
    pdf_a = _seed(a, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    pdf_b = data_root / "downloads" / "ia" / b / "book.pdf"
    _make_pdf(pdf_b, 5)
    builds[b] = {"title": "Other", "pdf_file": str(pdf_b)}
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    r = client.post("/api/pdf/pages/delete",
                    json={"build_id": a, "pdf": str(pdf_b), "pages": [2],
                          "page_revision": "unversioned"})
    assert r.status_code == 409
    assert r.get_json()["conflict"] == "pdf_not_attached"
    assert _page_count(pdf_a) == 3 and _page_count(pdf_b) == 5   # both untouched


def _ref(bid, page, source="primary"):
    return f"page:{bid}|{source}|{page}"


def test_restore_puts_page_marks_and_threads_back(data_root, client):
    """The delete shifts attention marks and review threads onto the new
    numbering and tombstones the ones whose page went away. A restore that
    rebuilt the pages and the text but left these one page off would be a
    half-restore reported as a success."""
    bid = "trash018"
    pdf = _seed(bid, data_root)
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {"attention": {
        _ref(bid, 1): {"label": "kept · page 1"},
        _ref(bid, 2): {"label": "doomed · page 2"},
        _ref(bid, 3): {"label": "shifts · page 3"},
    }})
    server.lib.save_json(server.REVIEWS_PATH, {
        "r-keep": {"id": "r-keep", "kind": "key", "ref": _ref(bid, 3),
                   "label": "Thread · Page 3"},
        "r-gone": {"id": "r-gone", "kind": "key", "ref": _ref(bid, 2),
                   "label": "Thread · Page 2"},
    })
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    att = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})["attention"]
    # page 2's mark is dropped and page 3's shifts INTO key 2 behind it
    assert set(att) == {_ref(bid, 1), _ref(bid, 2)}
    assert att[_ref(bid, 2)]["label"].startswith("shifts")
    reviews = server.lib.load_json(server.REVIEWS_PATH, {})
    assert reviews["r-keep"]["ref"] == _ref(bid, 2)      # 3 -> 2
    assert reviews["r-gone"]["ref"].startswith("page-deleted:")

    assert client.post("/api/trash/restore", json={"id": tid}).get_json()["ok"]

    att = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})["attention"]
    assert att[_ref(bid, 1)]["label"] == "kept · page 1"
    assert att[_ref(bid, 3)]["label"].endswith("page 3")   # shifted back
    assert att[_ref(bid, 2)]["label"] == "doomed · page 2"  # dropped, restored
    reviews = server.lib.load_json(server.REVIEWS_PATH, {})
    assert reviews["r-keep"]["ref"] == _ref(bid, 3)        # back to 3
    assert reviews["r-gone"]["ref"] == _ref(bid, 2)        # un-tombstoned
    assert not reviews["r-gone"]["label"].endswith(" · removed")


def test_restore_advances_the_page_revision_token(data_root, client):
    """A restore changes the page grid exactly as a delete does, and the grid
    is what page_revision guards. Leaving the token untouched let a client
    holding the post-delete token delete against numbering that had silently
    reverted — hitting a different physical page with no conflict raised."""
    bid = "trash019"
    pdf = _seed(bid, data_root)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    builds[bid].pop("title_pages", None)     # so the delete's `changed` is {}
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]
    after_delete = server.lib.load_json(
        server.BUILDS_PATH, {})[bid].get("updated_at")

    assert client.post("/api/trash/restore", json={"id": tid}).get_json()["ok"]
    after_restore = server.lib.load_json(
        server.BUILDS_PATH, {})[bid].get("updated_at")
    assert after_restore and after_restore != after_delete

    # a client still holding the post-delete token must now be refused
    r = client.post("/api/pdf/pages/delete",
                    json={"build_id": bid, "pdf": str(pdf), "pages": [2],
                          "page_revision": str(after_delete or "")})
    assert r.status_code == 409
    assert r.get_json()["conflict"] == "stale_page_revision"
    assert _page_count(pdf) == 3                      # nothing deleted


def test_restore_only_revives_its_own_tombstoned_threads(data_root, client):
    """A tombstone is identified only by (build, source, page), so restoring
    one delete used to resurrect every thread ever tombstoned on that page
    number — stranding an older thread on another page's content and leaving
    two threads sharing a key, where re-flagging the page silently edits
    whichever one dict order happens to yield."""
    bid = "trash020"
    pdf = _seed(bid, data_root, n_pages=6)
    server.lib.save_json(server.REVIEWS_PATH, {
        "r-old": {"id": "r-old", "kind": "key", "ref": _ref(bid, 3),
                  "label": "Old thread · Page 3"},
        "r-new": {"id": "r-new", "kind": "key", "ref": _ref(bid, 4),
                  "label": "New thread · Page 4"},
    })
    builds = server.lib.load_json(server.BUILDS_PATH, {})

    server._apply_page_deletion(bid, builds, pdf, [3])       # r-old tombstoned
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    second = server._apply_page_deletion(bid, builds, pdf, [3])  # r-new likewise
    reviews = server.lib.load_json(server.REVIEWS_PATH, {})
    assert reviews["r-old"]["ref"].startswith("page-deleted:")
    assert reviews["r-new"]["ref"].startswith("page-deleted:")

    body = client.post("/api/trash/restore",
                       json={"id": second["trash_id"]}).get_json()
    assert body["ok"], body
    reviews = server.lib.load_json(server.REVIEWS_PATH, {})
    assert reviews["r-new"]["ref"] == _ref(bid, 3)           # this row's thread
    assert reviews["r-old"]["ref"].startswith("page-deleted:")  # NOT this one's
    assert reviews["r-old"]["label"].endswith(" · removed")


def test_restore_will_not_overwrite_a_mark_already_in_the_way(data_root, client):
    """A key above the post-delete page count has no pre-delete original, so it
    never moves — but it is a valid TARGET for a mark shifting back, and
    writing over it would delete a mark while reporting success."""
    bid = "trash021"
    pdf = _seed(bid, data_root, n_pages=4)
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [1])["trash_id"]

    # a stale client flushes a pre-delete map: page 4 exists again in the blob
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {"attention": {
        _ref(bid, 3): {"label": "shifts back to 4"},
        _ref(bid, 4): {"label": "already sitting at 4"},
    }})

    body = client.post("/api/trash/restore", json={"id": tid}).get_json()
    assert body["ok"]
    att = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})["attention"]
    assert att[_ref(bid, 4)]["label"] == "already sitting at 4"   # NOT clobbered
    assert any("attention mark" in str(s.get("reason") or "")
               for s in body["skipped"])


def test_restore_recreates_a_vanished_attention_map(data_root, client):
    """The live map can disappear entirely (a client PUT replaces `settings`
    wholesale). Skipping the bucket silently discarded the payload and still
    reported success, and the row was then marked restored — so the mark was
    gone for good."""
    bid = "trash022"
    pdf = _seed(bid, data_root)
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {"attention": {
        _ref(bid, 2): {"label": "doomed"}}})
    builds = server.lib.load_json(server.BUILDS_PATH, {})
    tid = server._apply_page_deletion(bid, builds, pdf, [2])["trash_id"]

    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {})   # map vanishes

    assert client.post("/api/trash/restore", json={"id": tid}).get_json()["ok"]
    att = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {}).get("attention")
    assert att and att[_ref(bid, 2)]["label"] == "doomed"
