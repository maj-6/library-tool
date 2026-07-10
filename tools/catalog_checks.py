"""Offline copyright and WHL-catalogue checks shared across the tools.

Centralizes the cross-database identity test (title-forward, surname-based)
plus the loaders/indexes for the two local databases:
  - copyright_renewals.csv  (Catalog of Copyright Entries renewal records)
  - whl_catalog.csv         (World Herb Library catalogue export)

Used by tools/build_catalog_report.py for the status report and by the review
web app (tools/webapp/server.py) to check every submitted entry. Both checks
are entirely local; no network calls.
"""
from __future__ import annotations

import csv
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import libcommon as lib  # noqa: E402
import whl_client as whl  # noqa: E402

# reference data shipped read-only with the app
RENEWALS_CSV = lib.APP_ROOT / "copyright_renewals.csv"
WHL_CATALOG_CSV = lib.APP_ROOT / "whl_catalog.csv"

# Renewal era: works published in this inclusive window needed a renewal to
# keep copyright. Older works are public domain by age; newer were auto-renewed.
RENEWAL_ERA_END = 1963
AUTO_RENEW_END = 1977

# Title/author match acceptance shared by the WHL and renewals lookups.
# Matching is title-forward: a strong title prefix plus a full-title ratio,
# confirmed by an order-agnostic surname overlap. Year is never required
# (editions differ) and author order/format differences do not veto a match.
TITLE_PREFIX_MIN = 0.72   # first-16-char prefix similarity
TITLE_FULL_MIN = 0.62     # full-title ratio when a surname token agrees
TITLE_FULL_MISSING = 0.82 # full-title ratio when one side lacks an author
TITLE_FULL_STRICT = 0.90  # full-title ratio when authors look different


# --- title/author matching ---------------------------------------------------

def last_token(author: str) -> str:
    """Best-guess surname token from a normalized, order-flipped author name."""
    norm = whl._normalize(whl.flip_author(author or ""))
    toks = [t for t in norm.split() if len(t) >= 3]
    return toks[-1] if toks else ""


_AUTHOR_STOP = {
    "and", "the", "of", "by", "md", "phd", "dr", "prof", "sir", "mrs", "mr",
    "jr", "sr", "esq", "co", "company", "sons", "inc", "press", "editor",
    "edited", "author", "anon", "anonymous", "unknown", "various", "new",
}


def author_tokens(author: str) -> set:
    """Order-agnostic surname-ish tokens (len>=4, minus stopwords)."""
    norm = whl._normalize(whl.flip_author(author or ""))
    return {t for t in norm.split() if len(t) >= 4 and t not in _AUTHOR_STOP}


def _candidates(title: str, author: str, index: dict) -> set:
    """Row indices sharing a surname token or the title prefix."""
    cand: set = set()
    for tok in author_tokens(author):
        cand.update(index["by_author"].get(tok, []))
    prefix = whl._normalize(title)[:12]
    if prefix:
        cand.update(index["by_title"].get(prefix, []))
    return cand


def title_author_match(title: str, author: str, cand_title: str, cand_author: str) -> bool:
    """Tolerant cross-database identity test.

    Requires a strong leading-title match, then confirms with a full-title
    ratio whose bar depends on author agreement: lenient when a surname token
    is shared, medium when one side has no author, strict when the authors
    look different (guards against same-prefix but different books).
    """
    if whl.similarity_prefix(title, cand_title, whl.TITLE_PREFIX) < TITLE_PREFIX_MIN:
        return False
    tf = whl.similarity(title, cand_title)
    ours, theirs = author_tokens(author), author_tokens(cand_author)
    if ours & theirs:
        return tf >= TITLE_FULL_MIN
    if not ours or not theirs:
        return tf >= TITLE_FULL_MISSING
    return tf >= TITLE_FULL_STRICT


# --- copyright renewals index ------------------------------------------------

def load_renewals() -> dict:
    """Load renewals and index them by surname token and title prefix."""
    by_author: dict[str, list] = {}
    by_title: dict[str, list] = {}
    entries: list[dict] = []
    if not RENEWALS_CSV.exists():
        return {"entries": entries, "by_author": by_author, "by_title": by_title}

    with open(RENEWALS_CSV, "r", encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            title = raw.get("TITLE", "") or ""
            author = raw.get("AUTHOR", "") or ""
            t_norm = whl._normalize(title)
            if not t_norm:
                continue
            entry = {
                "id": raw.get("ID", "") or "",
                "title": title,
                "author": author,
                "t_norm": t_norm,
            }
            idx = len(entries)
            entries.append(entry)
            by_title.setdefault(t_norm[:12], []).append(idx)
            for tok in author_tokens(author):
                by_author.setdefault(tok, []).append(idx)
    return {"entries": entries, "by_author": by_author, "by_title": by_title}


def renewal_match(title: str, author: str, ren: dict) -> dict | None:
    """Return the best matching renewal entry for this book, or None."""
    entries = ren["entries"]
    best, best_tf = None, 0.0
    for i in _candidates(title, author, ren):
        e = entries[i]
        if not title_author_match(title, author, e["title"], e["author"]):
            continue
        tf = whl.similarity(title, e["title"])
        if tf > best_tf:
            best, best_tf = e, tf
    return best


def renewal_details(ids) -> dict[str, dict]:
    """Registration/renewal dates for renewal IDs, by one scan of the CSV.

    The in-memory index (246k rows) deliberately holds only what matching needs;
    carrying three more date strings per row would cost ~25 MB for a field the
    tooltip wants on a handful of books. Callers cache the result instead.
    """
    want = {str(i).strip() for i in ids if str(i).strip()}
    out: dict[str, dict] = {}
    if not want or not RENEWALS_CSV.exists():
        return out
    with open(RENEWALS_CSV, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for raw in csv.DictReader(fh):
            rid = (raw.get("ID") or "").strip()
            if rid not in want:
                continue
            out[rid] = {
                "id": rid,
                "renewal_year": (raw.get("DATE") or "").strip(),
                "renewal_date": (raw.get("DREG") or "").strip(),      # e.g. 12Jun50
                "registration_date": (raw.get("ODAT") or "").strip(),  # e.g. 24Jun23
                "registration_number": (raw.get("OREG") or "").strip(),
            }
            if len(out) == len(want):
                break
    return out


def copyright_status_for(
    title: str, author: str, year_value, ren: dict, this_year: int | None = None
) -> str:
    """US-centric copyright status for a title/author/year (heuristic).

    Public domain by age (95-year term expired); 1931..RENEWAL_ERA_END looks
    the work up in the renewals set; then the auto-renewal era; then simply
    in copyright. Missing/unparseable year -> Unknown.
    """
    year = whl._year(year_value)
    if not year:
        return "Unknown (no year)"
    if this_year is None:
        this_year = datetime.now().year
    y = int(year)
    if y <= this_year - 96:
        return f"Public domain (published {y})"
    if y <= RENEWAL_ERA_END:
        if not ren["entries"]:
            return "Unknown (renewals database missing)"
        match = renewal_match(title, author, ren)
        if match:
            return f"In copyright (renewal {match['id']})"
        return "Public domain (no renewal found)"
    if y <= AUTO_RENEW_END:
        return "In copyright (auto-renewed)"
    return "In copyright"


# --- World Herb Library catalogue ---------------------------------------------

def load_whl_catalog() -> dict:
    """Load the WHL catalogue CSV and index it by surname token / title prefix."""
    by_author: dict[str, list] = {}
    by_title: dict[str, list] = {}
    entries: list[dict] = []
    if not WHL_CATALOG_CSV.exists():
        return {"entries": entries, "by_author": by_author, "by_title": by_title}

    with open(WHL_CATALOG_CSV, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        for raw in csv.DictReader(fh):
            title = raw.get("Title", "") or ""
            t_norm = whl._normalize(title)
            if not t_norm:
                continue
            author = raw.get("Authors", "") or ""
            entry = {
                "whl_title": title,
                "author": author,
                "pub_date": whl._year(raw.get("Year Published")) or "",
                "status": (raw.get("Status", "") or "").strip().lower(),
                "permalink": (raw.get("Permalink", "") or "").strip(),
            }
            idx = len(entries)
            entries.append(entry)
            by_title.setdefault(t_norm[:12], []).append(idx)
            for tok in author_tokens(author):
                by_author.setdefault(tok, []).append(idx)
    return {"entries": entries, "by_author": by_author, "by_title": by_title}


def whl_match(title: str, author: str, cat: dict) -> tuple[str, dict | None]:
    """Best local WHL catalogue match for a book.

    Returns (flag, entry): flag is 'yes' when a published catalogue entry
    matches, 'draft' when only unpublished entries match, 'no' otherwise;
    entry is the best-matching catalogue record (published preferred), ranked
    by full-title similarity.
    """
    entries = cat["entries"]
    best_publish = best_any = None
    best_publish_tf = best_any_tf = 0.0
    for i in _candidates(title, author, cat):
        e = entries[i]
        if not title_author_match(title, author, e["whl_title"], e["author"]):
            continue
        tf = whl.similarity(title, e["whl_title"])
        if tf > best_any_tf:
            best_any, best_any_tf = e, tf
        if e["status"] == "publish" and tf > best_publish_tf:
            best_publish, best_publish_tf = e, tf
    if best_publish is not None:
        return "yes", best_publish
    if best_any is not None:
        return "draft", best_any
    return "no", None


def whl_catalog_flag(title: str, author: str, year: str, cat: dict) -> str:
    """Return whether the book is in the WHL catalogue.

    yes   -> a published catalogue entry matches
    draft -> only unpublished (draft) entries match
    no    -> no confident match

    Matching is title-forward and surname-based (see title_author_match), so
    'Lastname, Initials' vs 'Firstname Lastname' and edition-year differences
    do not block a real match. 'year' is accepted for call-site symmetry.
    """
    flag, _ = whl_match(title, author, cat)
    return flag


# --- process-cached indexes and the combined check -----------------------------

_cache_lock = threading.Lock()
_cached: dict[str, dict] = {}


def get_renewals() -> dict:
    """load_renewals(), loaded once per process (the CSV is ~40 MB)."""
    with _cache_lock:
        if "renewals" not in _cached:
            _cached["renewals"] = load_renewals()
        return _cached["renewals"]


def get_whl_catalog() -> dict:
    """load_whl_catalog(), loaded once per process."""
    with _cache_lock:
        if "whl" not in _cached:
            _cached["whl"] = load_whl_catalog()
        return _cached["whl"]


def check_entry(title: str, author: str = "", year_value=None) -> dict:
    """Run both offline checks for one submitted entry.

    Returns a JSON-serializable dict:
      copyright_status  status string (see copyright_status_for)
      in_whl            'yes' | 'draft' | 'no' | 'unknown' (catalogue missing)
      whl_match         matched catalogue record, or None
      checked_at        UTC timestamp
    """
    ren = get_renewals()
    cat = get_whl_catalog()
    out: dict = {
        "copyright_status": copyright_status_for(title, author, year_value, ren),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not cat["entries"]:
        out["in_whl"] = "unknown"
        out["whl_match"] = None
        return out
    flag, match = whl_match(title, author, cat)
    out["in_whl"] = flag
    out["whl_match"] = (
        {
            "title": match["whl_title"],
            "author": match["author"],
            "year": match["pub_date"],
            "status": match["status"],
            "permalink": match["permalink"],
        }
        if match
        else None
    )
    return out
