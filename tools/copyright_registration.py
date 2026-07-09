"""US copyright REGISTRATION lookups (network) for the copyright split tag.

Unlike catalog_checks (offline renewals), this queries live/remote registration
sources so a book can show whether an original copyright registration exists:

  - cprs : the U.S. Copyright Office public records system
           (https://publicrecords.copyright.gov) via its simple_search_dsl API,
           which covers pre-1978 Catalog of Copyright Entries card records.
  - nypl : the NYPL "Catalog of Copyright Entries" registrations dataset
           (books 1923-1964); optional, wired when the dataset is present.

Kept out of catalog_checks so that module stays entirely offline. Title/author
matching reuses catalog_checks.title_author_match (the shared cross-database
identity test).
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import catalog_checks as cc  # noqa: E402  (title_author_match)

CPRS_SEARCH_URL = ("https://api.publicrecords.copyright.gov"
                   "/search_service_external/simple_search_dsl")
_UA = "Mozilla/5.0 (world-herb-library tools)"

# All known registration sources, in preference order.
SOURCES = ("cprs", "nypl")


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def cprs_registration(title: str, author: str = "", year_value=None,
                      timeout: float = 12.0) -> dict | None:
    """Best matching book REGISTRATION on publicrecords.copyright.gov, or None.

    Queries simple_search_dsl by title, keeps only book registrations (not
    recordations or the card index), and accepts the first record that passes
    the shared title/author identity test.
    """
    title = str(title or "").strip()
    if not title:
        return None
    params = urllib.parse.urlencode(
        {"query": title, "records_per_page": "40", "page_number": "0"})
    req = urllib.request.Request(
        CPRS_SEARCH_URL + "?" + params,
        headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    for row in (data.get("data") or []):
        hit = row.get("hit") or {}
        if hit.get("type_of_record") != "registration":
            continue
        work = hit.get("cc_type_of_work") or ""
        allwork = hit.get("all_type_of_work") or []
        if work and work not in ("book", "text") and "book" not in allwork:
            continue
        authors = hit.get("author") or []
        cand_author = ("; ".join(a for a in authors if a)
                       if isinstance(authors, list) else str(authors or ""))
        titles = hit.get("title_of_work") or []
        for ct in (titles if isinstance(titles, list) else [titles]):
            if ct and cc.title_author_match(title, author, str(ct), cand_author):
                regnum = (hit.get("copyright_number_for_display")
                          or _first(hit.get("registration_number")))
                ryear = (hit.get("first_published_date_as_year")
                         or hit.get("fee_date_as_year") or "")
                return {
                    "source": "cprs",
                    "reg_number": str(regnum or "").strip(),
                    "title": str(ct),
                    "author": cand_author,
                    "year": str(ryear or "").strip(),
                    "record_id": hit.get("public_records_id") or "",
                }
    return None


def nypl_registration(title: str, author: str = "", year_value=None) -> dict | None:
    """Placeholder for the NYPL CCE registrations dataset lookup (offline).

    Returns None until the dataset is fetched and indexed; wiring it here keeps
    registration_lookup source-agnostic.
    """
    return None


def registration_lookup(title: str, author: str = "", year_value=None,
                        sources=("cprs",)) -> dict:
    """Combined registration lookup across the enabled sources.

    Returns {"found": bool, "sources": [names that matched], "match": <first>}.
    """
    matched: list[str] = []
    first = None
    for src in sources:
        if src == "cprs":
            m = cprs_registration(title, author, year_value)
        elif src == "nypl":
            m = nypl_registration(title, author, year_value)
        else:
            m = None
        if m:
            matched.append(src)
            if first is None:
                first = m
    return {"found": bool(matched), "sources": matched, "match": first}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--year", default="")
    a = ap.parse_args()
    print(json.dumps(registration_lookup(a.title, a.author, a.year), indent=2))
