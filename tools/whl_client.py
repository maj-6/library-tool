"""Search worldherblibrary.org and report the closest matching book.

Uses the site's public search API (https://api.worldherblibrary.org/search),
the same endpoint the website's own search box calls. Given a local book
title (and optional author), it returns the closest catalogue match and
whether that book appears to be available on World Herb Library.

Usable as a library (find_book / search) or a CLI that prints JSON:

    python3 tools/whl_client.py --title "An Introduction to Botany"
    python3 tools/whl_client.py --title "Species Plantarum" --author "Linnaeus"
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher

API_URL = "https://api.worldherblibrary.org/search"
USER_AGENT = "Mozilla/5.0 (world-herb-library tools)"

# Composite accuracy is computed from a few fixed-length, case-insensitive
# prefixes so that appended subtitles and OCR noise later in a string do not
# derail the match.
TITLE_PREFIX = 16   # compare the first 16 chars of the title
AUTHOR_PREFIX = 8   # compare the first 8 chars of the author
W_TITLE = 0.5       # component weights (renormalized over present fields)
W_AUTHOR = 0.3
W_DATE = 0.2
# Minimum composite accuracy for a match to count as "available".
MATCH_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace for comparison."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    """Case-insensitive full-string similarity (0..1)."""
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _prefix(text: str, n: int) -> str:
    return _normalize(text)[:n]


def similarity_prefix(a: str, b: str, n: int) -> float:
    """Case-insensitive similarity over the first n normalized characters."""
    return SequenceMatcher(None, _prefix(a, n), _prefix(b, n)).ratio()


def _year(value) -> str | None:
    """Extract a 4-digit year from a date-ish value, if present."""
    m = re.search(r"(1[0-9]{3}|20[0-9]{2})", str(value or ""))
    return m.group(1) if m else None


# Tails that are credentials/suffixes, not given names, so "Last, <tail>" must
# not be flipped (e.g. "Blair, M. D." or "Smith, Jr.").
_AUTHOR_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "md", "phd", "do", "esq",
    "ma", "ba", "bsc", "msc", "praeses", "respondent",
}


def flip_author(name: str) -> str:
    """Convert 'Lastname, Firstname' to 'Firstname Lastname'.

    Only flips on a single comma whose trailing part is a name rather than a
    credential/suffix; all other forms are left untouched.
    """
    s = (name or "").strip()
    if s.count(",") != 1:
        return s
    last, first = (p.strip() for p in s.split(",", 1))
    if not last or not first:
        return s
    if re.sub(r"[^a-z]", "", first.lower()) in _AUTHOR_SUFFIXES:
        return s
    return f"{first} {last}"


def accuracy(query: dict, match: dict) -> tuple[float, dict]:
    """Composite match accuracy from title, author, and publishing year.

    Title uses the first %d chars, author the first %d chars (both
    case-insensitive); year must match exactly. Only components present on
    both sides are scored, and the remaining weights are renormalized so a
    missing author or date neither helps nor hurts. Returns (accuracy,
    breakdown).
    """ % (TITLE_PREFIX, AUTHOR_PREFIX)
    title_score = similarity_prefix(query.get("title", ""), match.get("whl_title", ""), TITLE_PREFIX)
    parts: dict = {"title": title_score, "author": None, "date": None}
    components = [(title_score, W_TITLE)]

    if _normalize(query.get("author")) and _normalize(match.get("author")):
        # Reorder "Lastname, Firstname" forms so the first-8-char compare lines
        # up across the catalogue and WHL's "Firstname Lastname" style.
        author_score = similarity_prefix(
            flip_author(query["author"]), flip_author(match["author"]), AUTHOR_PREFIX
        )
        parts["author"] = author_score
        components.append((author_score, W_AUTHOR))

    qy, my = _year(query.get("date")), _year(match.get("pub_date"))
    if qy and my:
        parts["date"] = qy == my
        components.append((1.0 if qy == my else 0.0, W_DATE))

    total = sum(w for _, w in components) or 1.0
    return sum(s * w for s, w in components) / total, parts


def search(query: str, limit: int = 10, timeout: float = 15.0) -> dict:
    """Call the WHL search API and return the parsed JSON response."""
    params = urllib.parse.urlencode({"q": query, "limit": limit})
    req = urllib.request.Request(
        f"{API_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_match(result: dict, query: dict) -> dict:
    """Turn a raw WHL result into a scored match dict."""
    whl_title = (
        result.get("display_title")
        or result.get("book_name")
        or (result.get("book_filename") or "").replace(".pdf", "")
    )
    match = {
        "whl_title": whl_title,
        "author": result.get("author", ""),
        "pub_date": result.get("pub_date"),
        "wp_url": result.get("wp_url", ""),
        "book_filename": result.get("book_filename", ""),
        "score": result.get("score"),
    }
    acc, parts = accuracy(query, match)
    match["accuracy"] = round(acc, 3)
    match["title_score"] = round(parts["title"], 3)
    match["author_score"] = (
        round(parts["author"], 3) if parts["author"] is not None else None
    )
    match["date_match"] = parts["date"]
    return match


def find_book(
    title: str,
    author: str | None = None,
    date: str | None = None,
    limit: int = 10,
    threshold: float = MATCH_THRESHOLD,
) -> dict:
    """Find the closest WHL match for a local book.

    Matching is case-insensitive and uses the title, author, and publishing
    year together (see `accuracy`). Returns a JSON-serializable dict with an
    `available` flag:
      True  -> a book of sufficient composite accuracy exists on WHL
      False -> searched successfully but no close match
      None  -> the lookup itself failed (network/parse error)
    """
    out: dict = {
        "query_title": title,
        "query_author": author or "",
        "query_date": date or "",
        "available": None,
        "best_match": None,
        "alternatives": [],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not (title or author):
        out["available"] = False
        out["error"] = "empty query"
        return out

    query = {"title": title or "", "author": author or "", "date": date or ""}

    # Search by title and by author so OCR/format differences in one field can
    # be recovered via the other; rank everything by composite accuracy.
    queries = []
    if title:
        queries.append(f"[TI] {title}")
    if author:
        queries.append(f"[AU] {author}")
    if title:
        queries.append(title)

    matches: dict[str, dict] = {}
    try:
        for q in queries:
            data = search(q, limit=limit)
            for result in data.get("results", []):
                m = _build_match(result, query)
                key = m["book_filename"] or m["wp_url"] or m["whl_title"]
                # Keep the highest-accuracy instance of each book.
                if key not in matches or m["accuracy"] > matches[key]["accuracy"]:
                    matches[key] = m
            # A strong hit is enough; avoid extra calls.
            if matches and max(m["accuracy"] for m in matches.values()) >= 0.9:
                break
    except Exception as exc:  # network, timeout, JSON, etc.
        out["available"] = None
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    ranked = sorted(matches.values(), key=lambda m: m["accuracy"], reverse=True)
    if not ranked:
        out["available"] = False
        return out

    best = ranked[0]
    out["best_match"] = best
    out["alternatives"] = ranked[1:5]
    out["available"] = bool(best["accuracy"] >= threshold and best["wp_url"])
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the closest World Herb Library match for a book."
    )
    parser.add_argument("--title", default="", help="Book title to search for.")
    parser.add_argument("--author", default="", help="Optional author to refine.")
    parser.add_argument("--date", default="", help="Optional publishing year to refine.")
    parser.add_argument("--limit", type=int, default=10, help="Max results per query.")
    args = parser.parse_args()

    result = find_book(
        args.title, args.author or None, args.date or None, limit=args.limit
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
