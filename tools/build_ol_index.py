"""Convert the Open Library works dump into a local SQLite search index.

The raw dump (ol_dump_works_*.txt.gz, ~4 GB gzipped) is far too large to scan
per keystroke; this builds output/ol_works.db with an FTS5 full-text index
over work titles/subtitles so the explorer's constrained search and
autocomplete answer instantly and offline.

Works records carry only title, subtitle, author KEYS, and (rarely) a first
publish date — author names and publisher/city/edition/volume data live in
other dumps, so the web app resolves those through the Open Library API on
demand (cached). See tools/whl_explorer/server.py.

Run with python3 (a full build takes a while; use --limit to smoke-test):
  python3 tools/build_ol_index.py                    # full build (background it)
  python3 tools/build_ol_index.py --limit 1500000    # quick partial index
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

DB_PATH = lib.OUTPUT_DIR / "ol_works.db"

_YEAR_RE = re.compile(r"(1[0-9]{3}|20[0-9]{2})")
_AUTHOR_KEY_RE = re.compile(r"^OL\d+A$")

BATCH = 20000


def _find_dump() -> Path | None:
    """Pick a readable dump, preferring an already-decompressed .txt.

    A .txt mid-extraction is often perfectly readable on Windows (extractors
    rarely take exclusive locks), so a lock probe is not enough: also verify
    the file is not still growing, otherwise a truncated index would be built
    from however much happened to be extracted.
    """
    candidates = [Path(p) for p in
                  sorted(glob.glob(str(lib.ROOT / "ol_dump_works*.txt"))) +
                  sorted(glob.glob(str(lib.ROOT / "ol_dump_works*.txt.gz")))]
    for p in candidates:
        try:
            with open(p, "rb") as fh:
                fh.read(16)
        except OSError:
            continue
        size1 = p.stat().st_size
        time.sleep(2.0)
        if p.stat().st_size != size1:
            print(f"skipping {p.name}: still growing (extraction in progress?)",
                  flush=True)
            continue
        return p
    return None


def _year_of(d: dict) -> int | None:
    m = _YEAR_RE.search(str(d.get("first_publish_date", "") or ""))
    return int(m.group(1)) if m else None


def _open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache
    con.execute("""
        CREATE TABLE works(
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL,
            title TEXT NOT NULL,
            subtitle TEXT,
            authors TEXT,
            year INTEGER
        )""")
    # Contentless FTS keeps the index compact; rowids point into works.
    # remove_diacritics=2 lets 'vegetaux' match 'végétaux'.
    con.execute("""
        CREATE VIRTUAL TABLE works_fts USING fts5(
            text, content='', tokenize='unicode61 remove_diacritics 2')""")
    con.execute("CREATE TABLE work_authors(author TEXT NOT NULL, work INTEGER NOT NULL)")
    return con


def build(dump: Path, db: Path, limit: int | None) -> None:
    tmp = db.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)
    con = _open_db(tmp)

    opener = gzip.open if dump.suffix == ".gz" else open
    t0 = time.time()
    n = kept = 0
    works_buf: list[tuple] = []
    fts_buf: list[tuple] = []
    auth_buf: list[tuple] = []

    def flush():
        if works_buf:
            con.executemany(
                "INSERT INTO works(id, key, title, subtitle, authors, year) VALUES(?,?,?,?,?,?)",
                works_buf)
            con.executemany("INSERT INTO works_fts(rowid, text) VALUES(?,?)", fts_buf)
            if auth_buf:
                con.executemany("INSERT INTO work_authors(author, work) VALUES(?,?)", auth_buf)
            works_buf.clear()
            fts_buf.clear()
            auth_buf.clear()

    # A truncated .gz raises only at end-of-stream, after (almost) everything
    # readable was already parsed — salvage that work instead of discarding it.
    stream_error: Exception | None = None
    with opener(dump, "rt", encoding="utf-8", errors="replace") as fh:
      try:
        for line in fh:
            n += 1
            if limit and n > limit:
                break
            parts = line.split("\t", 4)
            if len(parts) < 5 or parts[0] != "/type/work":
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
            key = str(d.get("key") or parts[1]).rsplit("/", 1)[-1]
            subtitle = str(d.get("subtitle") or "").strip() or None
            akeys = []
            # The dump mixes shapes: {"author": {"key": ...}}, {"author": "..."},
            # {"key": "..."} and bare strings all occur.
            for a in d.get("authors") or []:
                k = None
                if isinstance(a, dict):
                    aa = a.get("author")
                    if isinstance(aa, dict):
                        k = aa.get("key")
                    elif isinstance(aa, str):
                        k = aa
                    elif isinstance(a.get("key"), str):
                        k = a["key"]
                elif isinstance(a, str):
                    k = a
                if isinstance(k, str):
                    tok = k.rsplit("/", 1)[-1]
                    if _AUTHOR_KEY_RE.match(tok):
                        akeys.append(tok)
            works_buf.append(
                (kept, key, title, subtitle, ",".join(akeys) or None, _year_of(d)))
            fts_buf.append((kept, title + (" " + subtitle if subtitle else "")))
            for ak in akeys:
                auth_buf.append((ak, kept))
            if len(works_buf) >= BATCH:
                flush()
            if n % 1000000 == 0:
                rate = n / max(time.time() - t0, 1)
                print(f"  {n/1e6:.0f}M lines, {kept/1e6:.2f}M works, {rate:,.0f} lines/s",
                      flush=True)
      except (EOFError, OSError) as exc:  # truncated/corrupt stream
        stream_error = exc
        print(f"WARNING: dump stream ended abnormally after {n:,} lines: {exc}",
              flush=True)
    flush()

    print("indexing authors ...", flush=True)
    con.execute("CREATE INDEX idx_work_authors ON work_authors(author)")
    con.execute("CREATE INDEX idx_works_key ON works(key)")
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")  # normal, safe mode for readers
    con.close()

    if stream_error:
        # The read data is intact, but the dump was incomplete: keep the
        # salvaged index beside the existing one instead of silently
        # replacing a complete index with a partial one.
        print(f"salvaged {kept:,} works to {tmp} — NOT replacing {db} "
              f"automatically (the dump appears truncated). Verify the dump, "
              f"or rename the .tmp over the .db yourself.", flush=True)
        return

    # On Windows the replace fails while a reader holds the old file open;
    # retry briefly rather than losing the finished build.
    for attempt in range(30):
        try:
            os.replace(tmp, db)
            break
        except PermissionError:
            time.sleep(2)
    else:
        print(f"could not replace {db} (still in use); the finished index is at {tmp}",
              flush=True)
        return
    mins = (time.time() - t0) / 60
    size = db.stat().st_size / 1e9
    print(f"done: {kept:,} works -> {db} ({size:.1f} GB) in {mins:.1f} min", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Open Library works index.")
    parser.add_argument("--dump", default="", help="Path to ol_dump_works*.txt(.gz).")
    parser.add_argument("--db", default=str(DB_PATH), help=f"Output DB (default {DB_PATH}).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only read the first N dump lines (smoke test).")
    args = parser.parse_args()

    dump = Path(args.dump) if args.dump else _find_dump()
    if not dump or not dump.exists():
        parser.error("dump file not found (expected ol_dump_works*.txt.gz in the repo root)")
    print(f"building {args.db} from {dump.name}" +
          (f" (first {args.limit:,} lines)" if args.limit else ""), flush=True)
    build(dump, Path(args.db), args.limit or None)


if __name__ == "__main__":
    main()
