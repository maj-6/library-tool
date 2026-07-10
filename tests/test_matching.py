"""Characterization tests for the matching helpers.

Pins the CURRENT behavior of whl_client's normalization/similarity/accuracy
helpers and catalog_checks.title_author_match, exactly as observed by
executing the modules. These are golden tests: several pinned values are
known quirks (boundary-less _year regex, similarity('','') == 1.0,
initials-only authors landing in the "missing author" tier) and are pinned
as-is on purpose — a failure here means the matching behavior CHANGED, not
that it is correct. Everything below is offline and deterministic; the
network-facing search/find_book paths are not touched.

conftest.py sets WHL_DATA_ROOT before collection, which catalog_checks
needs (it imports libcommon, which reads the env var once at import).
"""
from __future__ import annotations

import pytest

import catalog_checks as cc
import whl_client as whl


# --- threshold constants ------------------------------------------------------

def test_threshold_constants_current_values():
    """Pin the tuning constants so a retune is an explicit, visible diff.

    The point is to make retunes deliberate, not to forbid them: if any of
    these change, update this test AND re-derive the golden tables below
    (several title_author_match goldens sit right against these bars).
    """
    assert whl.TITLE_PREFIX == 16
    assert whl.AUTHOR_PREFIX == 8
    assert whl.W_TITLE == 0.5
    assert whl.W_AUTHOR == 0.3
    assert whl.W_DATE == 0.2
    assert whl.MATCH_THRESHOLD == 0.6
    assert cc.TITLE_PREFIX_MIN == 0.72
    assert cc.TITLE_FULL_MIN == 0.62
    assert cc.TITLE_FULL_MISSING == 0.82
    assert cc.TITLE_FULL_STRICT == 0.90


# --- whl_client._normalize ------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("  A Modern HERBAL!  Vol. 1 ", "a modern herbal vol 1"),
        # lowercasing happens first, then non-[a-z0-9] runs become spaces,
        # so accented letters are dropped like punctuation
        ("Café-Botany's Guide", "caf botany s guide"),
        ("Ünïcode Æther", "n code ther"),
        ("---", ""),
        ("", ""),
    ],
)
def test_normalize(text, expected):
    assert whl._normalize(text) == expected


# --- whl_client._year -----------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1931", "1931"),
        ("c1931.", "1931"),
        ("published 1899-1901", "1899"),  # first match wins
        ("2025", "2025"),
        ("0999", None),
        ("", None),
        (None, None),
        (1931, "1931"),  # non-strings pass through str()
        # regex has no word boundaries — pinned as-is
        ("20999", "2099"),
        ("12345", "1234"),
    ],
)
def test_year(value, expected):
    assert whl._year(value) == expected


# --- whl_client.flip_author -----------------------------------------------------

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Grieve, M.", "M. Grieve"),
        ("M. Grieve", "M. Grieve"),  # no comma: unchanged
        (" Culpeper , Nicholas ", "Nicholas Culpeper"),
        ("Linnaeus", "Linnaeus"),
        # credential/suffix tails must not flip
        ("Blair, M. D.", "Blair, M. D."),
        ("Smith, Jr.", "Smith, Jr."),
        ("Smith, PH.D.", "Smith, PH.D."),
        # two commas: unchanged
        ("Doe, Jane, PhD", "Doe, Jane, PhD"),
        # empty side of the comma: unchanged
        (", Nicholas", ", Nicholas"),
        ("Culpeper,", "Culpeper,"),
    ],
)
def test_flip_author(name, expected):
    assert whl.flip_author(name) == expected


# --- whl_client.similarity / similarity_prefix ----------------------------------

@pytest.mark.parametrize(
    ("a", "b", "full", "prefix16"),
    [
        ("A Modern Herbal", "A Modern Herbal", 1.0, 1.0),
        ("A MODERN HERBAL!", "a modern herbal", 1.0, 1.0),
        # subtitle kills the full ratio; the prefix window saves it —
        # prefix16 is not 1.0 because the long side's 16-char slice keeps
        # a trailing space ('a modern herbal ' vs 'a modern herbal')
        (
            "A Modern Herbal: The Medicinal, Culinary, Cosmetic and Economic Properties",
            "A Modern Herbal",
            0.348837,
            0.967742,
        ),
        ("A Modern Herbal", "A Modern Handbook", 0.6875, 0.709677),
        ("Species Plantarum", "The Origin of Species", 0.368421, 0.125),
        ("Culpeper's Complete Herbal", "The Complete Herbal", 0.755556, 0.4375),
        ("An Introduction to Botany", "Introduction to Botany", 0.93617, 0.8125),
        # two empty strings are a perfect match (SequenceMatcher) — pinned as-is
        ("", "", 1.0, 1.0),
        ("A Modern Herbal", "", 0.0, 0.0),
    ],
)
def test_similarity_and_prefix(a, b, full, prefix16):
    assert whl.similarity(a, b) == pytest.approx(full, abs=1e-6)
    assert whl.similarity_prefix(a, b, 16) == pytest.approx(prefix16, abs=1e-6)


def test_author_prefix_needs_flip():
    """The 8-char author compare only lines up after flip_author."""
    assert whl.similarity_prefix("Grieve, M.", "M. Grieve", 8) == pytest.approx(0.75, abs=1e-6)
    assert whl.similarity_prefix(
        whl.flip_author("Grieve, M."), whl.flip_author("M. Grieve"), 8
    ) == pytest.approx(1.0)


# --- whl_client.accuracy ---------------------------------------------------------

_Q = {"title": "A Modern Herbal", "author": "M. Grieve", "date": "1931"}
_M = {"whl_title": "A Modern Herbal", "author": "M. Grieve", "pub_date": "1931"}


@pytest.mark.parametrize(
    ("case", "query", "match", "score", "parts", "meets_threshold"),
    [
        (
            "exact all fields",
            _Q,
            _M,
            1.0,
            {"title": 1.0, "author": 1.0, "date": True},
            True,
        ),
        (
            "case+punct title",
            {**_Q, "title": "A MODERN HERBAL!"},
            {**_M, "whl_title": "a modern herbal"},
            1.0,
            {"title": 1.0, "author": 1.0, "date": True},
            True,
        ),
        (
            "subtitle appended",
            {**_Q, "title": "A Modern Herbal: The Medicinal, Culinary, Cosmetic and Economic Properties"},
            _M,
            0.983871,
            {"title": 0.967742, "author": 1.0, "date": True},
            True,
        ),
        (
            "author flipped",
            {**_Q, "author": "Grieve, M."},
            _M,
            1.0,
            {"title": 1.0, "author": 1.0, "date": True},
            True,
        ),
        (
            # exact 0.8 = (0.5 + 0.3) / 1.0: date component present but 0
            "year off-by-one",
            _Q,
            {**_M, "pub_date": "1932"},
            0.8,
            {"title": 1.0, "author": 1.0, "date": False},
            True,
        ),
        (
            # date missing on one side: renormalized over title+author
            "year missing on match side",
            _Q,
            {**_M, "pub_date": ""},
            1.0,
            {"title": 1.0, "author": 1.0, "date": None},
            True,
        ),
        (
            "author missing on query side",
            {**_Q, "author": ""},
            _M,
            1.0,
            {"title": 1.0, "author": None, "date": True},
            True,
        ),
        (
            "title only, no author/date keys",
            {"title": "A Modern Herbal"},
            {"whl_title": "A Modern Herbal"},
            1.0,
            {"title": 1.0, "author": None, "date": None},
            True,
        ),
        (
            "near-miss below threshold",
            _Q,
            {"whl_title": "A Modern Handbook of Gardening", "author": "J. Smith", "pub_date": "1950"},
            0.434839,
            {"title": 0.709677, "author": 0.266667, "date": False},
            False,
        ),
        (
            "clearly unrelated",
            {"title": "Species Plantarum", "author": "Carl Linnaeus", "date": "1753"},
            {"whl_title": "Moby Dick", "author": "Herman Melville", "pub_date": "1851"},
            0.115,
            {"title": 0.08, "author": 0.25, "date": False},
            False,
        ),
        (
            # _year extracts '1931' from both sides and compares with ==,
            # so these messy strings are an exact date match — pinned as-is
            "messy date strings",
            {**_Q, "date": "c1931."},
            {**_M, "pub_date": "London, 1931-1932"},
            1.0,
            {"title": 1.0, "author": 1.0, "date": True},
            True,
        ),
        (
            # an empty title still contributes its 0.0 at full weight
            "empty query title",
            {**_Q, "title": ""},
            _M,
            0.5,
            {"title": 0.0, "author": 1.0, "date": True},
            False,
        ),
    ],
)
def test_accuracy(case, query, match, score, parts, meets_threshold):
    got_score, got_parts = whl.accuracy(query, match)
    assert got_score == pytest.approx(score, abs=1e-6)
    assert set(got_parts) == {"title", "author", "date"}
    assert got_parts["title"] == pytest.approx(parts["title"], abs=1e-6)
    if parts["author"] is None:
        assert got_parts["author"] is None
    else:
        assert got_parts["author"] == pytest.approx(parts["author"], abs=1e-6)
    assert got_parts["date"] is parts["date"]
    assert (got_score >= whl.MATCH_THRESHOLD) is meets_threshold


# --- catalog_checks.title_author_match -------------------------------------------

@pytest.mark.parametrize(
    ("case", "title", "author", "cand_title", "cand_author", "expected"),
    [
        ("exact", "A Modern Herbal", "M. Grieve", "A Modern Herbal", "M. Grieve", True),
        ("case+punct+flip", "A MODERN HERBAL!", "GRIEVE, M.", "a modern herbal", "M. Grieve", True),
        (
            # unlike accuracy(), the full-title ratio here (0.348837 < 0.62)
            # vetoes a long appended subtitle even with a shared surname
            "subtitle vs plain, shared surname",
            "A Modern Herbal: The Medicinal, Culinary, Cosmetic and Economic Properties",
            "M. Grieve",
            "A Modern Herbal",
            "Grieve, Maud",
            False,
        ),
        ("flipped author", "A Modern Herbal", "Grieve, Maud", "A Modern Herbal", "Maud Grieve", True),
        # missing-author tier: 1.0 >= 0.82
        ("one side no author", "A Modern Herbal", "", "A Modern Herbal", "M. Grieve", True),
        # missing-author tier: full 0.545455 < 0.82
        (
            "no author + subtitle diff",
            "A Modern Herbal: The Medicinal Properties",
            "",
            "A Modern Herbal",
            "M. Grieve",
            False,
        ),
        # strict tier: identical title 1.0 >= 0.90 despite disjoint authors
        (
            "different authors, identical title",
            "A Modern Herbal",
            "Maud Grieve",
            "A Modern Herbal",
            "John Parkinson",
            True,
        ),
        # strict tier: full 0.952381 >= 0.90 (only the volume digit differs) —
        # a retune of TITLE_FULL_STRICT would flip this one
        (
            "different authors, near-identical title",
            "A Modern Herbal Vol 1",
            "Maud Grieve",
            "A Modern Herbal Vol 2",
            "John Parkinson",
            True,
        ),
        # fails step 1: prefix16 0.08 < TITLE_PREFIX_MIN
        ("unrelated (prefix veto)", "Species Plantarum", "Carl Linnaeus", "Moby Dick", "Herman Melville", False),
        # lenient tier but full 0.394737 < 0.62
        (
            "prefix ok, full too low, shared surname",
            "A Modern Herbal",
            "Maud Grieve",
            "A Modern Herbal Encyclopedia of Everything Botanical and More",
            "Grieve, Maud",
            False,
        ),
        # author_tokens drops len<4 tokens, so initials-only authors on BOTH
        # sides land in the MISSING tier (0.82), never the strict one — as-is
        ("initials-only authors both sides", "A Modern Herbal", "M. G.", "A Modern Herbal", "M. G.", True),
    ],
)
def test_title_author_match(case, title, author, cand_title, cand_author, expected):
    assert cc.title_author_match(title, author, cand_title, cand_author) is expected


# One title pair whose full ratio (0.666667) sits between TITLE_FULL_MIN and
# TITLE_FULL_MISSING, so the three author tiers give three different answers.
_TIER_TITLE_A = "A Modern Herbal of England"
_TIER_TITLE_B = "A Modern Herbal of the Scottish Isles"


@pytest.mark.parametrize(
    ("author", "cand_author", "expected"),
    [
        ("Maud Grieve", "Grieve, M.", True),      # shared surname: 0.666667 >= 0.62
        ("Maud Grieve", "John Parkinson", False),  # disjoint: 0.666667 < 0.90
        ("", "John Parkinson", False),             # missing: 0.666667 < 0.82
    ],
)
def test_title_author_match_tier_split(author, cand_author, expected):
    assert whl.similarity(_TIER_TITLE_A, _TIER_TITLE_B) == pytest.approx(0.666667, abs=1e-6)
    assert cc.title_author_match(_TIER_TITLE_A, author, _TIER_TITLE_B, cand_author) is expected


def test_title_author_match_all_tiers_fail_below_lenient():
    """A pair below even TITLE_FULL_MIN fails in every author tier."""
    a, b = "A Modern Herbal and Garden Guide", "A Modern Herbal of the Field"
    assert whl.similarity(a, b) == pytest.approx(0.566667, abs=1e-6)
    assert cc.title_author_match(a, "Maud Grieve", b, "Grieve, M.") is False
    assert cc.title_author_match(a, "Maud Grieve", b, "John Parkinson") is False
    assert cc.title_author_match(a, "", b, "John Parkinson") is False


# --- catalog_checks.author_tokens / last_token ------------------------------------

@pytest.mark.parametrize(
    ("author", "tokens", "last"),
    [
        ("Grieve, Maud", {"grieve", "maud"}, "grieve"),  # flip puts surname last
        ("M. Grieve", {"grieve"}, "grieve"),
        ("Grieve, M.", {"grieve"}, "grieve"),
        # author_tokens strips stopwords; last_token has NO stoplist, so
        # 'sons' survives there — pinned as-is
        ("Edited by John Smith and Sons", {"john", "smith"}, "sons"),
        ("Dr. Anonymous", set(), "anonymous"),
        ("", set(), ""),
    ],
)
def test_author_tokens_and_last_token(author, tokens, last):
    assert cc.author_tokens(author) == tokens
    assert cc.last_token(author) == last
