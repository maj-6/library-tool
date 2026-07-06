"""Local web app to review and edit per-book metadata.

Serves a 3-pane UI: a sidebar list of books, a left panel showing the
title-page image and the transcript region, and a right panel with editable
metadata fields. Submitting upserts the finalized entry into
output/library_db.json.

Run with python3 from anywhere:
    python3 tools/webapp/server.py
then open http://127.0.0.1:5000
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

# Make the shared library importable (tools/ is the parent of this package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import libcommon as lib  # noqa: E402

app = Flask(__name__)


def _index_by_id() -> dict[str, dict]:
    return {e["id"]: e for e in lib.load_json(lib.BOOKS_INDEX_PATH, [])}


def _metadata_by_id() -> dict[str, dict]:
    return {e["id"]: e for e in lib.load_json(lib.BOOKS_METADATA_PATH, [])}


def _empty_metadata() -> dict:
    return {f: "" for f in lib.METADATA_FIELDS}


def _image_files(book_id: str) -> list[str]:
    folder = lib.BOOKS_DIR / book_id
    if not folder.exists():
        return []
    jpgs = [p.name for p in folder.iterdir() if p.suffix.lower() == ".jpg"]
    # Sort numerically by stem (1.jpg, 2.jpg, ..., 10.jpg).
    return sorted(jpgs, key=lambda n: int(re.sub(r"\D", "", Path(n).stem) or 0))


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/books")
def api_books():
    index = lib.load_json(lib.BOOKS_INDEX_PATH, [])
    meta = _metadata_by_id()
    db = lib.load_json(lib.LIBRARY_DB_PATH, {})
    out = []
    for entry in index:
        bid = entry["id"]
        source = db.get(bid) or meta.get(bid) or {}
        title = (source.get("title") or "").strip() or f"(untitled {bid[:6]})"
        out.append(
            {
                "id": bid,
                "title": title,
                "submitted": bid in db,
                "image_count": entry.get("image_count", 0),
                "source_transcript": entry.get("source_transcript", ""),
            }
        )
    return jsonify(out)


@app.route("/api/book/<book_id>")
def api_book(book_id: str):
    index = _index_by_id()
    if book_id not in index:
        abort(404)
    entry = index[book_id]
    db = lib.load_json(lib.LIBRARY_DB_PATH, {})
    meta = _metadata_by_id()

    saved = db.get(book_id, {})
    metadata = _empty_metadata()
    metadata.update({k: v for k, v in meta.get(book_id, {}).items() if k in metadata})
    metadata.update({k: v for k, v in saved.items() if k in metadata})

    folder = lib.BOOKS_DIR / book_id
    transcript_path = folder / "transcript.txt"
    region_text = (
        transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
    )

    title_page = str(saved.get("title_page_image") or entry.get("title_page_image") or "1")
    return jsonify(
        {
            "id": book_id,
            "source_transcript": entry.get("source_transcript", ""),
            "time_region": entry.get("time_region", {}),
            "transcript": region_text,
            "images": _image_files(book_id),
            "title_page_image": title_page,
            "metadata": metadata,
            "submitted": book_id in db,
        }
    )


@app.route("/images/<book_id>/<path:filename>")
def images(book_id: str, filename: str):
    folder = lib.BOOKS_DIR / book_id
    if not folder.exists():
        abort(404)
    return send_from_directory(folder, filename)


@app.route("/api/book/<book_id>", methods=["POST"])
def api_save(book_id: str):
    if book_id not in _index_by_id():
        abort(404)
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("metadata", {})
    entry = {"id": book_id}
    for field in lib.METADATA_FIELDS:
        entry[field] = str(incoming.get(field, "")).strip()
    entry["title_page_image"] = str(payload.get("title_page_image") or "1")

    db = lib.load_json(lib.LIBRARY_DB_PATH, {})
    db[book_id] = entry
    lib.save_json(lib.LIBRARY_DB_PATH, db)
    return jsonify({"ok": True, "entry": entry})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
