"""Tests for the thumbnail-selection heuristic in tools/whl_explorer/server.py:
first_content_page() (the "cover candidate" the Editor's Resources tab and the
publish pipeline both fall back to when nothing was picked by hand), and
_ocr_extracted_images() (the figures an OCR service already pulled out of a
page, surfaced in the OCR tab's Documents tree).

conftest.py points WHL_DATA_ROOT at a throwaway directory before any tools
module is imported, so importing server below never touches live data.
"""
from __future__ import annotations

import json
from pathlib import Path

import server


def _make_pdf(path: Path, n_pages: int, inked_pages: frozenset = frozenset(),
              text_pages: frozenset = frozenset()) -> None:
    """A synthetic PDF: `n_pages` blank pages, except each 1-based page in
    `inked_pages` gets a filled rectangle (ink, no text layer -- exercises the
    ink-density half of the blank test) and each page in `text_pages` gets a
    real text layer (the other half)."""
    import fitz

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for i in range(n_pages):
        pg = doc.new_page(width=200, height=200)
        if (i + 1) in inked_pages:
            pg.draw_rect(fitz.Rect(20, 20, 180, 180), fill=(0, 0, 0))
        if (i + 1) in text_pages:
            pg.insert_text((50, 100), f"PAGE {i + 1}")
    doc.save(str(path))
    doc.close()


def test_first_content_page_all_blank_returns_none(tmp_path):
    pdf = tmp_path / "blank.pdf"
    _make_pdf(pdf, 3)
    assert server.first_content_page(pdf) is None


def test_first_content_page_skips_blank_flyleaf(tmp_path):
    pdf = tmp_path / "flyleaf.pdf"
    _make_pdf(pdf, 3, text_pages=frozenset({2}))
    assert server.first_content_page(pdf) == 2


def test_first_content_page_ink_without_text(tmp_path):
    """Not text-only -- a filled rectangle with no text layer still counts
    as "content" via the ink-density half of the test."""
    pdf = tmp_path / "inked.pdf"
    _make_pdf(pdf, 2, inked_pages=frozenset({1}))
    assert server.first_content_page(pdf) == 1


def test_first_content_page_respects_scan_cap(tmp_path):
    """A long all-blank-until-late document stops scanning at max_scan
    rather than walking every page to find content beyond it."""
    pdf = tmp_path / "long.pdf"
    _make_pdf(pdf, 30, text_pages=frozenset({25}))
    assert server.first_content_page(pdf, max_scan=10) is None
    assert server.first_content_page(pdf, max_scan=30) == 25


def test_ocr_extracted_images_lists_files_with_layout_meta(data_root):
    bid = "testthumbimg001"
    img_dir = server._entry_dir(bid) / "ocr" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    (img_dir / "p3-fig1.jpeg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    layout = {"images": {"p3-fig1.jpeg": {"page": 3, "x": 0.1, "y": 0.1,
                                          "w": 0.5, "h": 0.5}}}
    (server._entry_dir(bid) / "ocr" / "layout.json").write_text(
        json.dumps(layout), encoding="utf-8")

    out = server._ocr_extracted_images(bid)

    assert out == [{"name": "p3-fig1.jpeg", "page": 3,
                    "size": (img_dir / "p3-fig1.jpeg").stat().st_size}]


def test_ocr_extracted_images_empty_when_no_images_dir(data_root):
    assert server._ocr_extracted_images("no-such-build-at-all") == []
