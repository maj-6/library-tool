"""World Herb Library cataloging workbench (Flask, localhost, single user).

The tool supports one core workflow: reconciling a private herbal library
against the World Herb Library (WHL), locating existing scans, and preparing
new catalog entries for submission to WHL.

Data sources (all local):
  - whl_catalog.csv          WHL catalogue export (+ output/whl_scraped.json
                             from the website API, + output/whl_corrections.json
                             overlay for the user's edits)
  - output/ch_library.json   the CH private-library spreadsheet, converted
  - output/manual_entries.json  hand-entered books
  - copyright_renewals.csv   offline copyright-renewal check
  - output/ol_search.db      consolidated Open Library editions index
  - output/whl_builds.json   catalog entries being prepared for submission

Run with python3:
    python3 tools/whl_explorer/server.py
then open http://127.0.0.1:5001
"""
from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

# Make tools/ importable for the shared helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog_checks as checks  # noqa: E402
import libcommon as lib  # noqa: E402
import ol_client  # noqa: E402
import scan_search  # noqa: E402
import whl_client  # noqa: E402
import whl_scrape  # noqa: E402

app = Flask(__name__)


# --- the CH private-library catalogue -------------------------------------------

def _categories(row: dict) -> str:
    """Combine the CH Library KEY/KEY_2/KEY_3 category fields, de-duplicated."""
    seen_lower: set[str] = set()
    cats: list[str] = []
    for field in ("key", "key_2", "key_3"):
        val = str(row.get(field, "") or "").strip()
        if val and val.lower() not in seen_lower:
            seen_lower.add(val.lower())
            cats.append(val)
    return ", ".join(cats)


def _ch_row(idx: int, row: dict) -> dict:
    return {
        "idx": idx,
        "title": str(row.get("publication", "") or "").replace("_", " ").strip(),
        "subtitle": "",
        "author": str(row.get("authors", "") or "").strip(),
        "year": str(row.get("year_of_publication", "") or "").strip(),
        "edition": str(row.get("edition", "") or "").strip(),
        "publisher": str(row.get("publisher", "") or "").strip(),
        "city": str(row.get("city_published", "") or "").strip(),
        "pages": str(row.get("page_reference", "") or "").strip(),
        "condition": str(row.get("condition", "") or "").strip(),
        "illustrations": str(row.get("illustrations", "") or "").strip(),
        "price": str(row.get("price", "") or "").strip(),
        "acquired": str(row.get("date", "") or "").strip(),
        "categories": _categories(row),
        "notes": str(row.get("notes", "") or "").strip(),
    }


# --- routes ----------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/books")
def api_books():
    """The CH private-library catalogue (output/ch_library.json)."""
    raw = lib.load_json(lib.CH_LIBRARY_JSON_PATH, [])
    books = [_ch_row(i, r) for i, r in enumerate(raw)]
    return jsonify({"books": [b for b in books if b["title"] or b["author"]]})


# --- book builder: catalog entries being prepared for WHL submission -------------

BUILDS_PATH = lib.OUTPUT_DIR / "whl_builds.json"

# The field set mirrors what a WHL catalog entry needs. pdf_source is the
# source URL; pdf_file is the local PDF attached for the actual submission;
# ocr_active/ocr_verified/ocr_quality track the entry folder's OCR files.
_BUILD_FIELDS = ("title", "subtitle", "authors", "year", "publisher",
                 "publisher_city", "edition", "language", "pages",
                 "categories", "description", "pdf_source", "pdf_file",
                 "source_url", "notes", "status",
                 "ocr_active", "ocr_verified", "ocr_quality")


@app.route("/api/builds")
def api_builds():
    return jsonify({"builds": lib.load_json(BUILDS_PATH, {})})


@app.route("/api/builds", methods=["POST"])
def api_builds_create():
    payload = request.get_json(silent=True) or {}
    seed = payload.get("build") or {}
    builds = lib.load_json(BUILDS_PATH, {})
    build = {f: str(seed.get(f, "") or "").strip() for f in _BUILD_FIELDS}
    if build["status"] not in ("draft", "ready"):
        build["status"] = "draft"
    build["id"] = lib.gen_id(set(builds))
    build["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    build["updated_at"] = build["created_at"]
    builds[build["id"]] = build
    lib.save_json(BUILDS_PATH, builds)
    return jsonify({"ok": True, "build": build})


@app.route("/api/builds/<build_id>", methods=["PATCH"])
def api_builds_update(build_id: str):
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    payload = request.get_json(silent=True) or {}
    b = builds[build_id]
    for f in _BUILD_FIELDS:
        if f in payload:
            b[f] = str(payload[f] or "").strip()
    if b.get("status") not in ("draft", "ready"):
        b["status"] = "draft"
    b["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lib.save_json(BUILDS_PATH, builds)
    return jsonify({"ok": True, "build": b})


@app.route("/api/builds/<build_id>", methods=["DELETE"])
def api_builds_delete(build_id: str):
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    del builds[build_id]
    lib.save_json(BUILDS_PATH, builds)
    return jsonify({"ok": True})


@app.route("/api/builds/restore", methods=["POST"])
def api_builds_restore():
    """Reinsert a deleted build verbatim (undo support)."""
    payload = request.get_json(silent=True) or {}
    build = payload.get("build") or {}
    bid = str(build.get("id") or "")
    if not bid:
        abort(400)
    builds = lib.load_json(BUILDS_PATH, {})
    builds[bid] = build
    lib.save_json(BUILDS_PATH, builds)
    return jsonify({"ok": True, "build": build})


# --- local PDF serving + browsing (for the builder's SOURCE tab) ------------------
# Single-user localhost tool: the user picks PDFs from anywhere on disk, so
# these endpoints intentionally serve any local *.pdf path.

def _resolve_local(raw: str) -> Path | None:
    p = Path(raw)
    if not p.is_absolute():
        p = lib.ROOT / p
    try:
        return p.resolve()
    except OSError:
        return None


@app.route("/api/pdf")
def api_pdf():
    """Stream a local PDF (absolute path, or relative to the repo root).
    ?preview=1&pages=N serves a compressed, truncated derivative instead —
    much faster to load for large scans."""
    raw = (request.args.get("path") or "").strip()
    if not raw:
        abort(400)
    p = _resolve_local(raw)
    if p is None or p.suffix.lower() != ".pdf" or not p.is_file():
        abort(404)
    if request.args.get("preview"):
        try:
            pages = max(1, min(500, int(request.args.get("pages") or 20)))
        except ValueError:
            pages = 20
        try:
            p = _preview_pdf(p, pages)
        except Exception:
            pass  # fall back to the original
    return send_file(p, mimetype="application/pdf", conditional=True)


@app.route("/api/ai/summarize", methods=["POST"])
def api_ai_summarize():
    """Proxy a summarization request to an OpenAI-compatible chat API.
    The browser cannot call those APIs directly (no CORS), so the client
    sends its configured endpoint/model/key here."""
    p = request.get_json(silent=True) or {}
    base = (p.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    key = (p.get("api_key") or "").strip()
    model = (p.get("model") or "").strip()
    instructions = (p.get("instructions") or "").strip()
    text = (p.get("text") or "").strip()
    if not key or not model:
        return jsonify({"ok": False,
                        "error": "AI model / API key not configured (Settings > AI)"})
    if not text:
        return jsonify({"ok": False, "error": "no source text"})
    system = instructions or (
        "You summarize the OCR text of old books for a library catalog. "
        "Write a concise, factual catalog description in Markdown.")
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "Summarize this book from its OCR text:\n\n"
                                        + text[:60000]},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        summary = data["choices"][0]["message"]["content"]
        return jsonify({"ok": True, "summary": summary})
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return jsonify({"ok": False, "error": f"HTTP {exc.code}: {detail}"})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# --- entry folders: one directory per pending entry -------------------------------
# output/entries/<build-id>/ holds metadata.json, a compressed + truncated
# preview.pdf, and ocr/*.txt files (extracted plus any loaded for comparison).

ENTRIES_DIR = lib.OUTPUT_DIR / "entries"


def _entry_dir(build_id: str) -> Path:
    return ENTRIES_DIR / build_id


def _ocr_name(raw: str) -> str:
    name = re.sub(r"[^\w.\- ]", "_", (raw or "").strip()) or "ocr"
    if not name.lower().endswith(".txt"):
        name += ".txt"
    return name


def _entry_folder_info(build_id: str) -> dict:
    d = _entry_dir(build_id)
    ocr = []
    if (d / "ocr").is_dir():
        for f in sorted((d / "ocr").glob("*.txt")):
            ocr.append({"name": f.name, "size": f.stat().st_size})
    return {"exists": d.is_dir(), "path": str(d), "ocr": ocr,
            "preview": (d / "preview.pdf").is_file(),
            "metadata": (d / "metadata.json").is_file()}


def _pdf_extract_text(p: Path, max_pages: int) -> tuple[int, int, str]:
    """(total_pages, shown_pages, text) of a PDF's text/OCR layer."""
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    total = len(reader.pages)
    shown = min(total, max_pages)
    parts = []
    for i in range(shown):
        text = (reader.pages[i].extract_text() or "").strip()
        parts.append(f"--- page {i + 1} ---\n{text}")
    return total, shown, "\n\n".join(parts)


def _preview_pdf(src: Path, pages: int) -> Path:
    """A compressed, truncated preview derivative, cached by mtime."""
    import hashlib
    cache = lib.ROOT / "downloads" / "cache" / "previews"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{src}|{src.stat().st_mtime}|{pages}".encode("utf-8")).hexdigest()[:16]
    out = cache / f"{key}.pdf"
    if out.is_file():
        return out
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(str(src))
    writer = PdfWriter()
    for i in range(min(len(reader.pages), pages)):
        page = reader.pages[i]
        try:
            page.compress_content_streams()
        except Exception:
            pass
        writer.add_page(page)
    tmp = out.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        writer.write(fh)
    tmp.replace(out)
    return out


@app.route("/api/builds/<build_id>/folder")
def api_build_folder_info(build_id: str):
    return jsonify(_entry_folder_info(build_id))


@app.route("/api/builds/<build_id>/folder", methods=["POST"])
def api_build_folder_sync(build_id: str):
    """Create/refresh the entry folder: metadata, PDF preview, extracted OCR.
    Body: {pages: N, keep_original: bool}."""
    builds = lib.load_json(BUILDS_PATH, {})
    if build_id not in builds:
        abort(404)
    b = builds[build_id]
    p = request.get_json(silent=True) or {}
    try:
        pages = max(1, min(500, int(p.get("pages") or 20)))
    except (TypeError, ValueError):
        pages = 20
    keep_original = bool(p.get("keep_original", True))
    d = _entry_dir(build_id)
    (d / "ocr").mkdir(parents=True, exist_ok=True)
    lib.save_json(d / "metadata.json", b)
    notes = []
    src = None
    preview_ok = False  # THIS sync produced a fresh preview.pdf
    pf = (b.get("pdf_file") or "").strip()
    if pf:
        sp = _resolve_local(pf)
        if sp is not None and sp.is_file():
            src = sp
        else:
            notes.append("pdf_file not found")
    if src is not None:
        try:
            prev = _preview_pdf(src, pages)
            import shutil
            shutil.copyfile(prev, d / "preview.pdf")
            preview_ok = True
        except Exception as exc:
            notes.append(f"preview failed: {exc}")
        try:
            total, shown, text = _pdf_extract_text(src, 400)
            if text.strip():
                (d / "ocr" / "extracted.txt").write_text(
                    text, encoding="utf-8", errors="replace")
            else:
                notes.append("no text layer (supply OCR separately)")
        except Exception as exc:
            notes.append(f"text extraction failed: {exc}")
        # IA originals are temporary artifacts unless configured otherwise.
        # Only a preview produced by THIS sync may cost the original — a
        # leftover preview.pdf from an earlier run does not count.
        if not keep_original and preview_ok:
            try:
                srcr = src.resolve()
                if srcr.is_relative_to(lib.IA_DOWNLOADS_DIR.resolve()):
                    src.unlink()
                    notes.append("original removed (temporary artifact)")
                    # nothing may keep pointing at the deleted file: the
                    # entry folder's preview becomes the build's PDF, and
                    # the IA download catalog entry is retired
                    b["pdf_file"] = (d / "preview.pdf").resolve().relative_to(
                        lib.ROOT.resolve()).as_posix()
                    b["updated_at"] = datetime.now(timezone.utc).isoformat(
                        timespec="seconds")
                    lib.save_json(BUILDS_PATH, builds)
                    catalog = lib.load_json(lib.IA_CATALOG_PATH, {})
                    stale = [k for k, v in catalog.items()
                             if (lib.ROOT / str(v.get("saved_as") or "?")).resolve()
                             == srcr]
                    for k in stale:
                        del catalog[k]
                    if stale:
                        lib.save_json(lib.IA_CATALOG_PATH, catalog)
            except Exception as exc:
                notes.append(f"original cleanup failed: {exc}")
    out = _entry_folder_info(build_id)
    out.update({"ok": True, "notes": notes, "build": b})
    return jsonify(out)


@app.route("/api/entries")
def api_entries():
    """Folder info for every build that has an entry folder — one pass, so
    the OCR tab's book list doesn't need a request per build."""
    builds = lib.load_json(BUILDS_PATH, {})
    out = {}
    for bid in builds:
        info = _entry_folder_info(bid)
        if info["exists"]:
            out[bid] = {"ocr": info["ocr"], "preview": info["preview"]}
    return jsonify({"entries": out})


@app.route("/api/builds/<build_id>/ocr/<name>")
def api_build_ocr_get(build_id: str, name: str):
    # membership check doubles as path validation for the build_id segment
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    f = _entry_dir(build_id) / "ocr" / _ocr_name(name)
    if not f.is_file():
        abort(404)
    return jsonify({"ok": True, "name": f.name,
                    "text": f.read_text(encoding="utf-8", errors="replace")})


@app.route("/api/builds/<build_id>/ocr", methods=["POST"])
def api_build_ocr_put(build_id: str):
    """Store an OCR text file on the entry folder. Body: {name, text}."""
    if build_id not in lib.load_json(BUILDS_PATH, {}):
        abort(404)
    p = request.get_json(silent=True) or {}
    name = _ocr_name(p.get("name") or "")
    d = _entry_dir(build_id) / "ocr"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(str(p.get("text") or ""),
                          encoding="utf-8", errors="replace")
    return jsonify({"ok": True, "name": name,
                    "folder": _entry_folder_info(build_id)})


# --- PDF page rasterization (the OCR tab's side-by-side page view) ---------------

def _pageimg_pdf(raw: str) -> Path:
    p = _resolve_local(raw or "")
    if p is None or p.suffix.lower() != ".pdf" or not p.is_file():
        abort(404)
    return p


@app.route("/api/pdf/info")
def api_pdf_info():
    """Page count of a local PDF."""
    p = _pageimg_pdf(request.args.get("path"))
    try:
        from pypdf import PdfReader
        return jsonify({"ok": True, "pages": len(PdfReader(str(p)).pages)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


@app.route("/api/pdf/pageimg")
def api_pdf_pageimg():
    """One page of a local PDF rendered as a PNG (?path=&page=N&w=W).
    Rendered via PyMuPDF and cached on disk by path+mtime+page+width."""
    p = _pageimg_pdf(request.args.get("path"))
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        w = max(200, min(1600, int(request.args.get("w") or 700)))
    except ValueError:
        w = 700
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return jsonify({"ok": False, "error": "PyMuPDF is not installed"}), 501
    import hashlib
    cache = lib.ROOT / "downloads" / "cache" / "pages"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(
        f"{p}|{p.stat().st_mtime}|{page}|{w}".encode("utf-8")).hexdigest()[:16]
    out = cache / f"{key}.png"
    if not out.is_file():
        doc = fitz.open(str(p))
        try:
            if page > doc.page_count:
                abort(404)
            pg = doc[page - 1]
            zoom = w / max(1.0, pg.rect.width)
            pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            # pymupdf infers the format from the extension — the tmp name
            # must end in .png
            tmp = out.with_suffix(f".{page}.tmp.png")
            pix.save(str(tmp))
            tmp.replace(out)
        finally:
            doc.close()
    return send_file(out, mimetype="image/png", conditional=True)


_PDF_TEXT_CACHE: dict = {}


@app.route("/api/pdf/text")
def api_pdf_text():
    """Extract the text (OCR) layer of a PDF — a local path, or a remote URL
    that is fetched once into downloads/cache/."""
    raw_path = (request.args.get("path") or "").strip()
    url = (request.args.get("url") or "").strip()
    try:
        max_pages = max(1, min(500, int(request.args.get("pages") or 100)))
    except ValueError:
        max_pages = 100
    if raw_path:
        p = _resolve_local(raw_path)
        if p is None or not p.is_file():
            abort(404)
    elif url:
        if not url.lower().startswith(("http://", "https://")):
            abort(400)
        import hashlib
        cache_dir = lib.ROOT / "downloads" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        p = cache_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".pdf")
        if not p.exists():
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": whl_client.USER_AGENT})
                with urllib.request.urlopen(req, timeout=90) as resp, \
                        open(p, "wb") as fh:
                    import shutil
                    shutil.copyfileobj(resp, fh)
            except Exception as exc:
                p.unlink(missing_ok=True)
                return jsonify({"ok": False,
                                "error": f"fetch failed: {exc}"})
    else:
        abort(400)
    key = (str(p), p.stat().st_mtime, max_pages)
    if key in _PDF_TEXT_CACHE:
        return jsonify(_PDF_TEXT_CACHE[key])
    try:
        from pypdf import PdfReader  # noqa: F401
    except ImportError:
        return jsonify({"ok": False,
                        "error": "pypdf is not installed "
                                 "(python3 -m pip install pypdf)"})
    try:
        total, shown, text = _pdf_extract_text(p, max_pages)
        out = {"ok": True, "pages": total, "shown": shown, "text": text}
    except Exception as exc:
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _PDF_TEXT_CACHE[key] = out
    return jsonify(out)


@app.route("/api/pdf/browse")
def api_pdf_browse():
    """List a directory's subdirectories and PDF files (the file picker)."""
    raw = (request.args.get("dir") or "").strip()
    d = _resolve_local(raw) if raw else lib.IA_DOWNLOADS_DIR
    if d is None or not d.is_dir():
        d = lib.ROOT
    dirs: list[dict] = []
    pdfs: list[dict] = []
    try:
        for entry in sorted(d.iterdir(), key=lambda p: p.name.lower()):
            try:
                if entry.is_dir():
                    if not entry.name.startswith("."):
                        dirs.append({"name": entry.name, "path": str(entry)})
                elif entry.suffix.lower() == ".pdf":
                    pdfs.append({"name": entry.name, "path": str(entry),
                                 "size": entry.stat().st_size})
            except OSError:
                continue
    except OSError:
        pass
    parent = str(d.parent) if d.parent != d else None
    return jsonify({"dir": str(d), "parent": parent, "dirs": dirs,
                    "pdfs": pdfs, "drives": _drives()})


_DRIVES_CACHE: list[str] | None = None


def _drives() -> list[str]:
    """Available drive roots; probed once (floppy-era letters are slow)."""
    global _DRIVES_CACHE
    if _DRIVES_CACHE is None:
        _DRIVES_CACHE = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                         if Path(f"{c}:\\").exists()]
    return _DRIVES_CACHE


# --- manual entries (checked offline on submit) ------------------------------

def _entry_checks(entry: dict) -> dict:
    """Copyright + local-WHL checks; a check failure must not block the save."""
    try:
        return checks.check_entry(
            entry.get("title", ""), entry.get("author", ""), entry.get("year", "")
        )
    except Exception as exc:  # unexpected CSV/parse trouble
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.route("/api/manual")
def api_manual_list():
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    out = sorted(entries.values(), key=lambda e: e.get("created_at", ""), reverse=True)
    return jsonify(out)


@app.route("/api/manual", methods=["POST"])
def api_manual_add():
    payload = request.get_json(silent=True) or {}
    entry = {f: str(payload.get(f, "") or "").strip() for f in lib.MANUAL_ENTRY_FIELDS}
    if not entry["title"]:
        return jsonify({"ok": False, "error": "TITLE IS REQUIRED"}), 400

    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    entry["id"] = lib.gen_id(set(entries))
    entry["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry["checks"] = _entry_checks(entry)
    entries[entry["id"]] = entry
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/manual/<entry_id>", methods=["PATCH"])
def api_manual_update(entry_id: str):
    """Update fields of a manual entry; metadata changed, so re-run the
    offline checks and drop the stale scan results (the client re-scans).

    "_preserve": true keeps checks/scans/verifications — used for changes
    that don't alter the book's identity (title parsing migration, attaching
    a local scan PDF)."""
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    payload = request.get_json(silent=True) or {}
    e = entries[entry_id]
    for f in lib.MANUAL_ENTRY_FIELDS:
        if f in payload:
            e[f] = str(payload[f] or "").strip()
    if not e.get("title"):
        return jsonify({"ok": False, "error": "TITLE IS REQUIRED"}), 400
    if not payload.get("_preserve"):
        e["checks"] = _entry_checks(e)
        # Metadata changed: stored matches and their verifications are stale.
        e.pop("scans", None)
        e.pop("verify", None)
        e.pop("manual_urls", None)
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


@app.route("/api/manual/restore", methods=["POST"])
def api_manual_restore():
    """Reinsert a previously deleted entry verbatim (undo of a delete).

    The client sends back the full entry object it received from this server
    before the deletion, so checks/scans/verifications survive the round trip.
    """
    payload = request.get_json(silent=True) or {}
    entry = payload.get("entry") or {}
    eid = str(entry.get("id") or "")
    if not eid or not str(entry.get("title", "") or "").strip():
        abort(400)
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    entries[eid] = entry
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/manual/<entry_id>", methods=["DELETE"])
def api_manual_delete(entry_id: str):
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    del entries[entry_id]
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True})


@app.route("/api/manual/<entry_id>/scans", methods=["POST"])
def api_manual_scans(entry_id: str):
    """Run the IA + HathiTrust scan search and persist it on the entry."""
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    e = entries[entry_id]
    scans = scan_search.search_scans(
        e.get("title", ""), e.get("author") or None, e.get("year") or None
    )
    # The scan search is slow (network): the entry may have been edited in
    # the meantime. Re-read and merge only the scans, so this request can't
    # resurrect a stale snapshot of the other fields.
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    e = entries[entry_id]
    e["scans"] = scans
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


@app.route("/api/manual/<entry_id>/verify", methods=["POST"])
def api_manual_verify(entry_id: str):
    """Record the per-source verification of a matched record.

    Body: {"source": "whl"|"internet_archive"|"hathitrust",
           "state": "approved"|"rejected"|"pending"}.
    'rejected' marks the match as a false positive; 'pending' clears the
    verification.
    """
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source", "") or "")
    verdict = str(payload.get("state", "") or "")
    if source not in ("whl", "internet_archive", "hathitrust") or \
            verdict not in ("approved", "rejected", "pending"):
        abort(400)
    e = entries[entry_id]
    verify = e.setdefault("verify", {})
    if verdict == "pending":
        verify.pop(source, None)
    else:
        verify[source] = verdict
    if verdict != "rejected":
        # A manually located source only exists alongside a rejected match.
        (e.get("manual_urls") or {}).pop(source, None)
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


@app.route("/api/manual/<entry_id>/source", methods=["POST"])
def api_manual_source(entry_id: str):
    """Store the URL of a manually located source for a rejected match.

    Body: {"source": "whl"|"internet_archive"|"hathitrust", "url": "..."};
    an empty url clears it.
    """
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    if entry_id not in entries:
        abort(404)
    payload = request.get_json(silent=True) or {}
    source = str(payload.get("source", "") or "")
    url = str(payload.get("url", "") or "").strip()
    if source not in ("whl", "internet_archive", "hathitrust"):
        abort(400)
    e = entries[entry_id]
    urls = e.setdefault("manual_urls", {})
    if url:
        urls[source] = url
    else:
        urls.pop(source, None)
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)
    return jsonify({"ok": True, "entry": e})


# --- Internet Archive PDF downloads --------------------------------------------

_downloads: dict[str, dict] = {}
_downloads_lock = threading.Lock()


def _ia_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": scan_search.USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick_pdf(files: list) -> dict | None:
    """Choose the item's PDF derivative ('Text PDF' preferred)."""
    best = None
    for f in files:
        name = str(f.get("name", "") or "")
        if not name.lower().endswith(".pdf"):
            continue
        if (f.get("format") or "").lower() == "text pdf":
            return f
        if best is None:
            best = f
    return best


def _ia_pdf_path(identifier: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", identifier)
    return lib.IA_DOWNLOADS_DIR / f"{safe}.pdf"


def _ia_download_job(identifier: str, book: dict) -> None:
    """Download the item's PDF and write a cataloging entry (runs in a thread)."""
    job = _downloads[identifier]
    try:
        info = _ia_get_json(f"https://archive.org/metadata/{urllib.parse.quote(identifier)}")
        pdf = _pick_pdf(info.get("files") or [])
        if not pdf:
            raise RuntimeError("no PDF derivative on this item")
        name = pdf["name"]
        url = (
            "https://archive.org/download/"
            + urllib.parse.quote(identifier) + "/" + urllib.parse.quote(name)
        )
        lib.IA_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        dest = _ia_pdf_path(identifier)
        tmp = dest.with_suffix(".part")
        req = urllib.request.Request(url, headers={"User-Agent": scan_search.USER_AGENT})
        got = 0
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            job["total"] = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                job["bytes"] = got
        tmp.replace(dest)

        # Cataloging entry: our book metadata + where the scan came from.
        meta = info.get("metadata") or {}
        catalog = lib.load_json(lib.IA_CATALOG_PATH, {})
        catalog[identifier] = {
            "identifier": identifier,
            "source_url": f"https://archive.org/details/{identifier}",
            "pdf_file": name,
            "saved_as": str(dest.relative_to(lib.ROOT)),
            "size_bytes": got,
            "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ia_title": meta.get("title", ""),
            "ia_creator": meta.get("creator", ""),
            "ia_date": meta.get("date", ""),
            "book": book,
        }
        lib.save_json(lib.IA_CATALOG_PATH, catalog)
        job["status"] = "done"
        job["path"] = str(dest.relative_to(lib.ROOT))
    except Exception as exc:
        job["status"] = "error"
        job["error"] = f"{type(exc).__name__}: {exc}"


def _download_state(identifier: str) -> dict:
    job = _downloads.get(identifier)
    if job:
        return {"identifier": identifier, **{k: v for k, v in job.items() if k != "thread"}}
    catalog = lib.load_json(lib.IA_CATALOG_PATH, {})
    if identifier in catalog and _ia_pdf_path(identifier).exists():
        return {"identifier": identifier, "status": "done",
                "path": catalog[identifier].get("saved_as", "")}
    return {"identifier": identifier, "status": "none"}


@app.route("/api/ia/download", methods=["POST"])
def api_ia_download():
    payload = request.get_json(silent=True) or {}
    identifier = str(payload.get("identifier", "") or "").strip()
    if not identifier:
        abort(400)
    book = payload.get("book") or {}
    with _downloads_lock:
        current = _download_state(identifier)
        if current["status"] in ("downloading", "done"):
            return jsonify(current)
        _downloads[identifier] = {"status": "downloading", "bytes": 0, "total": 0}
        threading.Thread(
            target=_ia_download_job, args=(identifier, book), daemon=True
        ).start()
    return jsonify(_download_state(identifier))


@app.route("/api/ia/download/<path:identifier>")
def api_ia_download_status(identifier: str):
    return jsonify(_download_state(identifier))


@app.route("/api/ia/downloads")
def api_ia_downloads():
    return jsonify(lib.load_json(lib.IA_CATALOG_PATH, {}))


# --- Open Library indexes (constrained search + realtime + autocomplete) --------

@app.route("/api/ol/status")
def api_ol_status():
    st = ol_client.db_stats()
    st["editions"] = ol_client.editions_index_stats()
    return jsonify(st)


def _ol_params():
    p = request.args
    try:
        limit = min(int(p.get("limit", 12) or 12), 100)
    except ValueError:
        limit = 12
    return {
        "title": (p.get("title") or "").strip(),
        "author": (p.get("author") or "").strip(),
        "year": (p.get("year") or "").strip(),
        "edition": (p.get("edition") or "").strip(),
        "volume": (p.get("volume") or "").strip(),
        "publisher": (p.get("publisher") or "").strip(),
        "city": (p.get("city") or "").strip(),
        "limit": limit,
    }


@app.route("/api/ol/search")
def api_ol_search():
    params = _ol_params()
    # The consolidated editions index answers everything locally; the works
    # index (+ live API) is only the fallback while it hasn't been built.
    if ol_client.editions_index_available():
        return jsonify(ol_client.search_editions(**params))
    return jsonify(ol_client.search_works(
        **params, deep=(request.args.get("deep") or "") in ("1", "true")))


@app.route("/api/ol/realtime")
def api_ol_realtime():
    """Search-as-you-type endpoint for the bottom-pane Open Library table."""
    params = _ol_params()
    if ol_client.editions_index_available():
        verbatim = (request.args.get("title_verbatim") or "") in ("1", "true")
        return jsonify(ol_client.search_editions(**params, title_verbatim=verbatim))
    out = ol_client.search_works(
        title=params["title"], author=params["author"], year=params["year"],
        edition=params["edition"], volume=params["volume"],
        publisher=params["publisher"], city=params["city"],
        limit=params["limit"], deep=False)
    out["kind"] = "work"
    return jsonify(out)


# --- WHL catalogue view (editable via a corrections overlay) --------------------

WHL_CORRECTIONS_PATH = lib.OUTPUT_DIR / "whl_corrections.json"
_whl_rows_cache: list | None = None
_whl_rows_lock = threading.Lock()

# The catalogue export lacks subtitle/description/publisher/pages/language/
# subject (they exist on the WHL website); those columns are filled by the
# scraper (tools/whl_scrape.py) and refined via corrections.
_WHL_EDIT_FIELDS = ("title", "subtitle", "authors", "year", "categories",
                    "description", "publisher", "pages", "language", "subject")


def _load_whl_base() -> list[dict]:
    """whl_catalog.csv rows with stable indexes (cached; the CSV is static)."""
    global _whl_rows_cache
    with _whl_rows_lock:
        if _whl_rows_cache is None:
            rows = []
            path = checks.WHL_CATALOG_CSV
            if path.exists():
                import csv
                with open(path, "r", encoding="utf-8-sig", errors="replace",
                          newline="") as fh:
                    for i, raw in enumerate(csv.DictReader(fh)):
                        rows.append({
                            "idx": i,
                            "title": (raw.get("Title") or "").strip(),
                            "subtitle": "",
                            "authors": (raw.get("Authors") or "").strip(),
                            "year": whl_client._year(raw.get("Year Published")) or "",
                            "categories": (raw.get("Library Categories") or "").strip(),
                            "description": "",
                            "publisher": "",
                            "pages": "",
                            "language": "",
                            "subject": "",
                            "status": (raw.get("Status") or "").strip().lower(),
                            "permalink": (raw.get("Permalink") or "").strip(),
                            "file": (raw.get("Publication File") or "").strip(),
                        })
            _whl_rows_cache = rows
        return _whl_rows_cache


# Fields the scraper fills in when the CSV has nothing better.
_WHL_SCRAPED_FIELDS = ("subtitle", "description", "publisher", "pages",
                       "language", "subject")


def _permalink_slug(permalink: str) -> str:
    if "/catalog/" not in (permalink or ""):
        return ""  # drafts only have ?post_type=...&p= permalinks
    return permalink.rstrip("/").rsplit("/", 1)[-1]


def _merged_whl_rows() -> list[dict]:
    """Base CSV rows + scraped website metadata + the corrections overlay
    (in that precedence order); added rows first."""
    base = [dict(r) for r in _load_whl_base()]
    scraped = whl_scrape.load_scraped()
    if scraped:
        for r in base:
            s = scraped.get(_permalink_slug(r.get("permalink", "")))
            if not s:
                continue
            r["scraped"] = True
            for f in _WHL_SCRAPED_FIELDS:
                if s.get(f):
                    r[f] = s[f]
            # Scraped authors/year are authoritative where the CSV is blank.
            for f in ("authors", "year"):
                if not r.get(f) and s.get(f):
                    r[f] = s[f]
    corr = lib.load_json(WHL_CORRECTIONS_PATH, {})
    for sidx, edits in (corr.get("edits") or {}).items():
        try:
            i = int(sidx)
        except ValueError:
            continue
        if 0 <= i < len(base):
            # Keep the pre-correction values: the client shows the original
            # record while Alt is held over an edited row.
            orig = {}
            for f in _WHL_EDIT_FIELDS:
                if f in edits:
                    orig[f] = base[i].get(f, "")
                    base[i][f] = edits[f]
            base[i]["corrected"] = True
            base[i]["orig"] = orig
            # Which fields carry corrections — undo needs to know whether to
            # restore a previous correction or clear back to the CSV value.
            base[i]["edited_fields"] = [f for f in _WHL_EDIT_FIELDS if f in edits]
    added = []
    for j, a in enumerate(corr.get("added") or []):
        row = {f: a.get(f, "") for f in _WHL_EDIT_FIELDS}
        row.update({"idx": -(j + 1), "status": "added", "permalink": "",
                    "file": "", "added": True})
        added.append(row)
    added.reverse()  # newest first
    return added + base


@app.route("/api/whl_catalog")
def api_whl_catalog():
    return jsonify({"rows": _merged_whl_rows(),
                    "corrections": str(WHL_CORRECTIONS_PATH.name)})


# --- WHL website metadata scrape (background job) --------------------------------

_scrape_job: dict = {"status": "idle"}
_scrape_lock = threading.Lock()


def _run_scrape() -> None:
    try:
        whl_scrape.scrape_all(_scrape_job)
        _scrape_job["status"] = "done"
    except Exception as exc:
        _scrape_job["status"] = "error"
        _scrape_job["error"] = f"{type(exc).__name__}: {exc}"


@app.route("/api/whl_scrape", methods=["POST"])
def api_whl_scrape_start():
    with _scrape_lock:
        if _scrape_job.get("status") == "running":
            return jsonify(_scrape_job)
        _scrape_job.clear()
        _scrape_job.update({"status": "running", "page": 0, "pages": 0, "records": 0})
        threading.Thread(target=_run_scrape, daemon=True).start()
    return jsonify(_scrape_job)


@app.route("/api/whl_scrape/status")
def api_whl_scrape_status():
    out = dict(_scrape_job)
    out["scraped_total"] = len(whl_scrape.load_scraped())
    return jsonify(out)


@app.route("/api/whl_catalog", methods=["POST"])
def api_whl_catalog_edit():
    """Record corrections: {idx, field, value}, {idx, fields: {..}} for a
    multi-field repopulation, or {add: {...}} for a new row.

    The CSV export itself is never modified; changes live in
    output/whl_corrections.json so they are reviewable and revertible.
    """
    payload = request.get_json(silent=True) or {}
    corr = lib.load_json(WHL_CORRECTIONS_PATH, {})
    if "add" in payload:
        a = payload.get("add") or {}
        row = {f: str(a.get(f, "") or "").strip() for f in _WHL_EDIT_FIELDS}
        if not row["title"]:
            return jsonify({"ok": False, "error": "TITLE IS REQUIRED"}), 400
        corr.setdefault("added", []).append(row)
        lib.save_json(WHL_CORRECTIONS_PATH, corr)
        return jsonify({"ok": True, "idx": -len(corr["added"])})

    if "remove_added" in payload:  # undo of an add
        try:
            j = -int(payload["remove_added"]) - 1
        except (TypeError, ValueError):
            abort(400)
        added = corr.get("added") or []
        if not (0 <= j < len(added)):
            abort(404)
        added.pop(j)
        lib.save_json(WHL_CORRECTIONS_PATH, corr)
        return jsonify({"ok": True})

    fields = {f: str(v or "").strip() for f, v in (payload.get("fields") or {}).items()
              if f in _WHL_EDIT_FIELDS}
    if "field" in payload:
        field = str(payload.get("field", "") or "")
        if field not in _WHL_EDIT_FIELDS:
            abort(400)
        fields[field] = str(payload.get("value", "") or "").strip()
    clear = [f for f in (payload.get("clear_fields") or []) if f in _WHL_EDIT_FIELDS]
    if not fields and not clear:
        abort(400)
    try:
        idx = int(payload.get("idx"))
    except (TypeError, ValueError):
        abort(400)
    if idx >= 0:
        if idx >= len(_load_whl_base()):
            abort(404)
        edits = corr.setdefault("edits", {}).setdefault(str(idx), {})
        edits.update(fields)
        for f in clear:  # drop the correction entirely -> CSV value shows again
            edits.pop(f, None)
        if not edits:
            corr["edits"].pop(str(idx), None)
    else:
        added = corr.get("added") or []
        j = -idx - 1
        if j >= len(added):
            abort(404)
        added[j].update(fields)
        for f in clear:
            added[j][f] = ""
    lib.save_json(WHL_CORRECTIONS_PATH, corr)
    return jsonify({"ok": True})


@app.route("/api/ol/editions")
def api_ol_editions():
    work = (request.args.get("work") or "").strip()
    if not work:
        abort(400)
    constraints = {f: (request.args.get(f) or "").strip()
                   for f in ("publisher", "city", "year", "edition", "volume")}
    try:
        info = ol_client.best_edition(work, constraints)
        info["ok"] = True
        return jsonify(info)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# --- offline checks + scan search for arbitrary books --------------------------

@app.route("/api/check")
def api_check():
    """Offline copyright + local-WHL check for a title/author/year triple."""
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    if not title:
        abort(400)
    return jsonify(checks.check_entry(title, author, year))


@app.route("/api/scans")
def api_scans():
    title = (request.args.get("title") or "").strip()
    author = (request.args.get("author") or "").strip()
    year = (request.args.get("year") or "").strip()
    if not title:
        abort(400)
    return jsonify(scan_search.search_scans(title, author or None, year or None))


if __name__ == "__main__":
    # Warm the offline check indexes (the renewals CSV is ~40 MB) so the first
    # manual-entry submission doesn't stall while they load, and the drive
    # list so the first file-browser open is instant.
    threading.Thread(
        target=lambda: (checks.get_renewals(), checks.get_whl_catalog(),
                        _drives()),
        daemon=True,
    ).start()
    app.run(host="127.0.0.1", port=5001, debug=False)
