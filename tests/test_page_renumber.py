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
    """Delete the middle page of a 3-page book: PDF rewritten with backup,
    OCR markers renumbered (with .txt.bak safety copies), title_pages
    remapped and persisted to whl_builds.json."""
    bid = "testdel001"
    pdf = data_root / "downloads" / "ia" / "testbook" / "book.pdf"
    _make_pdf(pdf, 3)
    ocr_dir = server._entry_dir(bid) / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    (ocr_dir / "compiled.txt").write_text(T3, encoding="utf-8")
    (ocr_dir / "extracted.txt").write_text("no markers at all",
                                           encoding="utf-8")
    builds = {bid: {"title": "Test", "title_pages": "1,3"}}
    # the remap persists against a fresh read of the store, so the record
    # must exist on disk (in production the caller loaded builds from it)
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps(builds), encoding="utf-8")

    result = server._apply_page_deletion(bid, builds, pdf, [2])

    assert result == {
        "deleted": [2],
        "pages": 2,
        # ordering comes from Path.glob; compare order-insensitively
        "renumbered": result["renumbered"],
        "backup": "book.bak.pdf",
        "build": {"title": "Test", "title_pages": "1,2"},
    }
    assert sorted(result["renumbered"]) == ["compiled.txt", "extracted.txt"]
    # result["build"] is the same dict object mutated in place
    assert result["build"] is builds[bid]

    # PDF rewritten in place; .bak.pdf keeps the original; temp file gone
    assert _page_count(pdf) == 2
    assert _page_count(pdf.with_suffix(".bak.pdf")) == 3
    assert not pdf.with_suffix(".del.tmp").exists()

    # OCR renumbering + pre-deletion backups
    assert (ocr_dir / "compiled.txt").read_text(encoding="utf-8") == \
        "--- page 1 ---\nalpha\n\n--- page 2 ---\ncharlie"
    assert (ocr_dir / "compiled.txt.bak").read_text(encoding="utf-8") == T3
    # A marker-less file is still listed in "renumbered" and still gets a
    # .bak, even though its content is unchanged. Pinned as-is.
    assert (ocr_dir / "extracted.txt").read_text(encoding="utf-8") == \
        "no markers at all"
    assert (ocr_dir / "extracted.txt.bak").read_text(encoding="utf-8") == \
        "no markers at all"

    # title_pages remap persisted the whole builds dict to BUILDS_PATH
    on_disk = json.loads(server.BUILDS_PATH.read_text(encoding="utf-8"))
    assert on_disk[bid]["title_pages"] == "1,2"


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

    assert result == {"deleted": [1], "pages": 1, "renumbered": [],
                      "backup": "book.bak.pdf",
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
