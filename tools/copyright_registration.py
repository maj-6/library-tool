"""US copyright REGISTRATION lookups (network) for the copyright split tag.

Unlike catalog_checks (offline renewals), this queries live/remote registration
sources so a book can show whether an original copyright registration exists:

  - cprs : the U.S. Copyright Office public records system
           (https://publicrecords.copyright.gov) via its simple_search_dsl API,
           which covers registrations from 1978 to the present as well as the
           historical card records currently included by CPRS.
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
import threading
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


class RegistrationLookupError(RuntimeError):
    """A registration source could not be searched reliably.

    This is distinct from a successful search with no matching record. Callers
    may cache the latter, but should retry the former.
    """


def _first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def _cprs_query(title: str, author: str = "") -> str:
    """Build a CPRS phrase query so common title words do not swamp results."""
    def phrase(value):
        return '"' + str(value).replace('"', " ").strip() + '"'

    parts = [phrase(title)]
    if str(author or "").strip():
        parts.append(phrase(author))
    return " ".join(parts)


def _cprs_titles(hit: dict) -> list[str]:
    """Return title candidates from both CPRS card and post-1978 schemas."""
    values = hit.get("title_of_work") or []
    titles = list(values if isinstance(values, list) else [values])
    for item in hit.get("primary_titles_list") or []:
        if not isinstance(item, dict):
            continue
        proper = item.get("title_primary_title_title_proper")
        if proper:
            titles.append(str(proper))
    for item in hit.get("title_application_list") or []:
        if isinstance(item, dict) and item.get("title_application_title"):
            titles.append(str(item["title_application_title"]))
    if not titles and hit.get("title_concatenated"):
        titles.append(str(hit["title_concatenated"]))
    return list(dict.fromkeys(t for t in titles if t))


def _cprs_authors(hit: dict) -> str:
    """Return author text from both CPRS card and post-1978 schemas."""
    values = hit.get("author") or []
    authors = list(values if isinstance(values, list) else [values])
    if not authors:
        for person in (hit.get("display_names") or {}).get("persons") or []:
            if (isinstance(person, dict)
                    and "author" in (person.get("roles") or [])
                    and person.get("name")):
                authors.append(person["name"])
    if not authors:
        for item in hit.get("author_statement_list") or []:
            if isinstance(item, dict) and item.get("author_full_name"):
                authors.append(item["author_full_name"])
    return "; ".join(str(a).strip() for a in authors if str(a).strip())


def _cprs_is_book(hit: dict) -> bool:
    work = hit.get("cc_type_of_work") or hit.get("type_of_work") or ""
    allwork = hit.get("all_type_of_work") or []
    classes = hit.get("registration_class") or []
    allwork = allwork if isinstance(allwork, list) else [allwork]
    classes = classes if isinstance(classes, list) else [classes]
    kinds = {str(v).lower() for v in [work, *allwork, *classes] if v}
    if not kinds:
        return True
    return bool(kinds & {"book", "text", "tx"})


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
    params = urllib.parse.urlencode({
        "query": _cprs_query(title, author),
        "records_per_page": "40",
        "page_number": "0",
    })
    req = urllib.request.Request(
        CPRS_SEARCH_URL + "?" + params,
        headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise RegistrationLookupError("CPRS registration lookup unavailable") from exc
    target_year = None
    match = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", str(year_value or ""))
    if match:
        target_year = int(match.group(1))
    candidates: list[tuple[int, int, dict]] = []
    for position, row in enumerate(data.get("data") or []):
        hit = row.get("hit") or {}
        if hit.get("type_of_record") != "registration":
            continue
        if not _cprs_is_book(hit):
            continue
        cand_author = _cprs_authors(hit)
        for ct in _cprs_titles(hit):
            if ct and cc.title_author_match(title, author, str(ct), cand_author):
                regnum = (hit.get("copyright_number_for_display")
                          or _first(hit.get("registration_number")))
                ryear = (hit.get("first_published_date_as_year")
                         or hit.get("publication_date_as_year")
                         or hit.get("fee_date_as_year")
                         or str(hit.get("registration_date") or "")[:4])
                result = {
                    "source": "cprs",
                    "reg_number": str(regnum or "").strip(),
                    "title": str(ct),
                    "author": cand_author,
                    "year": str(ryear or "").strip(),
                    "record_id": hit.get("public_records_id") or "",
                }
                found_year = None
                ymatch = re.search(r"\b(1[5-9]\d{2}|20\d{2})\b", result["year"])
                if ymatch:
                    found_year = int(ymatch.group(1))
                distance = (abs(found_year - target_year)
                            if target_year is not None and found_year is not None
                            else 10_000)
                candidates.append((distance, position, result))
                break
    return min(candidates, default=(0, 0, None))[2]


# --- NYPL Catalog of Copyright Entries (books 1923-1963) --------------------
# The dataset is the parsed XML from github.com/NYPL/catalog_of_copyright_entries_project
# (its `xml/` tree of <copyrightEntry> records). NYPL matching is OFF unless the
# dataset directory is present. The server points NYPL_DIR at <DATA_ROOT>/nypl_cce;
# WHL_NYPL_CCE_DIR overrides. A parsed index is cached beside the data so repeat
# runs don't re-scan ~640k records.
NYPL_DIR: str | None = None
_nypl_index: dict | None = None   # {"sig", "entries": [ {title,author,regnum,year} ], "by_token": {tok:[i]}}
_nypl_lock = threading.Lock()

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
            h.update(f"{p.relative_to(root).as_posix()}|{st.st_size}|{st.st_mtime!r}".encode())
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
            regnum = (el.get("regnum") or "").strip().rstrip(".")
            if not regnum:
                rn = el.find("regNum")
                regnum = ("".join(rn.itertext()).strip().rstrip(".") if rn is not None else "")
            rd = el.find("regDate")
            date = (rd.get("date") if rd is not None else "") or ""
            year = date[:4] if date[:4].isdigit() else ""
            entries.append({"title": title, "author": author,
                            "regnum": regnum, "year": year, "date": date})
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
    with _nypl_lock:   # serialize the heavy build; another thread may have won
        if _nypl_index is not None and _nypl_index.get("sig") == sig:
            return _nypl_index
        cache = root / ".nypl_index.json"
        entries = None
        if cache.is_file():
            try:
                blob = json.loads(cache.read_text("utf-8"))
                if blob.get("sig") == sig and isinstance(blob.get("entries"), list):
                    entries = blob["entries"]
            except Exception:
                entries = None
        if entries is None:
            entries = _parse_nypl_dir(root)
            try:                                   # atomic write (no torn cache file)
                tmp = cache.with_name(cache.name + ".tmp")
                tmp.write_text(json.dumps({"sig": sig, "entries": entries}), "utf-8")
                os.replace(tmp, cache)
            except Exception:
                pass
        by_token: dict[str, list[int]] = {}
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                continue
            for tok in set(_title_tokens(e.get("title", ""))):
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
    # union of every shared title token's postings (mirrors catalog_checks
    # candidate gathering) — a min()-by-rarest-token prefilter would miss a valid
    # record whose only shared token happens to be a common one.
    candidates: set[int] = set()
    for t in set(toks):
        candidates.update(by_token.get(t, ()))
    for i in candidates:
        e = entries[i]
        if not isinstance(e, dict):
            continue
        if cc.title_author_match(title, author, e.get("title", ""), e.get("author", "")):
            return {
                "source": "nypl",
                "reg_number": e.get("regnum", ""),
                "title": e.get("title", ""),
                "author": e.get("author", ""),
                "year": e.get("year", ""),
                "date": e.get("date", ""),   # full registration date when the CCE has one
                "record_id": "",
            }
    return None


def registration_lookup(title: str, author: str = "", year_value=None,
                        sources=("cprs",)) -> dict:
    """Combined registration lookup across the enabled sources.

    Returns {"found": bool, "sources": [names that matched], "match": <first>}.
    """
    matched: list[str] = []
    unavailable: list[str] = []
    first = None
    for src in sources:
        try:
            if src == "cprs":
                m = cprs_registration(title, author, year_value)
            elif src == "nypl":
                m = nypl_registration(title, author, year_value)
            else:
                m = None
        except RegistrationLookupError:
            unavailable.append(src)
            continue
        if m:
            matched.append(src)
            if first is None:
                first = m
    if not matched and unavailable:
        names = ", ".join(name.upper() for name in unavailable)
        raise RegistrationLookupError(f"{names} registration lookup unavailable")
    return {"found": bool(matched), "sources": matched, "match": first}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="")
    ap.add_argument("--year", default="")
    a = ap.parse_args()
    print(json.dumps(registration_lookup(a.title, a.author, a.year), indent=2))
