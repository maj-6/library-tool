"""Tesseract readiness must cover both halves of the local OCR runtime."""

import server


def test_tesseract_check_rejects_missing_python_bridge(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_tesseract_bridge_error",
        lambda: "Python OCR bridge unavailable: No module named 'pytesseract'",
    )

    response = client.get("/api/ocr/tesseract")

    assert response.status_code == 200
    assert response.json == {
        "ok": True,
        "installed": False,
        "path": "",
        "version": "",
        "error": "Python OCR bridge unavailable: No module named 'pytesseract'",
    }
