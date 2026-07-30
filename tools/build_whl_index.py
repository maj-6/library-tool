"""Build the WHL catalogue index that ships inside the Android APK.

The phone needs to answer "is this book already in the World Herb Library?"
without downloading the catalogue.  This builder turns the tracked
``whl_catalog.csv`` snapshot into the same title/author posting lists used by
``tools/catalog_checks.py`` and carries the desktop-owned matching thresholds
with the data.

The output is plain JSON on purpose.  The APK's zip already deflates JSON, and
Android's asset packaging may strip a ``.gz`` suffix while inflating the file,
making the source-tree name differ from the name available on device.

Run:
  python3 tools/build_whl_index.py
  python3 tools/build_whl_index.py \
    --out android/BookCapture/app/src/main/assets
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog_checks as checks  # noqa: E402
import libcommon as lib  # noqa: E402
import whl_client as whl  # noqa: E402

# Candidate bucketing and acceptance-prefix scoring deliberately use different
# lengths, exactly as they do in catalog_checks._candidates/title_author_match.
BUCKET_CHARS = 12
INDEX_VERSION = 1
DEFAULT_CSV = lib.ROOT / "whl_catalog.csv"


def whl_rows(path: Path) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"{path} missing")
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        return list(csv.DictReader(fh))


def build_index(rows: list[dict], source_sha256: str) -> dict:
    """Build the compact records and candidate posting lists.

    Field names mirror ``catalog_checks.load_whl_catalog``:

    - an empty/unnormalizable title is excluded;
    - year is the first supported four-digit year;
    - status is lowercase;
    - the original CSV row index is retained for diagnostics.
    """
    entries: list[dict] = []
    by_author: dict[str, list[int]] = defaultdict(list)
    by_title: dict[str, list[int]] = defaultdict(list)

    for source_index, row in enumerate(rows):
        title = str(row.get("Title") or "")
        norm_title = whl._normalize(title)
        if not norm_title:
            continue
        author = str(row.get("Authors") or "")
        position = len(entries)
        entries.append(
            {
                "i": source_index,
                "t": title,
                "a": author,
                "y": whl._year(row.get("Year Published")) or "",
                "s": str(row.get("Status") or "").strip().lower(),
                "u": str(row.get("Permalink") or "").strip(),
            }
        )
        by_title[norm_title[:BUCKET_CHARS]].append(position)
        for token in checks.author_tokens(author):
            by_author[token].append(position)

    return {
        "version": INDEX_VERSION,
        "source": "whl_catalog.csv",
        "source_sha256": source_sha256,
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
        "by_author": {key: value for key, value in sorted(by_author.items())},
        "by_title": {key: value for key, value in sorted(by_title.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        default=str(DEFAULT_CSV),
        help="WHL catalogue CSV (default: the tracked repository snapshot)",
    )
    parser.add_argument(
        "--out",
        default=str(lib.OUTPUT_DIR),
        help="directory to write whl_index.json into",
    )
    args = parser.parse_args()

    source = Path(args.csv)
    source_bytes = source.read_bytes() if source.is_file() else b""
    rows = whl_rows(source)
    index = build_index(rows, hashlib.sha256(source_bytes).hexdigest())
    payload = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    target = out / "whl_index.json"
    target.write_bytes(payload)
    print(
        f"{target}  {len(payload) / 1e6:.2f} MB  "
        f"{len(index['entries'])} entries, "
        f"{len(index['by_title'])} title buckets, "
        f"{len(index['by_author'])} author tokens"
    )


if __name__ == "__main__":
    main()
