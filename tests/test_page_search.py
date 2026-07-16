"""Ranked page-text search, the desktop half (issue #139).

_search_normalize must mirror website/assets/textsearch.js exactly -- the
vectors here are the SAME ones tests/textsearch.test.js runs against the
client-side fold -- and _publish_bundle must carry the normalized layer to
volume_pages.search_body, degrading (not failing) on a live project that has
not applied docs/cloud/migrations/002_page_search.sql yet.
"""
from __future__ import annotations

import logging

import pytest
import server


# --- normalization parity ----------------------------------------------------------

# The same vectors as tests/textsearch.test.js: early-modern folding,
# diacritics, line-break hyphenation, whitespace collapse.
VECTORS = [
    ("Phyſick", "physick"),                       # long s
    ("oﬃce ﬁre ﬂoure aﬀection",    # ff/fi/fl/ffi ligatures
     "office fire floure affection"),
    ("beﬅ moﬆ", "best most"),                # st ligatures
    ("Cæſar Œconomy", "caesar oeconomy"),
    ("FLORA RÚSTICA", "flora rustica"),           # diacritics fold
    ("azafrán de los prados", "azafran de los prados"),
    ("phy-\nsick", "physick"),                         # line-break hyphenation
    ("phy- \r\n  sick garden", "physick garden"),
    ("the physick\n\n  garden of London", "the physick garden of london"),
]


@pytest.mark.parametrize("raw,folded", VECTORS)
def test_search_normalize_matches_the_client_fold(raw, folded):
    assert server._search_normalize(raw) == folded


def test_search_normalize_edges():
    assert server._search_normalize("") == ""
    assert server._search_normalize(None) == ""
    assert server._search_normalize("  padded  out  ") == "padded out"
    # a soft hyphen is invisible anywhere; a real mid-word hyphen stays
    assert server._search_normalize("phy\u00adsick") == "physick"
    assert server._search_normalize("well-known") == "well-known"


# --- the publish path --------------------------------------------------------------

CLOUD = {"url": "https://x", "key": "k"}


def _no_deletes(monkeypatch):
    monkeypatch.setattr(server.sbase, "delete_rows",
                        lambda cfg, table, filters: None)


def test_publish_bundle_sends_the_normalized_search_layer(monkeypatch):
    upserts = []

    def upsert(cfg, table, on_conflict, rows, chunk=200):
        upserts.append((table, [dict(r) for r in rows]))
        return len(rows)

    monkeypatch.setattr(server.sbase, "upsert_rows", upsert)
    _no_deletes(monkeypatch)
    art = {"about": "", "notes": [],
           "pages": {"": {1: "A treatise of Phyſick.",
                          2: "phy-\nsick herbs"}}}

    server._publish_bundle(CLOUD, "a-work", art)

    assert [t for t, _ in upserts] == ["volume_pages"]
    rows = upserts[0][1]
    assert [(r["page"], r["body"], r["search_body"]) for r in rows] == [
        (1, "A treatise of Phyſick.", "a treatise of physick."),
        (2, "phy-\nsick herbs", "physick herbs"),
    ]


def test_publish_bundle_degrades_without_the_search_column(monkeypatch, caplog):
    """A live project behind on migrations must still get its page text --
    the same degradation idiom as _publish_run's volumes upsert."""
    upserts = []

    def upsert(cfg, table, on_conflict, rows, chunk=200):
        upserts.append((table, [dict(r) for r in rows]))
        if any("search_body" in r for r in rows):
            raise server.sbase.SyncError(
                "PGRST204: Column 'search_body' of relation 'volume_pages' "
                "does not exist")
        return len(rows)

    monkeypatch.setattr(server.sbase, "upsert_rows", upsert)
    _no_deletes(monkeypatch)
    art = {"about": "", "notes": [], "pages": {"": {1: "Phyſick"}}}

    with caplog.at_level(logging.WARNING, logger="whl"):
        server._publish_bundle(CLOUD, "a-work", art)

    assert len(upserts) == 2                       # failed once, retried bare
    first, second = upserts
    assert "search_body" in first[1][0]
    assert "search_body" not in second[1][0]
    assert second[1][0]["body"] == "Phyſick"  # the verbatim text still lands
    assert "002_page_search" in caplog.text


def test_publish_bundle_reraises_unrelated_sync_errors(monkeypatch):
    def upsert(cfg, table, on_conflict, rows, chunk=200):
        raise server.sbase.SyncError("HTTP 500: something else entirely")

    monkeypatch.setattr(server.sbase, "upsert_rows", upsert)
    _no_deletes(monkeypatch)
    art = {"about": "", "notes": [], "pages": {"": {1: "body"}}}

    with pytest.raises(server.sbase.SyncError):
        server._publish_bundle(CLOUD, "a-work", art)
