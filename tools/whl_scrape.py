"""Scrape complete book metadata from the World Herb Library website.

The site is WordPress with the catalog as a custom post type whose REST API
is open, so no HTML scraping is needed: /wp-json/wp/v2/whl_catalog pages
through every PUBLISHED book (100 per request, ~46 pages) with the ACF
fields (subtitle, print length, language, edition, publisher city, ...)
inline and the taxonomy terms (publisher, authors, library categories)
embedded via ?_embed=wp:term. The description is the post body. Draft
entries are not exposed by the API and therefore have no metadata — exactly
as on the website.

Results are keyed by permalink slug (stable across catalogue re-exports) in
output/whl_scraped.json, written incrementally so an interrupted run keeps
what it fetched. The explorer merges these under its WHL catalog view.

Run standalone:
  python3 tools/whl_scrape.py
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libcommon as lib  # noqa: E402

API = "https://worldherblibrary.org/wp-json/wp/v2/whl_catalog"
USER_AGENT = "world-herb-library-tools/1.0"
TIMEOUT = 30.0
PER_PAGE = 100
SLEEP = 0.4  # politeness between page requests

SCRAPED_PATH = lib.OUTPUT_DIR / "whl_scraped.json"

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def _strip_html(text: str) -> str:
    """Rendered post HTML -> readable plain text (paragraphs kept)."""
    text = re.sub(r"</p>\s*<p[^>]*>", "\n\n", text or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def _terms(record: dict, taxonomy: str) -> list[str]:
    out = []
    for group in (record.get("_embedded") or {}).get("wp:term") or []:
        for t in group:
            if isinstance(t, dict) and t.get("taxonomy") == taxonomy and t.get("name"):
                out.append(html.unescape(str(t["name"])))
    return out


def _extract(record: dict) -> dict:
    acf = record.get("acf") or {}
    return {
        "slug": str(record.get("slug") or ""),
        "title": _strip_html((record.get("title") or {}).get("rendered", "")),
        "subtitle": str(acf.get("subtitle") or "").strip(),
        "description": _strip_html((record.get("content") or {}).get("rendered", "")),
        "publisher": "; ".join(_terms(record, "publisher")),
        "publisher_city": str(acf.get("publisher_city") or "").strip(),
        "pages": str(acf.get("print_length") or "").strip(),
        "language": str(acf.get("language") or "").strip(),
        "subject": "; ".join(_terms(record, "library-category")),
        "authors": "; ".join(_terms(record, "pub_author")),
        "year": str(acf.get("publication_date") or "").strip(),
        "edition": str(acf.get("edition") or "").strip(),
        "file": str(acf.get("publication_file") or "").strip(),
        "modified": str(record.get("modified") or ""),
    }


def _get_page(page: int) -> tuple[list, int]:
    url = f"{API}?per_page={PER_PAGE}&page={page}&_embed=wp:term"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        total_pages = int(resp.headers.get("X-WP-TotalPages") or 0)
        return json.loads(resp.read().decode("utf-8", errors="replace")), total_pages


def scrape_all(progress=None) -> dict:
    """Fetch metadata for every published book; returns the scraped dict.

    progress, if given, is a dict updated in place (page, pages, records) —
    the web app polls it from another thread.
    """
    scraped = lib.load_json(SCRAPED_PATH, {})
    meta = scraped.get("_meta") or {}
    books = {k: v for k, v in scraped.items() if k != "_meta"}
    page, total_pages = 1, 1
    while page <= total_pages:
        records, total_pages = _get_page(page)
        for r in records:
            e = _extract(r)
            if e["slug"]:
                books[e["slug"]] = e
        if progress is not None:
            progress.update({"page": page, "pages": total_pages,
                             "records": len(books)})
        # Save as we go so an interrupted run keeps its work.
        meta.update({"scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "pages_done": page, "pages_total": total_pages})
        out = dict(books)
        out["_meta"] = meta
        lib.save_json(SCRAPED_PATH, out)
        page += 1
        if page <= total_pages:
            time.sleep(SLEEP)
    return books


def load_scraped() -> dict:
    """Scraped books keyed by permalink slug (without the _meta entry)."""
    data = lib.load_json(SCRAPED_PATH, {})
    return {k: v for k, v in data.items() if k != "_meta"}


def main() -> None:
    print(f"scraping the WHL catalog into {SCRAPED_PATH} ...", flush=True)
    progress: dict = {}
    t0 = time.time()
    books = scrape_all(progress)
    print(f"done: {len(books):,} published books "
          f"({progress.get('pages', '?')} pages) in {(time.time()-t0)/60:.1f} min",
          flush=True)


if __name__ == "__main__":
    main()
