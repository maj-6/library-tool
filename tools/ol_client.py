"""Constrained search over the local Open Library works index.

The SQLite index built by tools/build_ol_index.py answers title queries
instantly and offline, but works records only carry author KEYS and no
edition data. This module fills the gaps through the Open Library API with
aggressive on-disk caching:

  - author names        /authors/<key>.json
  - author constraints  /search/authors.json?q=  (name -> keys, filters works)
  - publisher / city / year / edition / volume
                        /works/<key>/editions.json, ranked against the
                        caller's constraints

search_works() is the single entry point for both the explorer's SEARCH tab
(deep=True: edition-level constraints verified per candidate) and the manual
entry autocomplete (deep=False: instant, title/author/year only).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libcommon as lib  # noqa: E402
import whl_client as whl  # noqa: E402

DB_PATH = lib.OUTPUT_DIR / "ol_works.db"
SEARCH_DB_PATH = lib.OUTPUT_DIR / "ol_search.db"
CACHE_PATH = lib.OUTPUT_DIR / ".ol_api_cache.json"
OL_API = "https://openlibrary.org"
USER_AGENT = "world-herb-library-tools/1.0"
TIMEOUT = 15.0

FTS_SHORTLIST = 400   # candidate rows pulled from FTS before re-ranking
DEEP_CANDIDATES = 10  # works whose editions are fetched in a deep search

_VOL_RE = re.compile(
    r"\b(?:v(?:ol(?:ume)?)?|t(?:ome)?|band|pt|part)\.?\s*(\d+)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"(1[0-9]{3}|20[0-9]{2})")

# --- api cache (author names / author searches survive restarts) --------------

_cache_lock = threading.Lock()
_cache: dict = {"authors": {}, "author_search": {}}
_cache_loaded = False
_cache_dirty = 0
_editions_cache: dict[str, list] = {}  # per-process only (bulky)
_EDITIONS_CACHE_MAX = 300
# Failed author lookups are retried only after a cool-down, so a slow or
# throttling API is not hammered again on every autocomplete keystroke.
_fail_at: dict[str, float] = {}
_FAIL_TTL = 120.0

# Shared executors (created once; per-call pools would pile up under the
# threaded Flask server).
_names_pool = ThreadPoolExecutor(max_workers=6)
_deep_pool = ThreadPoolExecutor(max_workers=6)


def _load_cache() -> None:
    global _cache, _cache_loaded
    if not _cache_loaded:
        try:
            data = lib.load_json(CACHE_PATH, {})
        except Exception:  # corrupt cache file: start fresh
            data = {}
        _cache = {"authors": data.get("authors", {}),
                  "author_search": data.get("author_search", {})}
        _cache_loaded = True


def _save_cache(force: bool = False) -> None:
    """Persist the cache atomically (called with _cache_lock held)."""
    global _cache_dirty
    _cache_dirty += 1
    if force or _cache_dirty >= 20:
        _cache_dirty = 0
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(_cache, fh, ensure_ascii=False)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass  # cache persistence is best-effort


def _get_json(url: str, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --- database -----------------------------------------------------------------

def db_available() -> bool:
    return DB_PATH.exists()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def db_stats() -> dict:
    if not db_available():
        return {"available": False, "works": 0}
    try:
        con = _connect()
        n = con.execute("SELECT max(id) FROM works").fetchone()[0] or 0
        con.close()
        return {"available": True, "works": n}
    except Exception as exc:
        return {"available": False, "works": 0, "error": str(exc)}


def _fts_expr(title: str) -> str:
    words = re.findall(r"\w+", title, re.UNICODE)
    if not words:
        return ""
    terms = [f'"{w}"' for w in words[:-1]]
    terms.append(f'"{words[-1]}"*')  # last word may still be being typed
    return " ".join(terms)


# --- consolidated editions index (ol_search.db, built by build_ol_search.py) ---

def editions_index_available() -> bool:
    return SEARCH_DB_PATH.exists()


def editions_index_stats() -> dict:
    if not editions_index_available():
        return {"available": False, "editions": 0}
    try:
        con = sqlite3.connect(f"file:{SEARCH_DB_PATH.as_posix()}?mode=ro", uri=True)
        n = con.execute("SELECT max(id) FROM ed").fetchone()[0] or 0
        con.close()
        return {"available": True, "editions": n}
    except Exception as exc:
        return {"available": False, "editions": 0, "error": str(exc)}


def _fts_col_terms(text: str, prefix_all: bool = False, min_len: int = 1) -> str:
    """Quoted FTS terms for one column filter; optionally all prefix-starred."""
    words = [w for w in re.findall(r"\w+", text or "", re.UNICODE) if len(w) >= min_len]
    if not words:
        return ""
    if prefix_all:
        return " ".join(f'"{w}"*' for w in words)
    terms = [f'"{w}"' for w in words[:-1]]
    terms.append(f'"{words[-1]}"*')
    return " ".join(terms)


def _author_terms(author: str) -> str:
    """Author constraint terms: surname-ish tokens (initials match nothing
    against the full names stored in the index)."""
    import catalog_checks as checks
    toks = sorted(checks.author_tokens(author or ""))
    if not toks:
        toks = [w for w in re.findall(r"\w+", author or "") if len(w) >= 3]
    return " ".join(f'"{t}"' for t in toks)


def search_editions(
    title: str = "",
    author: str = "",
    publisher: str = "",
    city: str = "",
    year: str = "",
    edition: str = "",
    volume: str = "",
    limit: int = 30,
) -> dict:
    """Realtime constrained search of the consolidated editions index.

    Everything is local: title/author/publisher/place hit the column-filtered
    FTS index (prefix-indexed, so search-as-you-type is fast); year and
    volume filter exactly in SQL; the edition-name constraint is a substring
    match. No Open Library API calls.
    """
    out: dict = {"results": [], "kind": "edition",
                 "db": editions_index_stats()}
    if not out["db"]["available"]:
        out["error"] = ("editions index not built — run tools/build_ol_search.py "
                        "(falling back to the works index)")
        return out

    match_parts = []
    tq = _fts_col_terms(title)
    if tq:
        match_parts.append(f"title:({tq})")
    if (author or "").strip():
        aq = _author_terms(author)
        if aq:
            match_parts.append(f"authors:({aq})")
    pq = _fts_col_terms(publisher, prefix_all=True, min_len=3)
    if pq:
        match_parts.append(f"publisher:({pq})")
    cq = _fts_col_terms(city, prefix_all=True, min_len=3)
    if cq:
        match_parts.append(f"place:({cq})")

    want_year = whl._year(year)
    want_vol = re.sub(r"\D", "", str(volume or ""))
    ed_like = f"%{(edition or '').strip().lower()}%" if (edition or "").strip() else ""

    filters, args = [], []
    if want_year:
        filters.append("e.year = ?")
        args.append(int(want_year))
    if want_vol:
        filters.append("e.volume = ?")
        args.append(want_vol)
    if ed_like:
        filters.append("lower(coalesce(e.edition, '')) LIKE ?")
        args.append(ed_like)

    con = sqlite3.connect(f"file:{SEARCH_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        if match_parts:
            sql = ("SELECT e.ekey, e.wkey, e.title, e.subtitle, e.authors, e.year,"
                   " e.publisher, e.city, e.edition, e.volume, e.language, e.pages"
                   " FROM ed_fts f JOIN ed e ON e.id = f.rowid"
                   " WHERE ed_fts MATCH ?")
            qargs = [" AND ".join(match_parts)]
            if filters:
                sql += " AND " + " AND ".join(filters)
                qargs += args
            sql += " ORDER BY rank LIMIT ?"
            qargs.append(limit)
            rows = con.execute(sql, qargs).fetchall()
        elif filters:
            sql = ("SELECT e.ekey, e.wkey, e.title, e.subtitle, e.authors, e.year,"
                   " e.publisher, e.city, e.edition, e.volume, e.language, e.pages"
                   " FROM ed e WHERE " + " AND ".join(filters) + " LIMIT ?")
            rows = con.execute(sql, args + [limit]).fetchall()
        else:
            out["error"] = "enter something to search for"
            return out
    finally:
        con.close()

    for (ekey, wkey, rtitle, rsub, rauthors, ryear, rpub, rcity, red, rvol,
         rlang, rpages) in rows:
        out["results"].append({
            "kind": "edition",
            "key": ekey,
            "work_key": wkey or "",
            "title": rtitle,
            "subtitle": rsub or "",
            "authors": [a.strip() for a in (rauthors or "").split(";") if a.strip()],
            "year": str(ryear) if ryear else "",
            "first_year": ryear,
            "publisher": rpub or "",
            "city": rcity or "",
            "edition": red or "",
            "volume": rvol or "",
            "language": rlang or "",
            "pages": rpages or "",
            "url": f"{OL_API}/books/{ekey}",
        })
    return out


# --- author resolution ----------------------------------------------------------

def author_names(keys: list[str], timeout: float = 5.0) -> list[str]:
    """Resolve author keys to names via the OL API (cached; '?' on failure).

    Failures are negative-cached for a couple of minutes so autocomplete
    keystrokes do not refetch the same dead keys.
    """
    _load_cache()
    now = time.monotonic()
    missing = [k for k in keys
               if k not in _cache["authors"] and now - _fail_at.get(k, 0) > _FAIL_TTL]

    def fetch(key: str):
        try:
            d = _get_json(f"{OL_API}/authors/{urllib.parse.quote(key)}.json", timeout)
            return key, str(d.get("name") or "?")
        except Exception:
            return key, None  # transient: negative-cache briefly

    if missing:
        for key, name in _names_pool.map(fetch, missing):
            with _cache_lock:
                if name is not None:
                    _cache["authors"][key] = name
                    _fail_at.pop(key, None)
                else:
                    _fail_at[key] = time.monotonic()
        with _cache_lock:
            _save_cache()
    return [_cache["authors"].get(k, "?") for k in keys]


def author_search_keys(author: str) -> list[str] | None:
    """Author-name constraint -> plausible OL author keys (cached).

    Tries the full (order-flipped) name first, then surname tokens only, so
    'Spach, M. E.' still finds 'Édouard Spach'. Returns [] when Open Library
    genuinely knows no such author, and None when the lookup itself failed
    (offline / API error) so the caller can drop the constraint instead of
    reporting a false 'no such author'.
    """
    _load_cache()
    import catalog_checks as checks  # local import: avoids cycles at module load
    queries = []
    flipped = whl.flip_author(author or "").strip()
    if flipped:
        queries.append(flipped)
    surname = " ".join(sorted(checks.author_tokens(author or "")))
    if surname and surname.lower() != flipped.lower():
        queries.append(surname)

    failed = False
    for q in queries:
        ck = q.lower()
        if ck in _cache["author_search"]:
            keys = _cache["author_search"][ck]
        elif time.monotonic() - _fail_at.get("q:" + ck, 0) <= _FAIL_TTL:
            failed = True  # recently failed; don't hammer the API per keystroke
            continue
        else:
            try:
                data = _get_json(f"{OL_API}/search/authors.json?"
                                 + urllib.parse.urlencode({"q": q, "limit": 12}), 8.0)
                docs = data.get("docs") or []
                keys = [str(d.get("key", "")).rsplit("/", 1)[-1]
                        for d in docs if d.get("key")]
                with _cache_lock:
                    for d in docs:  # names come along for free
                        k = str(d.get("key", "")).rsplit("/", 1)[-1]
                        if k and d.get("name"):
                            _cache["authors"].setdefault(k, str(d["name"]))
                    _cache["author_search"][ck] = keys
                    _save_cache()
            except Exception:
                failed = True  # try the next query form; do not give up outright
                _fail_at["q:" + ck] = time.monotonic()
                continue
        if keys:
            return keys
    return None if failed else []


# --- editions -------------------------------------------------------------------

def _edition_year(e: dict) -> str:
    m = _YEAR_RE.search(str(e.get("publish_date", "") or ""))
    return m.group(1) if m else ""


def _edition_volume(e: dict) -> str:
    for text in (str(e.get("volume_number", "") or ""),
                 str(e.get("edition_name", "") or ""),
                 str(e.get("title", "") or ""),
                 str(e.get("full_title", "") or "")):
        if text.strip().isdigit():
            return text.strip()
        m = _VOL_RE.search(text)
        if m:
            return m.group(1)
    return ""


def _edition_fields(e: dict) -> dict:
    return {
        "edition_key": str(e.get("key", "") or "").rsplit("/", 1)[-1],
        "title": str(e.get("title", "") or ""),
        "publisher": "; ".join(str(p) for p in (e.get("publishers") or [])),
        "city": "; ".join(str(p) for p in (e.get("publish_places") or [])),
        "year": _edition_year(e),
        "edition": str(e.get("edition_name", "") or ""),
        "volume": _edition_volume(e),
    }


def fetch_editions(work_key: str) -> list[dict]:
    """All editions of a work (small FIFO per-process cache)."""
    if work_key in _editions_cache:
        return _editions_cache[work_key]
    data = _get_json(f"{OL_API}/works/{urllib.parse.quote(work_key)}/editions.json?limit=100")
    entries = [_edition_fields(e) for e in (data.get("entries") or [])]
    while len(_editions_cache) >= _EDITIONS_CACHE_MAX:
        _editions_cache.pop(next(iter(_editions_cache)))
    _editions_cache[work_key] = entries
    return entries


# Tokens too generic to prove a publisher/city/edition match on their own
# ('the' would otherwise "match" Theosophical, 'co.' Cologne, ...).
_STOP_TOKENS = {
    "the", "and", "for", "und", "der", "die", "das", "les", "des", "los",
    "las", "van", "von", "cie", "inc", "ltd", "son", "sons", "pub", "publ",
    "press", "company", "new", "edition", "printed",
}


def _text_match(want: str, have: str) -> bool | None:
    """Whole-word constraint match on normalized text; None = not decidable."""
    have_norm = " " + whl._normalize(have) + " "
    if not have_norm.strip():
        return None
    toks = [t for t in whl._normalize(want).split()
            if len(t) >= 3 and t not in _STOP_TOKENS]
    if not toks:
        return None  # constraint carries no distinctive token
    want_norm = whl._normalize(want)
    return any(f" {t} " in have_norm for t in toks) or \
        (want_norm and f" {want_norm} " in have_norm)


def _edition_score(ed: dict, constraints: dict) -> tuple[int, int]:
    """(violations, matches) of an edition against the user's constraints."""
    violations = matches = 0
    for field in ("publisher", "city", "edition"):
        want = (constraints.get(field) or "").strip()
        if not want:
            continue
        verdict = _text_match(want, ed.get(field, ""))
        if verdict is None:
            continue  # unknown is not a violation
        if verdict:
            matches += 1
        else:
            violations += 1
    want_year = whl._year(constraints.get("year"))
    if want_year:
        if ed.get("year") == want_year:
            matches += 1
        elif ed.get("year"):
            violations += 1
    want_vol = re.sub(r"\D", "", str(constraints.get("volume") or ""))
    if want_vol:
        if ed.get("volume") == want_vol:
            matches += 1
        elif ed.get("volume"):
            violations += 1
    return violations, matches


def best_edition(work_key: str, constraints: dict) -> dict:
    """The work's edition that best satisfies the constraints."""
    editions = fetch_editions(work_key)
    if not editions:
        return {"editions_count": 0, "best": None}
    scored = sorted(
        editions,
        key=lambda ed: (
            _edition_score(ed, constraints)[0],       # fewest violations
            -_edition_score(ed, constraints)[1],      # most matches
            # richer records first
            -sum(1 for f in ("publisher", "city", "year", "edition") if ed.get(f)),
        ))
    best = scored[0]
    return {
        "editions_count": len(editions),
        "best": best,
        "satisfies": _edition_score(best, constraints)[0] == 0,
    }


# --- search ----------------------------------------------------------------------

def search_works(
    title: str = "",
    author: str = "",
    year: str = "",
    edition: str = "",
    volume: str = "",
    publisher: str = "",
    city: str = "",
    limit: int = 12,
    deep: bool = False,
) -> dict:
    """Constrained search of the local works index.

    Local constraints: title (FTS), author (via cached OL author search),
    year (rank: matching first, unknown next, conflicting last), volume
    (title-pattern boost). With deep=True, edition-level constraints
    (publisher/city/edition/volume/year) are verified against each
    candidate's editions and the best edition is attached.
    """
    out: dict = {"results": [], "db": db_stats()}
    if not out["db"]["available"]:
        out["error"] = "Open Library index not built — run tools/build_ol_index.py"
        return out

    fts = _fts_expr(title or "")
    if (title or "").strip() and not fts:
        out["error"] = "the title contains no searchable characters"
        return out

    author_keys: list[str] | None = None
    if (author or "").strip():
        author_keys = author_search_keys(author)
        if author_keys == []:
            out["note"] = "author constraint matched no Open Library authors"
            return out
        if author_keys is None:  # lookup failed — drop the constraint, say so
            if not fts:
                out["error"] = ("author lookup unavailable (Open Library API "
                                "unreachable) and no title given")
                return out
            out["note"] = ("author lookup unavailable — searching without the "
                           "author constraint")
        else:
            out["author_keys"] = author_keys

    con = _connect()
    try:
        if fts and author_keys:
            # Filter in SQL so author-matching works are found even when the
            # title is common and the FTS shortlist would otherwise crowd
            # them out.
            marks = ",".join("?" for _ in author_keys)
            rows = con.execute(
                f"""SELECT DISTINCT w.id, w.key, w.title, w.subtitle, w.authors, w.year
                    FROM works_fts f
                    JOIN works w ON w.id = f.rowid
                    JOIN work_authors a ON a.work = w.id
                    WHERE works_fts MATCH ? AND a.author IN ({marks})
                    ORDER BY rank LIMIT ?""",
                (fts, *author_keys, FTS_SHORTLIST)).fetchall()
        elif fts:
            rows = con.execute(
                """SELECT w.id, w.key, w.title, w.subtitle, w.authors, w.year
                   FROM works_fts f JOIN works w ON w.id = f.rowid
                   WHERE works_fts MATCH ? ORDER BY rank LIMIT ?""",
                (fts, FTS_SHORTLIST)).fetchall()
        elif author_keys:
            marks = ",".join("?" for _ in author_keys)
            rows = con.execute(
                f"""SELECT DISTINCT w.id, w.key, w.title, w.subtitle, w.authors, w.year
                    FROM work_authors a JOIN works w ON w.id = a.work
                    WHERE a.author IN ({marks}) LIMIT ?""",
                (*author_keys, FTS_SHORTLIST)).fetchall()
        else:
            out["error"] = "enter at least a title or an author"
            return out
    finally:
        con.close()

    want_year = whl._year(year)
    want_vol = re.sub(r"\D", "", str(volume or ""))

    def rank(row):
        _, _, rtitle, rsub, _, ryear = row
        year_rank = 1  # unknown
        if want_year and ryear is not None:
            year_rank = 0 if str(ryear) == want_year else 2
        vol_rank = 1
        if want_vol:
            m = _VOL_RE.search(f"{rtitle} {rsub or ''}")
            if m:
                vol_rank = 0 if m.group(1) == want_vol else 2
        return (year_rank + vol_rank, rows.index(row))

    rows = sorted(rows, key=rank)[:max(limit, DEEP_CANDIDATES if deep else limit)]

    results = []
    for _, key, rtitle, rsub, akeys, ryear in rows:
        results.append({
            "key": key,
            "title": rtitle,
            "subtitle": rsub or "",
            "author_keys": (akeys or "").split(",") if akeys else [],
            "first_year": ryear,
            "url": f"{OL_API}/works/{key}",
        })

    # Resolve author names for what will actually be shown.
    shown = results[:limit]
    all_keys: list[str] = []
    for r in shown:
        all_keys.extend(k for k in r["author_keys"] if k not in all_keys)
    names = dict(zip(all_keys, author_names(all_keys))) if all_keys else {}
    for r in shown:
        r["authors"] = [names.get(k, "?") for k in r["author_keys"]]

    # Deep mode: verify edition-level constraints per candidate.
    edition_constraints = {"publisher": publisher, "city": city, "year": year,
                           "edition": edition, "volume": volume}
    if deep and any((v or "").strip() for v in edition_constraints.values()):
        def enrich(r):
            try:
                r["edition"] = best_edition(r["key"], edition_constraints)
            except Exception as exc:
                r["edition"] = {"error": f"{type(exc).__name__}: {exc}"}
            return r
        shown = list(_deep_pool.map(enrich, shown))
        # Candidates with a fully satisfying edition float to the top.
        shown.sort(key=lambda r: 0 if (r.get("edition") or {}).get("satisfies") else 1)

    out["results"] = shown
    return out
