"""Extraction must abstain when OCR recovered nothing readable.

Capture `7b9eba63` in the live corpus carried 52 characters of OCR that were
purely Mistral image placeholders. `if ocr_text and api_key:` treated that as
usable input, and the model answered with a complete, confident and entirely
invented record — Gibbon's *Decline and Fall*, John Murray, 1854 — which then
catalogued indistinguishably from a real reading.
"""
from __future__ import annotations

import capture_pipeline

# Verbatim OCR of capture 7b9eba63 (two photos, both unreadable figures).
_LIVE_FABRICATION_CASE = (
    "--- Photo 1 ---\n"
    "![img-0.jpeg](img-0.jpeg)\n\n![img-1.jpeg](img-1.jpeg)"
)


def test_placeholder_only_ocr_is_not_readable() -> None:
    assert not capture_pipeline.has_readable_ocr(_LIVE_FABRICATION_CASE)
    assert capture_pipeline.readable_ocr_chars(_LIVE_FABRICATION_CASE) == 0


def test_empty_and_whitespace_ocr_is_not_readable() -> None:
    for value in ("", "   ", "\n\n", None):
        assert not capture_pipeline.has_readable_ocr(value)


def test_section_headers_alone_are_not_evidence() -> None:
    """Our own headers must not keep a failed read alive."""
    headers = "--- Capture 1 (role: cover) ---\n\n--- Capture 2 (role: spine) ---"
    assert not capture_pipeline.has_readable_ocr(headers)


def test_a_real_title_page_is_readable() -> None:
    real = (
        "--- Capture 1 (role: title_page) ---\n"
        "# HISTORY\n\nof the\n\n## COMSTOCK PATENT MEDICINE\n\n"
        "by Robert B. Shaw\n\n![img-0.jpeg](img-0.jpeg)\n\n"
        "SMITHSONIAN INSTITUTION PRESS\n\n1972"
    )
    assert capture_pipeline.has_readable_ocr(real)


def test_a_short_spine_label_still_counts_as_readable() -> None:
    """A spine yields very little text; abstaining on it would lose real books."""
    spine = "--- Capture 1 (role: spine) ---\n# Pharmacopoeia Augustana"
    assert capture_pipeline.has_readable_ocr(spine)


def test_process_capture_skips_extraction_and_says_why(monkeypatch) -> None:
    """The abstention must be reported, not silent — a blank record with no
    explanation is indistinguishable from a book that genuinely has no imprint."""
    monkeypatch.setattr(capture_pipeline, "process_photo", lambda raw: raw)
    monkeypatch.setattr(
        capture_pipeline, "mistral_ocr",
        lambda data, key: "![img-0.jpeg](img-0.jpeg)")

    def _explode(*args, **kwargs):  # must never be reached
        raise AssertionError("extraction ran on unreadable OCR")

    monkeypatch.setattr(capture_pipeline, "extract_bibliography", _explode)

    result = capture_pipeline.process_capture([b"jpeg-bytes"], "test-key")

    assert result["fields"]["title"] == ""
    assert result["fields"]["author"] == ""
    assert any("extraction skipped" in e for e in result["errors"]), result["errors"]
