from __future__ import annotations

from pathlib import Path

import pytest


fitz = pytest.importorskip("fitz")


def _make_colored_pdf(path: Path) -> tuple[int, int, int]:
    paper = (184, 166, 123)
    colors = [(90, 35, 28), *([paper] * 5), (32, 55, 105)]
    doc = fitz.open()
    try:
        for page_no, rgb in enumerate(colors, 1):
            page = doc.new_page(width=200, height=300)
            color = tuple(v / 255 for v in rgb)
            # A scanner-black outer edge should be skipped before the margin
            # ring is measured; only the inset page stock is representative.
            page.draw_rect(page.rect, color=(0, 0, 0), fill=(0, 0, 0), width=0)
            page.draw_rect(fitz.Rect(8, 8, 192, 292), color=color,
                           fill=color, width=0)
            # Ink in the page body must not pull the margin estimate darker.
            page.insert_text((45, 150), f"Page {page_no}", fontsize=20,
                             color=(0, 0, 0))
        doc.save(path)
    finally:
        doc.close()
    return paper


def _hex_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


def test_pdf_paper_color_samples_interior_margins_and_lightens(client, data_root):
    pdf = data_root / "paper-sample.pdf"
    expected = _make_colored_pdf(pdf)

    response = client.get("/api/pdf/paper-color", query_string={
        "path": str(pdf), "samples": 3, "lighten": 25,
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["sampled_pages"] == [2, 4, 6]

    base = _hex_rgb(data["base_color"])
    assert all(abs(got - want) <= 3 for got, want in zip(base, expected))
    light = _hex_rgb(data["color"])
    want_light = tuple(round(v + (255 - v) * 0.25) for v in base)
    assert light == want_light


def test_paper_sample_page_selection_uses_all_short_pdf_pages():
    import server

    assert server._paper_sample_pages(0, 5) == []
    assert server._paper_sample_pages(3, 5) == [0, 1, 2]
    assert server._paper_sample_pages(7, 3) == [1, 3, 5]
    assert server._paper_sample_pages(20, 1) == [10]
