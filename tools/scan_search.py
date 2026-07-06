"""Search the Internet Archive and HathiTrust for existing scans of a book.

Internet Archive is searched directly through its public advancedsearch API.

HathiTrust's catalog search is closed to programs (robots.txt disallows
/Search and the server enforces it with 403s), so it is queried through its
official Bib API instead: the book's OCLC numbers are first discovered via the
Open Library search API (title/author -> editions -> OCLC), then looked up in
one Bib API call. Every result also carries a catalog search URL a human can
open in the browser.

Usable as a library (search_scans) or a CLI that prints JSON:

    python3 tools/scan_search.py --title "American Medicinal Plants" --author "Millspaugh"
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import catalog_checks as checks
import whl_client as whl

IA_SEARCH_API = "https://archive.org/advancedsearch.php"
IA_SEARCH_PAGE = "https://archive.org/search"
OL_SEARCH_API = "https://openlibrary.org/search.json"
HT_BIB_API = "https://catalog.hathitrust.org/api/volumes/brief/json/"
HT_SEARCH_PAGE = "https://catalog.hathitrust.org/Search/Home"

USER_AGENT = "world-herb-library-tools/1.0"
TIMEOUT = 20.0
# A result must reach this composite accuracy (see whl_client.accuracy) to
# count as the same book.
MATCH_THRESHOLD = whl.MATCH_THRESHOLD
MAX_MATCHES = 5     # matches reported per source
MAX_OCLCS = 8       # identifiers per HathiTrust Bib API call


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _title_words(title: str, n: int = 8) -> str:
    """Query terms for the IA title search.

    Plain normalization would glue possessives into tokens IA does not index
    ("Culpeper's" -> "culpepers" matches nothing); drop the possessive 's and
    split any remaining apostrophes instead.
    """
    t = re.sub(r"['‘’]s\b", " ", title or "", flags=re.IGNORECASE)
    t = re.sub(r"['‘’]", " ", t)
    return " ".join(whl._normalize(t).split()[:n])


def _surname(author: str | None) -> str:
    """Surname-ish query tokens of an author.

    Catalogue authors are frequently 'Lastname, Initial(s)' while IA / Open
    Library carry full names; querying with surname tokens only keeps
    'Millspaugh, C.' matching 'Charles Frederick Millspaugh'.
    """
    return " ".join(sorted(checks.author_tokens(author or "")))


# Component weights, mirroring whl_client's composite. The author component
# scores on surname-token overlap (not a prefix ratio): scan sources carry
# full names, catalogues carry 'Lastname, Initial(s)', and token overlap is
# the comparison that survives both.
_W_TITLE, _W_AUTHOR, _W_DATE = whl.W_TITLE, whl.W_AUTHOR, whl.W_DATE


def _score(query: dict, title: str, author: str, year) -> tuple[float, float]:
    """(accuracy, corroboration) of a candidate against the queried book.

    accuracy renormalizes over the components present on both sides (a
    missing author/date neither helps nor hurts) and gates availability;
    corroboration is the raw weighted sum (absent components count 0), so a
    title+author+year hit outranks an equal-accuracy title-only hit.
    """
    components = [(whl.similarity_prefix(query["title"], title or "", whl.TITLE_PREFIX), _W_TITLE)]
    ours, theirs = checks.author_tokens(query.get("author", "")), checks.author_tokens(author or "")
    if ours and theirs:
        components.append((1.0 if ours & theirs else 0.0, _W_AUTHOR))
    qy, cy = whl._year(query.get("date")), whl._year(year)
    if qy and cy:
        components.append((1.0 if qy == cy else 0.0, _W_DATE))
    raw = sum(s * w for s, w in components)
    total = sum(w for _, w in components) or 1.0
    return round(raw / total, 3), round(raw, 3)


# --- Internet Archive --------------------------------------------------------

def search_internet_archive(
    title: str, author: str | None = None, year: str | None = None, limit: int = 10
) -> dict:
    """Search the IA text collection and rank results by composite accuracy."""
    query = {"title": title or "", "author": author or "", "date": year or ""}
    words = _title_words(title)
    out: dict = {
        "available": None,
        "best_match": None,
        "matches": [],
        "search_url": f"{IA_SEARCH_PAGE}?"
        + urllib.parse.urlencode({"query": f"{title} {author or ''}".strip()}),
    }
    if not words:
        out["available"] = False
        out["error"] = "empty query"
        return out

    def run(q: str) -> list[dict]:
        url = IA_SEARCH_API + "?" + urllib.parse.urlencode(
            {
                "q": q,
                "fl[]": ["identifier", "title", "creator", "year"],
                "rows": limit,
                "output": "json",
            },
            doseq=True,
        )
        return _get_json(url).get("response", {}).get("docs", [])

    # Query ladder, most precise first: a quoted title phrase pins IA's loose
    # relevance ordering, and the creator filter uses surname tokens only
    # ('Millspaugh, C.' would match nothing as creator:(millspaugh c)). Later,
    # looser queries only run while no strong hit has been found.
    surname = _surname(author)
    queries = [f'title:("{words}") AND mediatype:texts']
    if surname:
        queries.insert(0, f'title:("{words}") AND mediatype:texts AND creator:({surname})')
        queries.append(f"title:({words}) AND mediatype:texts AND creator:({surname})")
    queries.append(f"title:({words}) AND mediatype:texts")

    by_id: dict[str, dict] = {}
    try:
        for q in queries:
            for d in run(q):
                ident = str(d.get("identifier", "") or "")
                if not ident or ident in by_id:
                    continue
                creator = d.get("creator")
                if isinstance(creator, list):
                    creator = "; ".join(str(c) for c in creator)
                m = {
                    "identifier": ident,
                    "title": str(d.get("title", "") or ""),
                    "author": str(creator or ""),
                    "year": str(d.get("year", "") or ""),
                    "url": f"https://archive.org/details/{ident}",
                }
                m["accuracy"], m["_rank"] = _score(query, m["title"], m["author"], m["year"])
                by_id[ident] = m
            # A well-corroborated hit is enough; skip the looser queries.
            if by_id and max(m["_rank"] for m in by_id.values()) >= 0.8:
                break
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    matches = sorted(by_id.values(), key=lambda m: (m["_rank"], m["accuracy"]), reverse=True)
    for m in matches:
        del m["_rank"]
    out["matches"] = matches[:MAX_MATCHES]
    out["best_match"] = matches[0] if matches else None
    out["available"] = bool(matches and matches[0]["accuracy"] >= MATCH_THRESHOLD)
    return out


# --- HathiTrust (via Open Library identifiers) --------------------------------

def _openlibrary_oclcs(title: str, author: str | None, year: str | None) -> list[dict]:
    """Open Library docs that plausibly are this book and carry OCLC numbers."""
    params = {
        "title": title,
        "fields": "title,author_name,first_publish_year,oclc",
        "limit": 10,
    }
    # Surname tokens only: 'Millspaugh, C.' as a full author string depresses
    # Open Library's matching the same way it does IA's creator filter.
    surname = _surname(author)
    if surname:
        params["author"] = surname
    docs = _get_json(OL_SEARCH_API + "?" + urllib.parse.urlencode(params)).get("docs", [])
    if not docs and "author" in params:
        # Author formats differ wildly across catalogues; retry title-only.
        del params["author"]
        docs = _get_json(OL_SEARCH_API + "?" + urllib.parse.urlencode(params)).get("docs", [])

    query = {"title": title or "", "author": author or "", "date": year or ""}
    candidates = []
    for d in docs:
        authors = d.get("author_name") or []
        acc, rank = _score(
            query, str(d.get("title", "") or ""), "; ".join(authors),
            d.get("first_publish_year"),
        )
        if acc >= MATCH_THRESHOLD and d.get("oclc"):
            candidates.append({"rank": rank, "oclcs": [str(o) for o in d["oclc"]]})
    candidates.sort(key=lambda c: c["rank"], reverse=True)
    return candidates


def search_hathitrust(
    title: str, author: str | None = None, year: str | None = None
) -> dict:
    """Look the book up in HathiTrust via its Bib API.

    available: True  -> HathiTrust holds scans for a matching OCLC number
               False -> matching OCLC numbers were found but HT holds nothing
               None  -> could not determine (no identifiers, or a lookup error)
    full_view is True when at least one held item is 'Full view'.
    """
    out: dict = {
        "available": None,
        "full_view": False,
        "best_match": None,
        "matches": [],
        "search_url": HT_SEARCH_PAGE + "?"
        + urllib.parse.urlencode({"lookfor": title, "type": "title"}),
    }
    if not whl._normalize(title):
        out["available"] = False
        out["error"] = "empty query"
        return out

    try:
        candidates = _openlibrary_oclcs(title, author, year)
    except Exception as exc:
        out["error"] = f"Open Library lookup failed: {type(exc).__name__}: {exc}"
        return out
    oclcs: list[str] = []
    for c in candidates:
        for o in c["oclcs"]:
            if o not in oclcs:
                oclcs.append(o)
    if not oclcs:
        out["note"] = (
            "no OCLC identifier found via Open Library; "
            "use the search link to check by hand"
        )
        return out
    oclcs = oclcs[:MAX_OCLCS]
    out["oclcs_tried"] = oclcs

    try:
        data = _get_json(HT_BIB_API + "|".join(f"oclc:{o}" for o in oclcs))
    except Exception as exc:
        out["error"] = f"Bib API lookup failed: {type(exc).__name__}: {exc}"
        return out

    query = {"title": title or "", "author": author or "", "date": year or ""}
    matches = []
    for result in data.values():
        records = result.get("records") or {}
        items = result.get("items") or []
        by_record: dict[str, list] = {}
        for it in items:
            by_record.setdefault(str(it.get("fromRecord", "")), []).append(it)
        for rec_id, rec in records.items():
            rec_items = by_record.get(str(rec_id), items if len(records) == 1 else [])
            rec_title = (rec.get("titles") or [""])[0]
            years = rec.get("publishDates") or []
            m = {
                "title": rec_title,
                "year": str(years[0]) if years else "",
                "record_url": rec.get("recordURL", ""),
                "items": [
                    {
                        "url": it.get("itemURL", ""),
                        "rights": it.get("usRightsString", ""),
                        "volume": it.get("enumcron") or "",
                    }
                    for it in rec_items
                ],
            }
            m["full_view"] = any(i["rights"].lower() == "full view" for i in m["items"])
            # The OCLC match already ties the record to this book; accuracy is
            # reported for transparency, not used as a gate.
            m["accuracy"], _ = _score(query, rec_title, "", m["year"])
            matches.append(m)

    matches.sort(key=lambda m: (m["full_view"], m["accuracy"]), reverse=True)
    out["matches"] = matches[:MAX_MATCHES]
    out["best_match"] = matches[0] if matches else None
    out["available"] = bool(any(m["items"] for m in matches))
    out["full_view"] = any(m["full_view"] for m in matches)
    return out


# --- combined ----------------------------------------------------------------

def search_scans(title: str, author: str | None = None, year: str | None = None) -> dict:
    """Search both sources for existing scans of the book."""
    return {
        "query_title": title or "",
        "query_author": author or "",
        "query_year": year or "",
        "internet_archive": search_internet_archive(title, author, year),
        "hathitrust": search_hathitrust(title, author, year),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search the Internet Archive and HathiTrust for scans of a book."
    )
    parser.add_argument("--title", default="", help="Book title to search for.")
    parser.add_argument("--author", default="", help="Optional author to refine.")
    parser.add_argument("--year", default="", help="Optional publishing year to refine.")
    args = parser.parse_args()

    result = search_scans(args.title, args.author or None, args.year or None)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
