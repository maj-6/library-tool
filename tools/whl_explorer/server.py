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
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

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

# The field set mirrors what a WHL catalog entry needs.
_BUILD_FIELDS = ("title", "subtitle", "authors", "year", "publisher",
                 "publisher_city", "edition", "language", "pages",
                 "categories", "description", "pdf_source", "source_url",
                 "notes", "status")


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
    offline checks and drop the stale scan results (the client re-scans)."""
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
    e["scans"] = scan_search.search_scans(
        e.get("title", ""), e.get("author") or None, e.get("year") or None
    )
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
            for f in _WHL_EDIT_FIELDS:
                if f in edits:
                    base[i][f] = edits[f]
            base[i]["corrected"] = True
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
    # manual-entry submission doesn't stall while they load.
    threading.Thread(
        target=lambda: (checks.get_renewals(), checks.get_whl_catalog()),
        daemon=True,
    ).start()
    app.run(host="127.0.0.1", port=5001, debug=False)
