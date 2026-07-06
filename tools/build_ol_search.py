"""Consolidate the three Open Library dumps into one fast local search index.

The books of interest are a tiny, old subset of Open Library, and the only
reliable "old" signal is the edition publish date — so this builds
output/ol_search.db from EDITIONS published up to --max-year (default 1950),
joining in author names (authors dump) and author keys via the works index
(output/ol_works.db, built by tools/build_ol_index.py). The result is a
single denormalized table + FTS5 index where title, author, publisher and
place are all searchable locally with prefix indexes — no Open Library API
calls on the search path at all.

Passes:
  A  authors dump   -> authors(key, name)            (~14M rows)
  B  editions dump  -> ed(...) rows, year <= cutoff  (regex prescreen, then JSON)
  C  fill missing author keys from ol_works.db; resolve names
  D  FTS5 build (title/authors/publisher/place, prefix='2 3 4') + indexes

Run with python3 (the full build takes a while; --limit smoke-tests):
  python3 tools/build_ol_search.py
  python3 tools/build_ol_search.py --limit 2000000 --max-year 1950
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libcommon as lib  # noqa: E402

DB_PATH = lib.OUTPUT_DIR / "ol_search.db"
WORKS_DB = lib.OUTPUT_DIR / "ol_works.db"

BATCH = 20000
_YEAR_RE = re.compile(r"(1[0-9]{3}|20[0-9]{2})")
_PUBDATE_RE = re.compile(r'"publish_date":\s*"([^"]{0,80})"')
_ROMAN_RE = re.compile(r"^[MDCLXVI]{4,}$")
_AUTHOR_KEY_RE = re.compile(r"^OL\d+A$")
_VOL_RE = re.compile(
    r"\b(?:v(?:ol(?:ume)?)?|t(?:ome)?|band|pt|part)\.?\s*(\d+)\b", re.IGNORECASE)

_ROMAN_VALS = {"M": 1000, "D": 500, "C": 100, "L": 50, "X": 10, "V": 5, "I": 1}


def _roman(s: str) -> int | None:
    total = 0
    for i, ch in enumerate(s):
        v = _ROMAN_VALS[ch]
        total += -v if i + 1 < len(s) and v < _ROMAN_VALS[s[i + 1]] else v
    return total if 1000 <= total <= 2100 else None


def _date_year(raw: str) -> int | None:
    """Year from a publish_date string; understands Roman-numeral dates."""
    m = _YEAR_RE.search(raw)
    if m:
        return int(m.group(1))
    token = re.sub(r"[^A-Z]", "", raw.upper())
    if _ROMAN_RE.match(token):
        return _roman(token)
    return None


def _find(pattern: str) -> Path | None:
    """Newest readable, non-growing file matching pattern (prefers .txt)."""
    hits = [Path(p) for p in
            sorted(glob.glob(str(lib.ROOT / (pattern + ".txt")))) +
            sorted(glob.glob(str(lib.ROOT / (pattern + ".txt.gz"))))]
    for p in hits:
        try:
            with open(p, "rb") as fh:
                fh.read(16)
        except OSError:
            continue
        size = p.stat().st_size
        time.sleep(1.0)
        if p.stat().st_size != size:
            print(f"skipping {p.name}: still growing", flush=True)
            continue
        return p
    return None


def _opener(p: Path):
    return gzip.open if p.suffix == ".gz" else open


def _open_db(path: Path) -> sqlite3.Connection:
    # uri=True also enables URI filenames for the later read-only ATTACH.
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=rwc", uri=True)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-200000")
    con.execute("""
        CREATE TABLE authors(key TEXT PRIMARY KEY, name TEXT) WITHOUT ROWID""")
    con.execute("""
        CREATE TABLE ed(
            id INTEGER PRIMARY KEY,
            ekey TEXT NOT NULL,
            wkey TEXT,
            title TEXT NOT NULL,
            subtitle TEXT,
            authors TEXT,
            akeys TEXT,
            year INTEGER,
            publisher TEXT,
            city TEXT,
            edition TEXT,
            volume TEXT,
            language TEXT,
            pages TEXT
        )""")
    # Contentless, column-filtered FTS with prefix indexes: search-as-you-type
    # stays fast even on short fragments.
    con.execute("""
        CREATE VIRTUAL TABLE ed_fts USING fts5(
            title, authors, publisher, place,
            content='', tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4')""")
    return con


# --- pass A: authors ----------------------------------------------------------

def load_authors(con: sqlite3.Connection, dump: Path, limit: int | None = None) -> int:
    t0 = time.time()
    n = kept = 0
    buf: list[tuple] = []
    with _opener(dump)(dump, "rt", encoding="utf-8", errors="replace") as fh:
        try:
            for line in fh:
                n += 1
                if limit and n > limit:
                    break
                parts = line.split("\t", 4)
                if len(parts) < 5 or parts[0] != "/type/author":
                    continue
                try:
                    d = json.loads(parts[4])
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                key = str(d.get("key") or parts[1]).rsplit("/", 1)[-1]
                name = str(d.get("name") or "").strip()
                if not name or not _AUTHOR_KEY_RE.match(key):
                    continue
                kept += 1
                buf.append((key, name))
                if len(buf) >= BATCH:
                    con.executemany(
                        "INSERT OR REPLACE INTO authors(key, name) VALUES(?,?)", buf)
                    buf.clear()
                if n % 2000000 == 0:
                    print(f"  authors: {n/1e6:.0f}M lines, {kept/1e6:.2f}M kept",
                          flush=True)
        except (EOFError, OSError) as exc:
            print(f"WARNING: authors stream ended abnormally after {n:,} lines: {exc}",
                  flush=True)
    if buf:
        con.executemany("INSERT OR REPLACE INTO authors(key, name) VALUES(?,?)", buf)
    print(f"pass A: {kept:,} authors in {(time.time()-t0)/60:.1f} min", flush=True)
    return kept


# --- pass B: editions ----------------------------------------------------------

def _clean_list(vals) -> str:
    return "; ".join(str(v).strip() for v in (vals or []) if str(v).strip())


def _edition_volume(d: dict, title: str) -> str:
    v = str(d.get("volume_number", "") or "").strip()
    if v.isdigit():
        return v
    for text in (str(d.get("edition_name", "") or ""), title,
                 str(d.get("full_title", "") or "")):
        m = _VOL_RE.search(text)
        if m:
            return m.group(1)
    return ""


def load_editions(con: sqlite3.Connection, dump: Path, max_year: int,
                  limit: int | None) -> int:
    t0 = time.time()
    n = kept = 0
    buf: list[tuple] = []

    def flush():
        if buf:
            con.executemany(
                """INSERT INTO ed(id, ekey, wkey, title, subtitle, authors, akeys,
                                  year, publisher, city, edition, volume, language, pages)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", buf)
            buf.clear()

    with _opener(dump)(dump, "rt", encoding="utf-8", errors="replace") as fh:
        try:
            for line in fh:
                n += 1
                if limit and n > limit:
                    break
                # Cheap prescreen before JSON: only old, dated editions survive.
                m = _PUBDATE_RE.search(line)
                if not m:
                    continue
                year = _date_year(m.group(1))
                if year is None or year > max_year:
                    continue
                parts = line.split("\t", 4)
                if len(parts) < 5 or parts[0] != "/type/edition":
                    continue
                try:
                    d = json.loads(parts[4])
                except Exception:
                    continue
                if not isinstance(d, dict):
                    continue
                title = str(d.get("title") or "").strip()
                if not title:
                    continue
                kept += 1
                ekey = str(d.get("key") or parts[1]).rsplit("/", 1)[-1]
                wkeys = d.get("works") or []
                wkey = ""
                if wkeys and isinstance(wkeys[0], dict):
                    wkey = str(wkeys[0].get("key") or "").rsplit("/", 1)[-1]
                akeys = []
                for a in d.get("authors") or []:
                    k = a.get("key") if isinstance(a, dict) else a
                    if isinstance(k, str):
                        tok = k.rsplit("/", 1)[-1]
                        if _AUTHOR_KEY_RE.match(tok):
                            akeys.append(tok)
                langs = []
                for lg in d.get("languages") or []:
                    k = lg.get("key") if isinstance(lg, dict) else lg
                    if isinstance(k, str):
                        langs.append(k.rsplit("/", 1)[-1])
                buf.append((
                    kept, ekey, wkey, title,
                    str(d.get("subtitle") or "").strip() or None,
                    None, ",".join(akeys) or None, year,
                    _clean_list(d.get("publishers")) or None,
                    _clean_list(d.get("publish_places")) or None,
                    str(d.get("edition_name") or "").strip() or None,
                    _edition_volume(d, title) or None,
                    "; ".join(langs) or None,
                    str(d.get("number_of_pages") or "").strip() or None,
                ))
                if len(buf) >= BATCH:
                    flush()
                if n % 5000000 == 0:
                    rate = n / max(time.time() - t0, 1)
                    print(f"  editions: {n/1e6:.0f}M lines, {kept/1e6:.2f}M kept, "
                          f"{rate:,.0f} lines/s", flush=True)
        except (EOFError, OSError) as exc:
            print(f"WARNING: editions stream ended abnormally after {n:,} lines: {exc}",
                  flush=True)
    flush()
    print(f"pass B: {kept:,} editions (<= {max_year}) from {n:,} lines "
          f"in {(time.time()-t0)/60:.1f} min", flush=True)
    return kept


# --- pass C: author keys (works db) + names -------------------------------------

def resolve_authors(con: sqlite3.Connection, works_db: Path) -> None:
    t0 = time.time()
    if works_db.exists():
        con.execute(f"ATTACH DATABASE 'file:{works_db.as_posix()}?mode=ro' AS w")
        n = con.execute(
            """UPDATE ed SET akeys = (
                   SELECT wk.authors FROM w.works wk WHERE wk.key = ed.wkey)
               WHERE (akeys IS NULL OR akeys = '') AND wkey != ''""").rowcount
        con.commit()  # DETACH refuses while the UPDATE's transaction is open
        con.execute("DETACH DATABASE w")
        print(f"pass C1: filled author keys for {n:,} editions from the works index",
              flush=True)
    else:
        print(f"pass C1 skipped: {works_db} not found (edition-level authors only)",
              flush=True)

    # Resolve names in batches; authors repeat heavily, so cache locally.
    names: dict[str, str] = {}
    updates: list[tuple] = []
    total = 0
    cur = con.execute("SELECT id, akeys FROM ed WHERE akeys IS NOT NULL AND akeys != ''")
    while True:
        rows = cur.fetchmany(BATCH)
        if not rows:
            break
        missing = sorted({k for _, ak in rows for k in ak.split(",")
                          if k and k not in names})
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            marks = ",".join("?" for _ in chunk)
            for key, name in con.execute(
                    f"SELECT key, name FROM authors WHERE key IN ({marks})", chunk):
                names[key] = name
            for k in chunk:
                names.setdefault(k, "")
        for rid, ak in rows:
            joined = "; ".join(n for n in (names.get(k, "") for k in ak.split(","))
                               if n)
            if joined:
                updates.append((joined, rid))
        if len(updates) >= BATCH:
            con.executemany("UPDATE ed SET authors = ? WHERE id = ?", updates)
            updates.clear()
        total += len(rows)
        if len(names) > 3000000:  # keep the local cache bounded
            names.clear()
    if updates:
        con.executemany("UPDATE ed SET authors = ? WHERE id = ?", updates)
    print(f"pass C2: author names resolved for {total:,} editions "
          f"in {(time.time()-t0)/60:.1f} min", flush=True)


# --- pass D: FTS + indexes -------------------------------------------------------

def build_fts(con: sqlite3.Connection) -> None:
    t0 = time.time()
    con.execute(
        """INSERT INTO ed_fts(rowid, title, authors, publisher, place)
           SELECT id,
                  title || coalesce(' ' || subtitle, ''),
                  coalesce(authors, ''),
                  coalesce(publisher, ''),
                  coalesce(city, '')
           FROM ed""")
    con.execute("CREATE INDEX idx_ed_year ON ed(year)")
    con.execute("CREATE INDEX idx_ed_ekey ON ed(ekey)")
    con.execute("ANALYZE")
    print(f"pass D: FTS + indexes in {(time.time()-t0)/60:.1f} min", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the consolidated Open Library search index.")
    parser.add_argument("--editions", default="", help="Editions dump path.")
    parser.add_argument("--authors", default="", help="Authors dump path.")
    parser.add_argument("--works-db", default=str(WORKS_DB),
                        help=f"Works index for author-key fallback (default {WORKS_DB}).")
    parser.add_argument("--db", default=str(DB_PATH), help=f"Output (default {DB_PATH}).")
    parser.add_argument("--max-year", type=int, default=1950,
                        help="Keep editions published up to this year (default 1950).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only read the first N edition lines (smoke test).")
    args = parser.parse_args()

    editions = Path(args.editions) if args.editions else _find("ol_dump_editions*")
    authors = Path(args.authors) if args.authors else _find("ol_dump_authors*")
    if not editions or not editions.exists():
        parser.error("editions dump not found (ol_dump_editions*.txt[.gz])")
    if not authors or not authors.exists():
        parser.error("authors dump not found (ol_dump_authors*.txt[.gz])")

    db = Path(args.db)
    tmp = db.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)

    print(f"building {db} from {editions.name} + {authors.name} "
          f"(editions <= {args.max_year})", flush=True)
    t0 = time.time()
    con = _open_db(tmp)
    load_authors(con, authors, args.limit or None)
    con.commit()
    load_editions(con, editions, args.max_year, args.limit or None)
    con.commit()
    resolve_authors(con, Path(args.works_db))
    con.commit()
    build_fts(con)
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()

    for attempt in range(30):
        try:
            os.replace(tmp, db)
            break
        except PermissionError:
            time.sleep(2)
    else:
        print(f"could not replace {db} (in use); finished index left at {tmp}",
              flush=True)
        return
    mins = (time.time() - t0) / 60
    print(f"done -> {db} ({db.stat().st_size/1e9:.1f} GB) in {mins:.1f} min",
          flush=True)


if __name__ == "__main__":
    main()
