"""Characterization tests for the scan-search module (IA + HathiTrust).

Pins the CURRENT behavior of scan_search's pure helpers (_title_words,
_surname, _score) and of both search functions with the single network
call site — scan_search._get_json — replaced by a fake. Every fixture is
inlined below and every golden value was produced by executing the real
code, so these are offline, deterministic goldens: several pinned values
are known quirks (empty-vs-empty titles scoring a perfect accuracy,
prefix-16-only title comparison, IA matches not filtered by the match
threshold, corroboration outranking accuracy) and are pinned as-is on
purpose. A failure here means the search behavior CHANGED, not that it
is correct.

conftest.py sets WHL_DATA_ROOT before collection, which scan_search
needs (it imports catalog_checks -> libcommon, which reads the env var
once at import). No real network request is ever made.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import scan_search


# The one query used throughout: a real book with a "Lastname, Initials"
# catalogue author, so the surname-token paths are all exercised.
QUERY_TITLE = "American Medicinal Plants"
QUERY_AUTHOR = "Millspaugh, C. F."
QUERY_YEAR = "1887"


def _install_fake_get(monkeypatch, respond):
    """Replace scan_search._get_json; returns the list of URLs requested.

    respond(url) returns the JSON dict for that URL, or an Exception
    instance to be raised. Patching the module attribute isolates every
    caller: it is the module's only network call site.
    """
    calls: list[str] = []

    def fake(url: str) -> dict:
        calls.append(url)
        out = respond(url)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(scan_search, "_get_json", fake)
    return calls


# --- module constants ----------------------------------------------------------

def test_constants_current_values():
    """Pin the tuning constants so a retune is an explicit, visible diff."""
    assert scan_search.MATCH_THRESHOLD == 0.6
    assert scan_search.MAX_MATCHES == 5
    assert scan_search.MAX_OCLCS == 8
    assert scan_search._W_TITLE == 0.5
    assert scan_search._W_AUTHOR == 0.3
    assert scan_search._W_DATE == 0.2


# --- _title_words ----------------------------------------------------------------

@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("American Medicinal Plants", "american medicinal plants"),
        # possessive 's is dropped, straight and curly apostrophes alike
        ("Culpeper's Complete Herbal", "culpeper complete herbal"),
        ("Culpeper’s Complete Herbal", "culpeper complete herbal"),
        # non-possessive apostrophes split the token
        ("L'Herbier de France", "l herbier de france"),
        # capped at the first 8 words
        ("one two three four five six seven eight nine",
         "one two three four five six seven eight"),
        ("", ""),
        (None, ""),  # accepts None despite the str annotation
        ("!!! ???", ""),
    ],
)
def test_title_words(title, expected):
    assert scan_search._title_words(title) == expected


# --- _surname --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("Millspaugh, C. F.", "millspaugh"),  # initials < 4 chars dropped
        ("Charles Frederick Millspaugh", "charles frederick millspaugh"),
        (None, ""),
        ("Smith, Jr.", "smith"),
        ("Li, X.", ""),  # every token too short -> empty
        # tokens come back alphabetically sorted, not in name order
        ("Zeta Aaronson", "aaronson zeta"),
    ],
)
def test_surname(author, expected):
    assert scan_search._surname(author) == expected


# --- _score ----------------------------------------------------------------------

_Q = {"title": QUERY_TITLE, "author": QUERY_AUTHOR, "date": QUERY_YEAR}


@pytest.mark.parametrize(
    ("query", "title", "author", "year", "expected"),
    [
        # full agreement on all three components
        (_Q, "American medicinal plants", "Millspaugh, Charles Frederick", "1887",
         (1.0, 1.0)),
        # year mismatch: binary date component zeroes out
        (_Q, "American medicinal plants", "Millspaugh, Charles Frederick", "1892",
         (0.8, 0.8)),
        # authorless candidate: accuracy renormalizes (perfect), corroboration drops
        (_Q, "American medicinal plants", "", "1887", (1.0, 0.7)),
        (_Q, "American medicinal plants", "", "", (1.0, 0.5)),
        # wrong author: binary author component zeroes out
        (_Q, "American medicinal plants", "Jones, Robert", "1887", (0.7, 0.7)),
        # unrelated book
        (_Q, "Gardening for Ladies", "Loudon, Jane", "1840", (0.156, 0.156)),
        # title comparison is prefix-16 only: both normalize to
        # "american medicin" and differences after that are invisible
        (_Q, "American medicinal flora", "Millspaugh, Charles Frederick", "1887",
         (1.0, 1.0)),
        # components missing on the QUERY side also renormalize away
        ({"title": QUERY_TITLE, "author": "", "date": QUERY_YEAR},
         "American medicinal plants", "Millspaugh", "1887", (1.0, 0.7)),
        ({"title": QUERY_TITLE, "author": QUERY_AUTHOR, "date": ""},
         "American medicinal plants", "Millspaugh", "1887", (1.0, 0.8)),
        # quirk: SequenceMatcher("", "").ratio() == 1.0, so empty-vs-empty
        # titles score a perfect accuracy — pinned as-is
        ({"title": "", "author": "", "date": ""}, "", "", "", (1.0, 0.5)),
        # years are str()-coerced and regex-extracted from date-ish strings
        (_Q, "American medicinal plants", "Millspaugh, Charles Frederick", 1887,
         (1.0, 1.0)),
        (_Q, "American medicinal plants", "Millspaugh, Charles Frederick",
         "c. 1887, revised", (1.0, 1.0)),
    ],
)
def test_score(query, title, author, year, expected):
    acc, rank = scan_search._score(query, title, author, year)
    assert acc == pytest.approx(expected[0])
    assert rank == pytest.approx(expected[1])


# --- search_internet_archive -------------------------------------------------------

# Five IA docs: an exact hit, a year mismatch, an authorless copy, a
# restricted (borrow-only) copy, and an unrelated book.
_IA_DOCS = [
    {
        "identifier": "americanmedicin01mill",
        "title": "American medicinal plants",
        # a creator LIST is joined with "; "
        "creator": ["Millspaugh, Charles Frederick, 1854-1923"],
        "year": 1887,  # int year is str()-coerced
    },
    {
        "identifier": "americanmedicin02mill",
        "title": "American medicinal plants",
        "creator": "Millspaugh, Charles Frederick, 1854-1923",
        "year": "1892",
    },
    {
        "identifier": "americanmedicin03anon",
        "title": "American medicinal plants",
        "year": "1887",
    },
    {
        "identifier": "americanmedicin04borrow",
        "title": "American medicinal plants",
        "creator": "Millspaugh, Charles Frederick, 1854-1923",
        "year": "1887",
        "access-restricted-item": "true",
    },
    {
        "identifier": "gardeningforladies00loud",
        "title": "Gardening for Ladies",
        "creator": "Loudon, Jane",
        "year": "1840",
    },
]


def _ia_response(docs):
    return {"response": {"docs": docs}}


def test_ia_happy_path_ranking_and_ladder_break(monkeypatch):
    calls = _install_fake_get(monkeypatch, lambda url: _ia_response(_IA_DOCS))
    out = scan_search.search_internet_archive(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    # the first (most precise) ladder query found a rank >= 0.8 hit, so the
    # three looser queries never run
    assert len(calls) == 1
    assert calls[0] == (
        "https://archive.org/advancedsearch.php"
        "?q=title%3A%28%22american+medicinal+plants%22%29"
        "+AND+mediatype%3Atexts+AND+creator%3A%28millspaugh%29"
        "&fl%5B%5D=identifier&fl%5B%5D=title&fl%5B%5D=creator&fl%5B%5D=year"
        "&fl%5B%5D=access-restricted-item&rows=10&output=json"
    )
    assert out["search_url"] == (
        "https://archive.org/search?query=American+Medicinal+Plants+Millspaugh%2C+C.+F."
    )

    # restricted doc excluded; the rest sorted by (corroboration, accuracy).
    # Quirk pinned as-is: the authorless copy (accuracy 1.0, rank 0.7) sorts
    # BELOW the wrong-year copy (rank 0.8), and the 0.156 unrelated hit is
    # NOT filtered by MATCH_THRESHOLD — only `available` is gated.
    assert [m["identifier"] for m in out["matches"]] == [
        "americanmedicin01mill",
        "americanmedicin02mill",
        "americanmedicin03anon",
        "gardeningforladies00loud",
    ]
    assert [m["accuracy"] for m in out["matches"]] == pytest.approx(
        [1.0, 0.8, 1.0, 0.156]
    )
    for m in out["matches"]:
        assert "_rank" not in m  # internal sort key is stripped before return

    best = out["best_match"]
    assert best == {
        "identifier": "americanmedicin01mill",
        "title": "American medicinal plants",
        "author": "Millspaugh, Charles Frederick, 1854-1923",
        "year": "1887",
        "url": "https://archive.org/details/americanmedicin01mill",
        "downloadable": True,
        "accuracy": 1.0,
    }
    assert out["available"] is True
    assert out["no_download"] is False


def test_ia_all_results_restricted(monkeypatch):
    restricted = [d for d in _IA_DOCS if d.get("access-restricted-item") == "true"]
    calls = _install_fake_get(monkeypatch, lambda url: _ia_response(restricted))
    out = scan_search.search_internet_archive(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    # the restricted hit still ranks 1.0 and breaks the ladder, but only
    # downloadable matches are reported
    assert len(calls) == 1
    assert out["matches"] == []
    assert out["best_match"] is None
    assert out["available"] is False
    assert out["no_download"] is True


def test_ia_empty_response_runs_whole_ladder(monkeypatch):
    calls = _install_fake_get(monkeypatch, lambda url: {})
    out = scan_search.search_internet_archive(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert len(calls) == 4  # with an author the ladder is 4 queries
    assert out["matches"] == []
    assert out["best_match"] is None
    assert out["available"] is False
    assert out["no_download"] is False
    assert "error" not in out


def test_ia_empty_response_ladder_without_author(monkeypatch):
    calls = _install_fake_get(monkeypatch, lambda url: {})
    out = scan_search.search_internet_archive(QUERY_TITLE)

    assert len(calls) == 2  # without an author the ladder is only 2 queries
    assert out["available"] is False


def test_ia_network_error_is_reported(monkeypatch):
    calls = _install_fake_get(monkeypatch, lambda url: ValueError("boom"))
    out = scan_search.search_internet_archive(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert len(calls) == 1
    assert out["error"] == "ValueError: boom"
    assert out["available"] is None
    assert out["best_match"] is None
    assert out["matches"] == []
    assert "no_download" not in out  # error path returns before it is set


@pytest.mark.parametrize(
    ("title", "url_suffix"),
    [
        ("", "query="),
        ("!!!", "query=%21%21%21"),  # normalizes to no words at all
    ],
)
def test_ia_empty_query_short_circuits(monkeypatch, title, url_suffix):
    calls = _install_fake_get(monkeypatch, lambda url: _ia_response(_IA_DOCS))
    out = scan_search.search_internet_archive(title)

    assert calls == []  # no request at all
    assert out["error"] == "empty query"
    # quirk pinned as-is: False (not None) even though nothing was searched
    assert out["available"] is False
    assert out["search_url"] == "https://archive.org/search?" + url_suffix


# --- HathiTrust: _openlibrary_oclcs -------------------------------------------------

# Four Open Library docs: (a) exact match with OCLCs, (b) same title but no
# author and a later year, (c) unrelated (below threshold), (d) exact match
# but carrying no OCLC numbers.
_OL_DOCS = [
    {
        "title": "American medicinal plants",
        "author_name": ["Charles Frederick Millspaugh"],
        "first_publish_year": 1887,
        "oclc": ["1590780", "651411"],
    },
    {
        "title": "American medicinal plants",
        "first_publish_year": 1974,
        "oclc": [1148061],  # non-str OCLCs are str()-coerced
    },
    {
        "title": "Gardening for Ladies",
        "author_name": ["Jane Loudon"],
        "first_publish_year": 1840,
        "oclc": ["999999"],
    },
    {
        "title": "American medicinal plants",
        "author_name": ["Charles Frederick Millspaugh"],
        "first_publish_year": 1887,
    },
]

_HT_BIB = {
    "oclc:1590780": {
        "records": {
            "001480575": {
                "titles": ["American medicinal plants;"],
                "publishDates": ["1887"],
                "recordURL": "https://catalog.hathitrust.org/Record/001480575",
            }
        },
        "items": [
            {
                "fromRecord": "001480575",
                "itemURL": "https://babel.hathitrust.org/cgi/pt?id=uc1.b1",
                "usRightsString": "Full view",
                "enumcron": "v.1",
            },
            {
                "fromRecord": "001480575",
                "itemURL": "https://babel.hathitrust.org/cgi/pt?id=uc1.b2",
                "usRightsString": "Limited (search-only)",
                "enumcron": "v.2",
            },
        ],
    },
    "oclc:651411": {"records": {}, "items": []},
    "oclc:1148061": {
        "records": {
            "009999999": {
                "titles": ["American medicinal plants"],
                "publishDates": ["1974"],
                "recordURL": "https://catalog.hathitrust.org/Record/009999999",
            }
        },
        # the item has NO fromRecord: with exactly one record all items are
        # attached to it anyway (the single-record fallback)
        "items": [
            {
                "itemURL": "https://babel.hathitrust.org/cgi/pt?id=mdp.x1",
                "usRightsString": "Limited (search-only)",
            }
        ],
    },
}


def _ht_respond(ol_docs_by_call=None, bib=None):
    """Dispatch fake: OL search URLs get docs, the Bib URL gets bib.

    ol_docs_by_call lets the first OL call (with author=) answer differently
    from the title-only retry.
    """
    state = {"ol_calls": 0}
    ol_docs_by_call = ol_docs_by_call or [_OL_DOCS]
    bib = _HT_BIB if bib is None else bib

    def respond(url):
        if url.startswith("https://openlibrary.org/search.json"):
            docs = ol_docs_by_call[min(state["ol_calls"], len(ol_docs_by_call) - 1)]
            state["ol_calls"] += 1
            return {"docs": docs}
        return bib

    return respond


def test_openlibrary_oclcs_threshold_and_rank_order(monkeypatch):
    _install_fake_get(monkeypatch, _ht_respond())
    got = scan_search._openlibrary_oclcs(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    # the authorless 1974 doc passes the ACCURACY gate (0.714 >= 0.6) but
    # sorts by its lower corroboration rank; the unrelated and OCLC-less
    # docs are dropped
    assert got == [
        {"rank": 1.0, "oclcs": ["1590780", "651411"]},
        {"rank": 0.5, "oclcs": ["1148061"]},
    ]


# --- search_hathitrust ----------------------------------------------------------

def test_ht_happy_path(monkeypatch):
    calls = _install_fake_get(monkeypatch, _ht_respond())
    out = scan_search.search_hathitrust(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert calls == [
        "https://openlibrary.org/search.json"
        "?title=American+Medicinal+Plants"
        "&fields=title%2Cauthor_name%2Cfirst_publish_year%2Coclc"
        "&limit=10&author=millspaugh",
        "https://catalog.hathitrust.org/api/volumes/brief/json/"
        "oclc:1590780|oclc:651411|oclc:1148061",
    ]
    assert out["oclcs_tried"] == ["1590780", "651411", "1148061"]
    assert out["search_url"] == (
        "https://catalog.hathitrust.org/Search/Home"
        "?lookfor=American+Medicinal+Plants&type=title"
    )

    assert out["matches"] == [
        {
            "title": "American medicinal plants;",
            "year": "1887",
            "record_url": "https://catalog.hathitrust.org/Record/001480575",
            "items": [
                {
                    "url": "https://babel.hathitrust.org/cgi/pt?id=uc1.b1",
                    "rights": "Full view",
                    "volume": "v.1",
                },
                {
                    "url": "https://babel.hathitrust.org/cgi/pt?id=uc1.b2",
                    "rights": "Limited (search-only)",
                    "volume": "v.2",
                },
            ],
            "full_view": True,
            "accuracy": 1.0,
        },
        {
            "title": "American medicinal plants",
            "year": "1974",
            "record_url": "https://catalog.hathitrust.org/Record/009999999",
            # single-record fallback: the fromRecord-less item is attached
            "items": [
                {
                    "url": "https://babel.hathitrust.org/cgi/pt?id=mdp.x1",
                    "rights": "Limited (search-only)",
                    "volume": "",  # missing enumcron -> ""
                }
            ],
            "full_view": False,
            # HT accuracy is computed with author="" (the OCLC already ties
            # identity): (0.5*1 + 0.2*0) / 0.7
            "accuracy": 0.714,
        },
    ]
    assert out["best_match"] is out["matches"][0]
    assert out["available"] is True
    assert out["full_view"] is True


def test_ht_ol_author_query_empty_retries_without_author(monkeypatch):
    calls = _install_fake_get(monkeypatch, _ht_respond(ol_docs_by_call=[[], _OL_DOCS]))
    out = scan_search.search_hathitrust(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert len(calls) == 3  # OL with author, OL title-only retry, Bib
    assert "&author=millspaugh" in calls[0]
    assert "&author=" not in calls[1]  # fields=...author_name... remains
    assert out["oclcs_tried"] == ["1590780", "651411", "1148061"]
    assert out["available"] is True


def test_ht_no_oclcs_found(monkeypatch):
    calls = _install_fake_get(monkeypatch, _ht_respond(ol_docs_by_call=[[], []]))
    out = scan_search.search_hathitrust(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert len(calls) == 2  # both OL queries, but never the Bib API
    assert out["available"] is None
    assert out["note"] == (
        "no OCLC identifier found via Open Library; "
        "use the search link to check by hand"
    )
    assert "oclcs_tried" not in out


def test_ht_oclcs_found_but_ht_holds_nothing(monkeypatch):
    empty_bib = {f"oclc:{o}": {"records": {}, "items": []}
                 for o in ("1590780", "651411", "1148061")}
    _install_fake_get(monkeypatch, _ht_respond(bib=empty_bib))
    out = scan_search.search_hathitrust(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert out["oclcs_tried"] == ["1590780", "651411", "1148061"]
    assert out["matches"] == []
    assert out["best_match"] is None
    assert out["available"] is False  # OCLCs found, HT holds nothing
    assert out["full_view"] is False


def test_ht_openlibrary_error_is_reported(monkeypatch):
    _install_fake_get(monkeypatch, lambda url: OSError("net down"))
    out = scan_search.search_hathitrust(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert out["error"] == "Open Library lookup failed: OSError: net down"
    assert out["available"] is None
    assert "oclcs_tried" not in out


def test_ht_bib_error_is_reported(monkeypatch):
    def respond(url):
        if url.startswith("https://openlibrary.org/"):
            return {"docs": _OL_DOCS}
        return OSError("net down")

    _install_fake_get(monkeypatch, respond)
    out = scan_search.search_hathitrust(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert out["error"] == "Bib API lookup failed: OSError: net down"
    assert out["available"] is None
    assert out["oclcs_tried"] == ["1590780", "651411", "1148061"]


def test_ht_empty_query_short_circuits(monkeypatch):
    calls = _install_fake_get(monkeypatch, _ht_respond())
    out = scan_search.search_hathitrust("")

    assert calls == []
    assert out["error"] == "empty query"
    assert out["available"] is False
    assert out["search_url"] == (
        "https://catalog.hathitrust.org/Search/Home?lookfor=&type=title"
    )


# --- search_scans (composition) ---------------------------------------------------

def test_search_scans_composes_both_sources(monkeypatch):
    _install_fake_get(monkeypatch, lambda url: {})
    out = scan_search.search_scans(QUERY_TITLE, QUERY_AUTHOR, QUERY_YEAR)

    assert out["query_title"] == QUERY_TITLE
    assert out["query_author"] == QUERY_AUTHOR
    assert out["query_year"] == QUERY_YEAR
    assert out["internet_archive"]["available"] is False
    assert out["hathitrust"]["available"] is None
    # a seconds-precision UTC ISO timestamp; the value itself is wall-clock
    stamp = out["checked_at"]
    assert stamp.endswith("+00:00")
    assert datetime.fromisoformat(stamp).utcoffset().total_seconds() == 0


def test_search_scans_none_author_year_echo_as_empty(monkeypatch):
    _install_fake_get(monkeypatch, lambda url: {})
    out = scan_search.search_scans(QUERY_TITLE)

    assert out["query_author"] == ""
    assert out["query_year"] == ""
