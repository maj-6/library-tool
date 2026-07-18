"""PDF viewers should prefer the PDF/OCR comparison view on first open."""

from pathlib import Path


APP_JS = Path(__file__).parents[1] / "tools" / "whl_explorer" / "static" / "app.js"


def test_pdf_ocr_comparison_is_the_default_but_explicit_preferences_win():
    source = APP_JS.read_text(encoding="utf-8")

    assert "whlModalOcr: true" in source
    assert "let ocrWanted = true;" in source
    assert "let ocrOn = false;" in source
    assert "setOcr(opts.ocr != null ? opts.ocr : ocrWanted);" in source
