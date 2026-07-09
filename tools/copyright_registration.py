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
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
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


# --- NYPL Catalog of Copyright Entries (books 1923-1963) --------------------
# The dataset is the parsed XML from github.com/NYPL/catalog_of_copyright_entries_project
# (its `xml/` tree of <copyrightEntry> records). NYPL matching is OFF unless the
# dataset directory is present. The server points NYPL_DIR at <DATA_ROOT>/nypl_cce;
# WHL_NYPL_CCE_DIR overrides. A parsed index is cached beside the data so repeat
# runs don't re-scan ~640k records.
NYPL_DIR: str | None = None
_nypl_index: dict | None = None   # {"sig", "entries": [ {title,author,regnum,year} ], "by_token": {tok:[i]}}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with",
         "by", "de", "la", "le", "el", "und", "der", "die", "das"}


def _title_tokens(title: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(str(title or "").lower())
            if len(t) > 2 and t not in _STOP]


def _nypl_dataset_dir() -> Path | None:
    for cand in (NYPL_DIR, os.environ.get("WHL_NYPL_CCE_DIR")):
        if cand and Path(cand).is_dir():
            return Path(cand)
    return None


def _nypl_signature(root: Path) -> str:
    import hashlib
    h = hashlib.sha1()
    for p in sorted(root.rglob("*.xml")):
        try:
            st = p.stat()
            h.update(f"{p.name}|{st.st_size}|{int(st.st_mtime)}".encode())
        except OSError:
            pass
    return h.hexdigest()[:16]


def _parse_nypl_dir(root: Path) -> list[dict]:
    entries: list[dict] = []
    for xf in sorted(root.rglob("*.xml")):
        try:
            tree = ET.parse(str(xf))
        except (ET.ParseError, OSError):
            continue
        for el in tree.iter("copyrightEntry"):
            te = el.find("title")   # note: an Element with no children is falsy, so `is not None`
            title = ("".join(te.itertext()).strip() if te is not None else "")
            if not title:
                continue
            an = el.find("author/authorName")
            author = ("".join(an.itertext()).strip() if an is not None else "")
            regnum = (el.get("regnum") or "").strip()
            if not regnum:
                rn = el.find("regNum")
                regnum = ("".join(rn.itertext()).strip().rstrip(".") if rn is not None else "")
            rd = el.find("regDate")
            date = (rd.get("date") if rd is not None else "") or ""
            year = date[:4] if date[:4].isdigit() else ""
            entries.append({"title": title, "author": author,
                            "regnum": regnum, "year": year})
    return entries


def _load_nypl_index() -> dict | None:
    """Lazily build (and disk-cache) the title-token index of the NYPL dataset."""
    global _nypl_index
    root = _nypl_dataset_dir()
    if root is None:
        return None
    sig = _nypl_signature(root)
    if _nypl_index is not None and _nypl_index.get("sig") == sig:
        return _nypl_index
    cache = root / ".nypl_index.json"
    entries = None
    if cache.is_file():
        try:
            blob = json.loads(cache.read_text("utf-8"))
            if blob.get("sig") == sig:
                entries = blob.get("entries")
        except Exception:
            entries = None
    if entries is None:
        entries = _parse_nypl_dir(root)
        try:
            cache.write_text(json.dumps({"sig": sig, "entries": entries}), "utf-8")
        except Exception:
            pass
    by_token: dict[str, list[int]] = {}
    for i, e in enumerate(entries):
        for tok in set(_title_tokens(e["title"])):
            by_token.setdefault(tok, []).append(i)
    _nypl_index = {"sig": sig, "entries": entries, "by_token": by_token}
    return _nypl_index


def nypl_registration(title: str, author: str = "", year_value=None) -> dict | None:
    """Best matching book registration in the NYPL CCE dataset, or None.

    Prefilters candidates by the rarest shared title token, then applies the
    shared title/author identity test. Inert (None) when the dataset is absent.
    """
    title = str(title or "").strip()
    if not title:
        return None
    idx = _load_nypl_index()
    if not idx:
        return None
    toks = _title_tokens(title)
    if not toks:
        return None
    by_token, entries = idx["by_token"], idx["entries"]
    postings = [by_token.get(t, ()) for t in set(toks)]
    postings = [p for p in postings if p]
    if not postings:
        return None
    candidates = min(postings, key=len)   # the most selective title token
    for i in candidates:
        e = entries[i]
        if cc.title_author_match(title, author, e["title"], e["author"]):
            return {
                "source": "nypl",
                "reg_number": e["regnum"],
                "title": e["title"],
                "author": e["author"],
                "year": e["year"],
                "record_id": "",
            }
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
