"""Characterization tests for page deletion in tools/whl_explorer/server.py.

Pins the CURRENT behavior of _renumber_marked_text (the "--- page N ---"
marker remapper) and _apply_page_deletion (PDF rewrite + OCR renumber +
title_pages remap), exactly as observed. Several behaviors look buggy —
duplicate removed values double-shift into colliding markers, near-miss
markers survive as body text next to renumbered real ones, out-of-range
pages are reported as deleted — and are pinned as-is; nothing here asserts
what the code *should* do, only what it does.

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so importing server below never touches live data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import server


T3 = "--- page 1 ---\nalpha\n\n--- page 2 ---\nbravo\n\n--- page 3 ---\ncharlie"


# --- _renumber_marked_text goldens -------------------------------------------

@pytest.mark.parametrize(
    ("text", "removed", "expected"),
    [
        pytest.param(
            T3, [2],
            "--- page 1 ---\nalpha\n\n--- page 2 ---\ncharlie",
            id="middle"),
        pytest.param(
            T3, [1],
            "--- page 1 ---\nbravo\n\n--- page 2 ---\ncharlie",
            id="first"),
        pytest.param(
            T3, [3],
            "--- page 1 ---\nalpha\n\n--- page 2 ---\nbravo",
            id="last"),
        pytest.param(
            T3, [1, 3],
            "--- page 1 ---\nbravo",
            id="multi"),
        pytest.param(
            "just plain text\nno markers here", [1],
            "just plain text\nno markers here",
            id="no-markers-returns-input-unchanged"),
        pytest.param(
            "PREAMBLE line\n\n--- page 1 ---\nalpha\n\n--- page 2 ---\nbravo",
            [1],
            "PREAMBLE line\n\n--- page 1 ---\nbravo",
            id="preamble-kept"),
        # A double-space "marker" does not match the regex: it survives as
        # preamble text while the real page 3 shifts down to 2, producing a
        # lookalike duplicate "--- page 2 ---". Pinned as-is.
        pytest.param(
            "---  page 2 ---\nx\n\n--- page 3 ---\ny", [2],
            "---  page 2 ---\nx\n\n--- page 2 ---\ny",
            id="double-space-marker-no-match"),
        # Trailing space defeats the $ anchor; the near-miss marker stays as
        # body text while page 2 renumbers to 1 anyway. Pinned as-is.
        pytest.param(
            "--- page 1 --- \nalpha\n\n--- page 2 ---\nbravo", [1],
            "--- page 1 --- \nalpha\n\n--- page 1 ---\nbravo",
            id="trailing-space-marker-no-match"),
        # Indentation defeats the ^ anchor — same lookalike-duplicate shape.
        pytest.param(
            "  --- page 1 ---\nalpha\n\n--- page 2 ---\nbravo", [1],
            "  --- page 1 ---\nalpha\n\n--- page 1 ---\nbravo",
            id="indented-marker-no-match"),
        # With literal \r before the newline, "---$" under re.M never
        # matches, so zero markers are found and the text comes back
        # verbatim. Real on-disk CRLF files are unaffected: the caller
        # reads them via Path.read_text (universal newlines).
        pytest.param(
            "--- page 1 ---\r\na\r\n\r\n--- page 2 ---\r\nb", [1],
            "--- page 1 ---\r\na\r\n\r\n--- page 2 ---\r\nb",
            id="crlf-unchanged"),
        # Bodies are strip("\n")ed: blank-line padding around sections
        # collapses and trailing newlines are dropped.
        pytest.param(
            "--- page 1 ---\n\n\nalpha\n\n\n\n--- page 3 ---\n\ncharlie\n\n",
            [1],
            "--- page 2 ---\ncharlie",
            id="blank-line-padding-collapses"),
        # The shift applies even when the removed page has no section.
        pytest.param(
            "--- page 1 ---\na\n\n--- page 5 ---\ne", [3],
            "--- page 1 ---\na\n\n--- page 4 ---\ne",
            id="removed-page-absent-from-text"),
        # Duplicate removed values count twice in the shift: page 3 becomes
        # page 1, COLLIDING with the real page 1. Looks buggy; pinned as-is.
        # The HTTP endpoint dedupes pages before calling, so only direct
        # callers can hit this.
        pytest.param(
            T3, [2, 2],
            "--- page 1 ---\nalpha\n\n--- page 1 ---\ncharlie",
            id="duplicate-removed-double-shifts"),
        # Output follows document order of the markers, not numeric order.
        pytest.param(
            "--- page 3 ---\nc\n\n--- page 1 ---\na", [2],
            "--- page 2 ---\nc\n\n--- page 1 ---\na",
            id="document-order-preserved"),
    ],
)
def test_renumber_marked_text_golden(text, removed, expected):
    assert server._renumber_marked_text(text, removed) == expected


# --- _apply_page_deletion end-to-end ------------------------------------------

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


def test_apply_page_deletion_end_to_end(data_root):
    """Delete the middle page of a 3-page book: PDF rewritten, the removed
    page and whole copies of the rewritten OCR files kept in the trash,
    markers renumbered, title_pages remapped and persisted."""
    bid = "testdel001"
    pdf = data_root / "downloads" / "ia" / "testbook" / "book.pdf"
    _make_pdf(pdf, 3)
    ocr_dir = server._entry_dir(bid) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "compiled.txt").write_text(T3, encoding="utf-8")
    (ocr_dir / "extracted.txt").write_text("no markers at all",
                                           encoding="utf-8")
    translations = server._entry_dir(bid) / "translations"
    translations.mkdir(parents=True, exist_ok=True)
    translated_before = (
        "--- page 1 ---\nuno\n\n--- page 2 ---\ndos\n\n"
        "--- page 3 ---\ntres")
    (translations / "es.txt").write_text(translated_before, encoding="utf-8")
    translation_meta = {
        "version": 1, "src": "compiled.txt", "model": "test",
        "pages": {
            "1": {"sha1": server._page_sha("alpha")},
            "2": {"sha1": server._page_sha("bravo")},
            "3": {"sha1": server._page_sha("charlie")},
        },
    }
    server.lib.save_json(translations / "es.meta.json", translation_meta)
    server.lib.save_json(ocr_dir / "layout.json", {
        "words": {"primary": {"1": ["one"], "2": ["two"], "3": ["three"]}},
        "images": {
            "p1-a.jpeg": {"page": 1, "src_key": "primary"},
            "p2-b.jpeg": {"page": 2, "src_key": "primary"},
            "p3-c.jpeg": {"page": 3, "src_key": "primary"},
        },
    })
    images_dir = ocr_dir / "images"
    images_dir.mkdir()
    for name in ("p1-a.jpeg", "p2-b.jpeg", "p3-c.jpeg"):
        (images_dir / name).write_bytes(name.encode())
    builds = {bid: {"title": "Test", "title_pages": "1,3"}}
    # the remap persists against a fresh read of the store, so the record
    # must exist on disk (in production the caller loaded builds from it)
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    result = server._apply_page_deletion(bid, builds, pdf, [2])

    assert result["deleted"] == [2]
    assert "partial" not in result
    assert "warnings" not in result
    assert result["pages"] == 2
    assert result["trash_id"]
    assert result["build"]["title"] == "Test"
    assert result["build"]["title_pages"] == "1,2"
    assert result["build"]["updated_at"]
    assert sorted(result["renumbered"]) == ["compiled.txt", "extracted.txt"]
    # result["build"] is the same dict object mutated in place
    assert result["build"] is builds[bid]

    # PDF rewritten in place; the trash keeps what was removed; temp gone
    assert _page_count(pdf) == 2
    assert not pdf.with_suffix(".bak.pdf").exists()   # retired by the trash
    assert not pdf.with_suffix(".del.tmp").exists()
    tdir = server.TRASH_DIR / result["trash_id"]
    assert _page_count(tdir / "pages.pdf") == 1       # the deleted page itself
    assert _page_count(tdir / "original.pdf") == 3    # small PDF -> full pre-image

    # OCR renumbering + pre-deletion backups
    assert (ocr_dir / "compiled.txt").read_text(encoding="utf-8") == \
        "--- page 1 ---\nalpha\n\n--- page 2 ---\ncharlie"
    assert not (ocr_dir / "compiled.txt.bak").exists()   # now in the trash item
    assert (tdir / "ocr" / "compiled.txt").read_text(encoding="utf-8") == T3
    # A marker-less file is still listed in "renumbered" and is still
    # snapshotted, even though its content is unchanged. Pinned as-is.
    assert (ocr_dir / "extracted.txt").read_text(encoding="utf-8") == \
        "no markers at all"
    assert not (ocr_dir / "extracted.txt.bak").exists()
    assert (tdir / "ocr" / "extracted.txt").read_text(encoding="utf-8") == \
        "no markers at all"

    # Translation text and its source hashes shift together; deleted-page
    # content is gone rather than surviving as an obsolete trailing page.
    assert (translations / "es.txt").read_text(encoding="utf-8") == \
        "--- page 1 ---\nuno\n\n--- page 2 ---\ntres"
    # the pre-image lives in the trash item now, not as a .bak sibling
    assert not (translations / "es.txt.bak").exists()
    assert (tdir / "translations" / "es.txt").read_text(encoding="utf-8") == \
        translated_before
    meta = json.loads((translations / "es.meta.json").read_text(encoding="utf-8"))
    assert set(meta["pages"]) == {"1", "2"}
    assert meta["pages"]["2"]["sha1"] == server._page_sha("charlie")
    assert not (translations / "es.meta.json.bak").exists()
    assert (tdir / "translations" / "es.meta.json").is_file()

    # Word boxes and extracted figures use the same remap. A figure from the
    # deleted page is removed from layout metadata; the kept page 3 moves to 2.
    layout = json.loads((ocr_dir / "layout.json").read_text(encoding="utf-8"))
    assert set(layout["words"]["primary"]) == {"1", "2"}
    assert layout["words"]["primary"]["2"] == ["three"]
    assert "p2-b.jpeg" not in layout["images"]
    assert layout["images"]["p3-c.jpeg"]["page"] == 2
    assert not (images_dir / "p2-b.jpeg").exists()
    # the figure moves into the trash item at its own relative path, which is
    # what lets restore write it back with no special case; the old
    # .page-delete-backup dead-drop (which nothing ever read) is retired
    assert not (images_dir / ".page-delete-backup").exists()
    assert (tdir / "ocr" / "images" / "p2-b.jpeg").is_file()
    assert (images_dir / "p1-a.jpeg").is_file()
    assert (images_dir / "p3-c.jpeg").is_file()

    # title_pages remap persisted the whole builds dict to BUILDS_PATH
    on_disk = json.loads(server.BUILDS_PATH.read_text(encoding="utf-8"))
    assert on_disk[bid]["title_pages"] == "1,2"
    assert on_disk[bid]["updated_at"] == result["build"]["updated_at"]


def test_page_deletion_remaps_attention_metadata_and_review_threads(data_root):
    """Page remarks follow kept physical pages and deleted threads tombstone."""
    bid = "remark-pages"
    pdf = data_root / "downloads" / "ia" / "remarks" / "book.pdf"
    _make_pdf(pdf, 6)
    builds = {bid: {"title": "Remarked Herbal"}}
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    key = lambda page, source="primary", book=bid: \
        f"page:{book}|{source}|{page}"  # noqa: E731
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {
        "attention": {
            key(1): "keep one",
            key(2): "delete two",
            key(3): "shift three",
            key(5): "shift five",
            key(5, "secondary"): "leave secondary",
            key(5, "primary", "other-book"): "leave other book",
            "pub:book%3Apublic": "leave publication",
        },
        "settings": {"remarksMeta": {
            key(2): {"label": "deleted", "category": "pages"},
            key(3): {"label": "Remarked Herbal \u00b7 page 3", "category": "pages"},
            key(5): {"label": "Remarked Herbal \u00b7 page 5", "category": "pages"},
            key(6): {"label": "Remarked Herbal \u00b7 page 6", "category": "pages"},
            key(5, "secondary"): {"label": "secondary", "category": "pages"},
        }},
    })
    comments = [{"author": "Ada", "text": "Keep this history"}]
    server.lib.save_json(server.REVIEWS_PATH, {
        "survivor": {
            "id": "survivor", "kind": "key", "ref": key(3),
            "key": "key:" + key(3), "label": "Remarked Herbal · Page 3",
            "status": "open", "comments": comments,
        },
        "later": {
            "id": "later", "kind": "key", "ref": key(5),
            "key": "key:" + key(5), "label": "Remarked Herbal · page 5",
            "status": "resolved", "comments": [],
        },
        "deleted": {
            "id": "deleted", "kind": "key", "ref": key(2),
            "key": "key:" + key(2), "label": "Remarked Herbal · Page 2",
            "status": "open", "comments": [{"text": "Do not lose me"}],
        },
        "secondary": {
            "id": "secondary", "kind": "key", "ref": key(5, "secondary"),
            "key": "key:" + key(5, "secondary"), "label": "Secondary · Page 5",
            "status": "open", "comments": [],
        },
        "catalog": {
            "id": "catalog", "kind": "key", "ref": "whl:7",
            "key": "key:whl:7", "label": "Catalog", "status": "open",
            "comments": [],
        },
    })

    result = server._apply_page_deletion(bid, builds, pdf, [2, 4])

    assert result["page_remap"] == {"source": "primary", "deleted": [2, 4]}
    client_state = server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})
    assert client_state["attention"] == {
        key(1): "keep one",
        key(2): "shift three",
        key(3): "shift five",
        key(5, "secondary"): "leave secondary",
        key(5, "primary", "other-book"): "leave other book",
        "pub:book%3Apublic": "leave publication",
    }
    meta = client_state["settings"]["remarksMeta"]
    assert meta[key(2)]["label"] == "Remarked Herbal \u00b7 page 2"
    assert meta[key(3)]["label"] == "Remarked Herbal \u00b7 page 3"
    assert meta[key(4)]["label"] == "Remarked Herbal \u00b7 page 4"
    assert meta[key(5, "secondary")]["label"] == "secondary"
    assert all(item["label"] != "deleted" for item in meta.values())

    reviews = server.lib.load_json(server.REVIEWS_PATH, {})
    assert reviews["survivor"]["ref"] == key(2)
    assert reviews["survivor"]["key"] == "key:" + key(2)
    assert reviews["survivor"]["label"] == "Remarked Herbal · Page 2"
    assert reviews["survivor"]["comments"] == comments
    assert reviews["later"]["ref"] == key(3)
    assert reviews["later"]["label"] == "Remarked Herbal · page 3"
    assert reviews["later"]["status"] == "resolved"
    deleted_ref = reviews["deleted"]["ref"]
    assert deleted_ref == "page-deleted:remark-pages|primary|2|deleted"
    assert reviews["deleted"]["key"] == "key:" + deleted_ref
    assert reviews["deleted"]["label"].endswith(" · removed")
    assert reviews["deleted"]["comments"] == [{"text": "Do not lose me"}]
    assert reviews["secondary"]["ref"] == key(5, "secondary")
    assert reviews["catalog"]["ref"] == "whl:7"

    # Deleting current page 2 again removes a different physical page. Its
    # thread receives a different tombstone rather than merging with the first.
    second = server._apply_page_deletion(bid, builds, pdf, [2])
    assert second["page_remap"] == {"source": "primary", "deleted": [2]}
    reviews = server.lib.load_json(server.REVIEWS_PATH, {})
    survivor_ref = reviews["survivor"]["ref"]
    assert survivor_ref == "page-deleted:remark-pages|primary|2|survivor"
    assert survivor_ref != deleted_ref
    assert reviews["survivor"]["id"] == "survivor"
    assert reviews["survivor"]["status"] == "open"
    assert reviews["survivor"]["comments"] == comments


def test_page_remap_reports_attention_and_review_save_failures(
        data_root, monkeypatch):
    """A rewritten PDF must not hide a failed reference-remap write."""
    bid = "remark-save-failure"
    key = f"page:{bid}|primary|2"
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {
        "attention": {key: "keep visible"},
        "settings": {"remarksMeta": {key: {"label": "Page 2"}}},
    })
    server.lib.save_json(server.REVIEWS_PATH, {
        "thread": {
            "id": "thread", "kind": "key", "ref": key,
            "key": "key:" + key, "label": "Page 2", "comments": [],
        },
    })
    real_save = server.lib.save_json
    failed = {server.lib.CLIENT_STATE_PATH, server.REVIEWS_PATH}

    def refuse_remap(path, value):
        if Path(path) in failed:
            raise OSError("read-only test store")
        return real_save(path, value)

    monkeypatch.setattr(server.lib, "save_json", refuse_remap)
    warnings = server._remap_page_attention_references(
        bid, "primary", [1])

    assert warnings == [
        "personal attention marks could not be renumbered",
        "shared review threads could not be renumbered",
    ]
    assert server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})[
        "attention"] == {key: "keep visible"}
    assert server.lib.load_json(server.REVIEWS_PATH, {})["thread"]["ref"] == key


def test_page_deletion_returns_reference_remap_warnings(data_root, monkeypatch):
    bid = "remark-warning-result"
    pdf = data_root / "remark-warning-result.pdf"
    _make_pdf(pdf, 2)
    builds = {bid: {"title": "Warning Herbal"}}
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")
    monkeypatch.setattr(
        server, "_remap_page_attention_references",
        lambda *_args: ["shared review threads could not be renumbered"])

    result = server._apply_page_deletion(bid, builds, pdf, [2])

    assert result["warnings"] == [
        "shared review threads could not be renumbered"]
    assert result["partial"] is True
    assert _page_count(pdf) == 1


def test_page_delete_endpoint_returns_partial_after_post_commit_failure(
        data_root, client, monkeypatch):
    bid = "layout-warning-result"
    pdf = data_root / "layout-warning-result.pdf"
    _make_pdf(pdf, 2)
    builds = {bid: {"id": bid, "title": "Layout Herbal",
                    "pdf_file": str(pdf)}}
    server.lib.save_json(server.BUILDS_PATH, builds)

    def fail_layout(*_args):
        raise OSError("read-only layout")

    monkeypatch.setattr(server, "_renumber_layout_words", fail_layout)
    response = client.post("/api/pdf/pages/delete", json={
        "build_id": bid, "pdf": str(pdf), "pages": [2],
        "page_revision": "unversioned",
    })
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["partial"] is True
    assert data["warnings"] == ["OCR page layout could not be renumbered"]
    assert data["page_remap"] == {"source": "primary", "deleted": [2]}
    assert _page_count(pdf) == 1
    # the pre-image is a trash payload now, not a .bak.pdf sibling
    assert not pdf.with_suffix(".bak.pdf").exists()
    assert _page_count(
        server.TRASH_DIR / data["trash_id"] / "original.pdf") == 2


def test_page_delete_endpoint_rejects_stale_revision_and_detached_pdf(
        data_root, client):
    bid = "delete-conflict"
    pdf = data_root / "delete-conflict.pdf"
    detached = data_root / "detached.pdf"
    _make_pdf(pdf, 3)
    _make_pdf(detached, 2)
    revision = "2026-07-18T12:00:00+00:00"
    server.lib.save_json(server.BUILDS_PATH, {
        bid: {"id": bid, "title": "Conflict Herbal",
              "pdf_file": str(pdf), "pdf_sources": [],
              "updated_at": revision},
    })

    wrong_pdf = client.post("/api/pdf/pages/delete", json={
        "build_id": bid, "pdf": str(detached), "pages": [1],
        "page_revision": revision,
    })
    assert wrong_pdf.status_code == 409
    assert wrong_pdf.get_json()["conflict"] == "pdf_not_attached"
    assert _page_count(detached) == 2
    assert not detached.with_suffix(".bak.pdf").exists()
    assert server.lib.load_json(server.BUILDS_PATH, {})[bid][
        "updated_at"] == revision

    stale = client.post("/api/pdf/pages/delete", json={
        "build_id": bid, "pdf": str(pdf), "pages": [1],
        "page_revision": "2026-07-18T11:00:00+00:00",
    })
    assert stale.status_code == 409
    assert stale.get_json()["conflict"] == "stale_page_revision"
    assert _page_count(pdf) == 3
    assert not pdf.with_suffix(".bak.pdf").exists()

    missing = client.post("/api/pdf/pages/delete", json={
        "build_id": bid, "pdf": str(pdf), "pages": [1],
    })
    assert missing.status_code == 409
    assert missing.get_json()["conflict"] == "stale_page_revision"
    assert _page_count(pdf) == 3


def test_page_delete_endpoint_rejects_a_second_request_from_the_old_grid(
        data_root, client):
    bid = "delete-twice"
    pdf = data_root / "delete-twice.pdf"
    _make_pdf(pdf, 3)
    revision = "2026-07-18T12:10:00+00:00"
    server.lib.save_json(server.BUILDS_PATH, {
        bid: {"id": bid, "title": "Double Herbal",
              "pdf_file": str(pdf), "pdf_sources": [],
              "updated_at": revision},
    })
    payload = {"build_id": bid, "pdf": str(pdf), "pages": [1],
               "page_revision": revision}

    first = client.post("/api/pdf/pages/delete", json=payload)
    assert first.status_code == 200
    assert first.get_json()["ok"] is True
    assert first.get_json()["build"]["updated_at"] != revision
    assert _page_count(pdf) == 2
    assert not pdf.with_suffix(".bak.pdf").exists()
    assert _page_count(
        server.TRASH_DIR / first.get_json()["trash_id"] / "original.pdf") == 3

    second = client.post("/api/pdf/pages/delete", json=payload)
    assert second.status_code == 409
    assert second.get_json()["conflict"] == "stale_page_revision"
    # The stale request neither deletes the shifted-in physical page nor
    # overwrites the only pre-image of the original three-page file.
    assert _page_count(pdf) == 2
    assert _page_count(
        server.TRASH_DIR / first.get_json()["trash_id"] / "original.pdf") == 3


def test_post_commit_build_save_failure_still_invalidates_old_review_token(
        data_root, client, monkeypatch):
    bid = "durable-page-revision"
    pdf = data_root / "durable-page-revision.pdf"
    _make_pdf(pdf, 2)
    revision = "2026-07-18T12:20:00+00:00"
    server.lib.save_json(server.BUILDS_PATH, {
        bid: {"id": bid, "title": "Durable Herbal",
              "pdf_file": str(pdf), "pdf_sources": [],
              "updated_at": revision},
    })
    server.lib.save_json(server.REVIEWS_PATH, {})

    def fail_post_commit_merge(*_args, **_kwargs):
        raise OSError("read-only build store")

    monkeypatch.setattr(server, "_builds_apply", fail_post_commit_merge)
    deleted = client.post("/api/pdf/pages/delete", json={
        "build_id": bid, "pdf": str(pdf), "pages": [1],
        "page_revision": revision,
    })
    data = deleted.get_json()

    assert deleted.status_code == 200
    assert data["ok"] is True
    assert data["partial"] is True
    assert "build metadata could not be saved" in data["warnings"]
    assert _page_count(pdf) == 1
    durable_revision = server.lib.load_json(server.BUILDS_PATH, {})[bid][
        "updated_at"]
    assert durable_revision != revision
    assert data["build"]["updated_at"] == durable_revision

    stale = client.post("/api/reviews", json={
        "kind": "key", "ref": f"page:{bid}|primary|1",
        "label": "Durable Herbal \u00b7 page 1",
        "page_revision": revision,
    })
    assert stale.status_code == 409
    assert server.lib.load_json(server.REVIEWS_PATH, {}) == {}


def test_page_review_rejects_a_revision_from_before_deletion(
        data_root, client):
    """A late Q popover cannot attach its thread to the shifted-in page."""
    bid = "review-revision"
    pdf = data_root / "review-revision.pdf"
    _make_pdf(pdf, 3)
    before = "2026-07-18T00:00:00+00:00"
    build = {
        "id": bid, "title": "Revision Herbal", "pdf_file": str(pdf),
        "pdf_sources": [], "updated_at": before,
    }
    server.lib.save_json(server.BUILDS_PATH, {bid: build})
    server.lib.save_json(server.REVIEWS_PATH, {})

    live = {bid: dict(build)}
    result = server._apply_page_deletion(bid, live, pdf, [1])
    after = result["build"]["updated_at"]
    assert after != before

    stale = client.post("/api/reviews", json={
        "kind": "key", "ref": f"page:{bid}|primary|2",
        "label": "Revision Herbal \u00b7 page 2", "page_revision": before,
    })
    assert stale.status_code == 409
    assert server.lib.load_json(server.REVIEWS_PATH, {}) == {}

    current = client.post("/api/reviews", json={
        "kind": "key", "ref": f"page:{bid}|primary|1",
        "label": "Revision Herbal \u00b7 page 1", "page_revision": after,
    })
    assert current.status_code == 200
    assert current.get_json()["review"]["ref"] == f"page:{bid}|primary|1"


def test_page_review_accepts_an_unversioned_legacy_build(client):
    bid = "legacy-page-review"
    server.lib.save_json(server.BUILDS_PATH, {
        bid: {"id": bid, "title": "Legacy Herbal", "pdf_sources": []},
    })
    server.lib.save_json(server.REVIEWS_PATH, {})

    response = client.post("/api/reviews", json={
        "kind": "key", "ref": f"page:{bid}|primary|1",
        "label": "Legacy Herbal \u00b7 page 1",
        "page_revision": "unversioned",
    })

    assert response.status_code == 200
    assert response.get_json()["review"]["ref"] == f"page:{bid}|primary|1"


def test_apply_page_deletion_refusals_leave_pdf_untouched(data_root):
    """Delete-all and out-of-range-only raise ValueError BEFORE any write:
    no backup appears and the PDF keeps its pages."""
    bid = "testdel002"
    pdf = data_root / "downloads" / "ia" / "testbook2" / "book.pdf"
    _make_pdf(pdf, 2)
    builds = {bid: {"title": "T"}}

    with pytest.raises(ValueError, match="cannot delete every page"):
        server._apply_page_deletion(bid, builds, pdf, [1, 2])
    with pytest.raises(ValueError, match="pages out of range"):
        server._apply_page_deletion(bid, builds, pdf, [99])

    assert _page_count(pdf) == 2
    assert not pdf.with_suffix(".bak.pdf").exists()


def test_apply_page_deletion_mixed_out_of_range_succeeds(data_root):
    """A valid page mixed with an out-of-range one succeeds, and the bogus
    number is reported in "deleted" as if it were removed. Pinned as-is."""
    bid = "testdel003"
    pdf = data_root / "downloads" / "ia" / "testbook3" / "book.pdf"
    _make_pdf(pdf, 2)
    ocr_dir = server._entry_dir(bid) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "compiled.txt").write_text(
        "--- page 1 ---\nalpha\n\n--- page 2 ---\nbravo", encoding="utf-8")
    builds = {bid: {"title": "Mixed", "title_pages": "1"}}

    result = server._apply_page_deletion(bid, builds, pdf, [2, 99])

    assert result["deleted"] == [2, 99]
    assert result["page_remap"] == {"source": "primary", "deleted": [2]}
    assert result["pages"] == 1
    assert _page_count(pdf) == 1
    assert (ocr_dir / "compiled.txt").read_text(encoding="utf-8") == \
        "--- page 1 ---\nalpha"
    assert builds[bid]["title_pages"] == "1"


def test_apply_page_deletion_no_titles_no_ocr(data_root):
    """Without title_pages and without an ocr/ dir the deletion still works,
    "renumbered" is empty, and BUILDS_PATH is NOT rewritten."""
    bid = "testdel004"
    pdf = data_root / "downloads" / "ia" / "testbook4" / "book.pdf"
    _make_pdf(pdf, 2)
    builds = {bid: {"title": "NoTitles"}}
    sentinel = json.dumps({"__sentinel__": True})
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(sentinel, encoding="utf-8")

    result = server._apply_page_deletion(bid, builds, pdf, [1])

    trash_id = result.pop("trash_id")
    assert trash_id and (server.TRASH_DIR / trash_id / "pages.pdf").is_file()
    assert result == {"deleted": [1], "pages": 1, "renumbered": [],
                      "page_remap": {"source": "primary", "deleted": [1]},
                      "build": {"title": "NoTitles"}}
    assert "title_pages" not in builds[bid]
    # save_json is skipped entirely when there are no title pages
    assert server.BUILDS_PATH.read_text(encoding="utf-8") == sentinel


# --- thumbnail_source remap on page deletion (mirrors title_pages) ----------

def test_apply_page_deletion_shifts_thumbnail_source_page_reference(data_root):
    """thumbnail_source="page:3" shifts to "page:2" when page 2 is deleted
    from a 3-page primary PDF, the same arithmetic as title_pages."""
    bid = "testdel005"
    pdf = data_root / "downloads" / "ia" / "testbook5" / "book.pdf"
    _make_pdf(pdf, 3)
    builds = {bid: {"title": "Thumb", "thumbnail_source": "page:3"}}
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    result = server._apply_page_deletion(bid, builds, pdf, [2])

    assert result["build"]["thumbnail_source"] == "page:2"
    on_disk = json.loads(server.BUILDS_PATH.read_text(encoding="utf-8"))
    assert on_disk[bid]["thumbnail_source"] == "page:2"


def test_apply_page_deletion_clears_thumbnail_source_of_deleted_page(data_root):
    """thumbnail_source pointing at the page being deleted clears to "" --
    the publish pipeline falls back to the cover-candidate heuristic."""
    bid = "testdel006"
    pdf = data_root / "downloads" / "ia" / "testbook6" / "book.pdf"
    _make_pdf(pdf, 3)
    builds = {bid: {"title": "Thumb", "thumbnail_source": "page:2"}}

    result = server._apply_page_deletion(bid, builds, pdf, [2])

    assert result["build"]["thumbnail_source"] == ""


def test_apply_page_deletion_leaves_image_thumbnail_source_untouched(data_root):
    """An "image:<name>" source references an OCR-extracted figure, not a
    PDF page number -- page deletion must never rewrite or clear it."""
    bid = "testdel007"
    pdf = data_root / "downloads" / "ia" / "testbook7" / "book.pdf"
    _make_pdf(pdf, 3)
    builds = {bid: {"title": "Thumb", "thumbnail_source": "image:p2-fig1.jpeg"}}

    result = server._apply_page_deletion(bid, builds, pdf, [2])

    assert result["build"]["thumbnail_source"] == "image:p2-fig1.jpeg"


def test_page_deletion_refuses_active_analyze_job(data_root):
    bid = "testdelbusy"
    pdf = data_root / "downloads" / "ia" / "busy" / "book.pdf"
    _make_pdf(pdf, 2)
    builds = {bid: {"title": "Busy"}}
    job = {"id": "busytranslate", "build_id": bid,
           "kind": "translate:es", "status": "running"}
    with server._an_jobs_lock:
        server._an_jobs[job["id"]] = job
    try:
        with pytest.raises(ValueError, match="page-processing job"):
            server._apply_page_deletion(bid, builds, pdf, [1])
    finally:
        with server._an_jobs_lock:
            server._an_jobs.pop(job["id"], None)

    assert _page_count(pdf) == 2
    assert not pdf.with_suffix(".bak.pdf").exists()


def test_stale_ocr_and_analyze_snapshots_cannot_start_after_renumber():
    bid = "revisionguard"
    with server._page_structure_lock:
        old = server._page_structure_revision.get(bid, 0)
        server._page_structure_revision[bid] = old + 1
    ocr = {"id": "stalerevision", "build_id": bid, "target": "compiled.txt",
           "src_key": "primary", "status": "running"}
    try:
        assert server._ocr_job_start_guarded(ocr, old) is False
        assert ocr["id"] not in server._ocr_jobs
        with pytest.raises(server._AnalyzeSourceChanged):
            server._an_job_start_guarded(
                bid, old, "translate:es", 1, lambda _job: None)
    finally:
        with server._page_structure_lock:
            if old:
                server._page_structure_revision[bid] = old
            else:
                server._page_structure_revision.pop(bid, None)
