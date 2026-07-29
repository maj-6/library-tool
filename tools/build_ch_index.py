"""Build the CH match index that ships inside the Android APK.

The phone answers "is this book already on CH's list?" offline, right after a
scan, so it needs both the CH records and the posting lists the desktop matcher
uses to generate candidates. Everything here is derived from the SAME functions
the desktop uses (tools/catalog_checks.py, tools/whl_client.py) so the two sides
cannot disagree about normalisation.

Stdlib only: the packaged sidecar installs tools/requirements.txt and nothing
else, and this module is imported by the server to serve the same index over
LAN.

Outputs
  <out>/ch_index.json[.gz]   the index the phone bundles
  <out>/ch_match_fixtures.json  golden ratios for the Kotlin conformance test

Run:
  python3 tools/build_ch_index.py
  python3 tools/build_ch_index.py --out android/BookCapture/app/src/main/assets
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_checks as checks  # noqa: E402
import libcommon as lib  # noqa: E402
import whl_client as whl  # noqa: E402

# The two prefix lengths are NOT the same and must not be conflated:
#   BUCKET_CHARS — how candidates are bucketed (catalog_checks._candidates)
#   whl.TITLE_PREFIX — how much of the title the prefix similarity compares
BUCKET_CHARS = 12

INDEX_VERSION = 1


def ch_rows() -> list[dict]:
    rows = lib.load_json(lib.CH_LIBRARY_JSON_PATH, None)
    if not isinstance(rows, list):
        raise SystemExit(
            f"{lib.CH_LIBRARY_JSON_PATH} missing or not a list — "
            "run tools/convert_xlsx.py first"
        )
    return rows


def display_title(row: dict) -> str:
    """CH stores the title underscore-separated; every consumer wants spaces."""
    return str(row.get("publication") or "").replace("_", " ").strip()


def merge_fields(row: dict, title: str, author: str) -> dict:
    """CH's row, keyed by the field names the phone's records use.

    Only fields an approved match should be able to contribute are included —
    CH's acquisition DATE, for instance, is about CH's purchase and says nothing
    about the book, so it is deliberately absent.

    Empty values are dropped rather than emitted as "": the merge rule is that a
    blank never overwrites, and shipping thousands of empty strings would bloat
    the index for no effect.
    """
    keys = [
        str(row.get("key") or ""),
        str(row.get("key_2") or ""),
        str(row.get("key_3") or ""),
    ]
    fields = {
        "title": title,
        "author": author,
        "year": str(row.get("year_of_publication") or ""),
        "edition": str(row.get("edition") or ""),
        "publisher": str(row.get("publisher") or ""),
        "city": str(row.get("city_published") or ""),
        "pages": str(row.get("page_reference") or ""),
        "condition": str(row.get("condition") or ""),
        "illustrations": str(row.get("illustrations") or ""),
        "price": str(row.get("price") or ""),
        "notes": str(row.get("notes") or ""),
        "categories": ", ".join(k for k in keys if k.strip()),
    }
    return {k: v.strip() for k, v in fields.items() if v and v.strip()}


def build_index(rows: list[dict]) -> dict:
    entries: list[dict] = []
    by_author: dict[str, list[int]] = defaultdict(list)
    by_title: dict[str, list[int]] = defaultdict(list)

    for idx, row in enumerate(rows):
        title = display_title(row)
        author = str(row.get("authors") or "").strip()
        if not title and not author:
            continue

        position = len(entries)
        entries.append({
            "i": idx,                                   # index into ch_library.json
            "t": title,
            "a": author,
            "y": row.get("year_of_publication") or "",
            "p": str(row.get("publisher") or ""),
            "e": str(row.get("edition") or ""),
            # Precomputed so the phone never has to reproduce _normalize on
            # 5k rows at query time — only on the one query string.
            "nt": whl._normalize(title),
            "na": whl._normalize(whl.flip_author(author)),
            # Everything an approved match can contribute, already keyed by the
            # APP's field names. Mapping CH's column names lives here rather
            # than in Kotlin so the two never disagree about what "pages" is.
            "f": merge_fields(row, title, author),
        })

        for token in checks.author_tokens(author):
            by_author[token].append(position)
        norm = whl._normalize(title)
        if norm:
            by_title[norm[:BUCKET_CHARS]].append(position)

    return {
        "version": INDEX_VERSION,
        "bucket_chars": BUCKET_CHARS,
        "title_prefix": whl.TITLE_PREFIX,
        "thresholds": {
            "prefix_min": checks.TITLE_PREFIX_MIN,
            "full_min": checks.TITLE_FULL_MIN,
            "full_missing": checks.TITLE_FULL_MISSING,
            "full_strict": checks.TITLE_FULL_STRICT,
        },
        "author_stop": sorted(checks._AUTHOR_STOP),
        "entries": entries,
        "by_author": {k: v for k, v in sorted(by_author.items())},
        "by_title": {k: v for k, v in sorted(by_title.items())},
    }


# --- conformance fixtures ---------------------------------------------------
# The Kotlin port has to reproduce difflib.SequenceMatcher.ratio() exactly,
# because the acceptance thresholds were tuned against it. difflib's autojunk
# heuristic only engages for sequences of 200+ elements, which no title reaches,
# so the algorithm is deterministic and portable — but "should port cleanly" is
# not evidence, so every pair below is checked by a Kotlin unit test.

def fixture_pairs(rows: list[dict], limit: int = 400) -> list[tuple[str, str]]:
    """Real CH titles paired with realistic scan-side variants."""
    titles = [display_title(r) for r in rows if display_title(r)]
    pairs: list[tuple[str, str]] = []

    def add(a: str, b: str) -> None:
        if a and b:
            pairs.append((a, b))

    # Adjacent real titles: the hard negatives, where two different books share
    # a prefix. These are what the thresholds exist to separate.
    for i in range(0, min(len(titles), limit) - 1, 2):
        add(titles[i], titles[i + 1])

    # Realistic distortions of a real title: OCR/dictation damage, article
    # dropped, subtitle present on one side only, case and punctuation drift.
    for title in titles[:limit:4]:
        add(title, title.lower())
        add(title, title.replace(" ", "  "))
        if title.lower().startswith("the "):
            add(title, title[4:])
        else:
            add(title, f"The {title}")
        if len(title) > 12:
            add(title, title[: len(title) // 2])
            add(title, title[:8] + "X" + title[9:])
        add(title, f"{title} A Treatise on the Materia Medica")

    # Degenerate inputs, which is where a naive port diverges.
    add("a", "a")
    add("a", "b")
    add("", "x")
    add("x", "")
    add("aaaa", "aaaaaaaa")
    add("ab", "ba")
    return pairs


def build_fixtures(rows: list[dict]) -> dict:
    cases = []
    for a, b in fixture_pairs(rows):
        cases.append({
            "a": a,
            "b": b,
            "normalized_a": whl._normalize(a),
            "normalized_b": whl._normalize(b),
            "ratio": whl.similarity(a, b),
            "prefix_ratio": whl.similarity_prefix(a, b, whl.TITLE_PREFIX),
        })

    author_cases = []
    seen: set[str] = set()
    for row in rows:
        author = str(row.get("authors") or "").strip()
        if not author or author in seen:
            continue
        seen.add(author)
        author_cases.append({
            "author": author,
            "flipped": whl.flip_author(author),
            "tokens": sorted(checks.author_tokens(author)),
            "last_token": checks.last_token(author),
        })
        if len(author_cases) >= 400:
            break

    match_cases = []
    for row in rows[:300]:
        title, author = display_title(row), str(row.get("authors") or "")
        for cand_title, cand_author in ((title, author), (title, ""), (f"The {title}", author)):
            match_cases.append({
                "title": title, "author": author,
                "cand_title": cand_title, "cand_author": cand_author,
                "match": checks.title_author_match(title, author, cand_title, cand_author),
            })

    return {
        "version": INDEX_VERSION,
        # Carried so the Kotlin conformance test asserts against the tuning the
        # fixtures were actually generated with, instead of a copy that can
        # drift once someone retunes a threshold here.
        "bucket_chars": BUCKET_CHARS,
        "title_prefix": whl.TITLE_PREFIX,
        "thresholds": {
            "prefix_min": checks.TITLE_PREFIX_MIN,
            "full_min": checks.TITLE_FULL_MIN,
            "full_missing": checks.TITLE_FULL_MISSING,
            "full_strict": checks.TITLE_FULL_STRICT,
        },
        "author_stop": sorted(checks._AUTHOR_STOP),
        "similarity": cases,
        "authors": author_cases,
        "matches": match_cases,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(lib.OUTPUT_DIR),
                    help="directory to write ch_index.json(.gz) into")
    ap.add_argument("--fixtures", default="",
                    help="also write ch_match_fixtures.json here")
    ap.add_argument("--no-gzip", action="store_true",
                    help="write plain JSON (default also writes .gz for the APK)")
    args = ap.parse_args()

    rows = ch_rows()
    index = build_index(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    plain = out / "ch_index.json"
    plain.write_bytes(payload)
    print(f"{plain}  {len(payload) / 1e6:.2f} MB  "
          f"{len(index['entries'])} entries, "
          f"{len(index['by_title'])} title buckets, "
          f"{len(index['by_author'])} author tokens")

    if not args.no_gzip:
        gz = out / "ch_index.json.gz"
        # mtime=0 so a rebuild with unchanged data produces an identical file
        # and does not churn the APK.
        with gzip.GzipFile(filename="", mode="wb", fileobj=gz.open("wb"), mtime=0) as fh:
            fh.write(payload)
        print(f"{gz}  {gz.stat().st_size / 1e6:.2f} MB gzipped")

    if args.fixtures:
        fixtures = build_fixtures(rows)
        target = Path(args.fixtures)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(fixtures, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{target}  {len(fixtures['similarity'])} similarity, "
              f"{len(fixtures['authors'])} author, {len(fixtures['matches'])} match cases")


if __name__ == "__main__":
    main()
