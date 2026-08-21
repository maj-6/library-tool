from __future__ import annotations

import contextlib
from copy import deepcopy
from types import SimpleNamespace

import libcommon as lib
import pytest
import server
import supabase_sync


CAPTURE_ID = "2ec86526-1133-4e74-a2c7-497886201d76"
SECOND_CAPTURE_ID = "ed3cb24e-490a-49b1-a066-4e9768bf3f00"
OWNER_CFG = {"url": "https://project.test", "key": "service-secret"}
CAPTURE_CFG = {
    "url": "https://project.test",
    "key": "public-key",
    "access_token": "user-jwt",
}


def test_capture_roundtrip_requires_same_project_user_scope():
    with pytest.raises(ValueError, match="signed-in user"):
        server._capture_roundtrip_configs(OWNER_CFG, {
            "url": OWNER_CFG["url"], "key": "public-key",
        })
    with pytest.raises(ValueError, match="different projects"):
        server._capture_roundtrip_configs(OWNER_CFG, {
            **CAPTURE_CFG, "url": "https://other-project.test",
        })


def test_desktop_snapshot_contains_phone_list_and_popup_fields():
    builds = {
        "book-1": {
            "title": "A Captured Herbal",
            "authors": "A. Author",
            "year": "1928",
            "capture_id": CAPTURE_ID,
            "updated_at": "2026-07-22T12:00:00+00:00",
            "rights": "searchable-only",
            "status": "ready",
            "attention": "Check the edition",
            "notes": "Shelf note",
        },
    }
    source = {
        "capture_id": CAPTURE_ID,
        "title": "A Captured Herbal",
        "author": "A. Author",
        "year": "1928",
        "local_pdf": "scan.pdf",
        "checks": {
            "copyright_status": "In copyright (renewal R123)",
            "in_whl": "yes",
            "whl_match": {"permalink": "https://worldherblibrary.org/book/1"},
        },
        "scans": {
            "internet_archive": {
                "available": True,
                "full_view": True,
                "best_match": {
                    "identifier": "captured-herbal",
                    "url": "https://archive.org/details/captured-herbal",
                },
            },
        },
        "extra": {"remark": "Binding is fragile"},
    }
    reviews = {
        "review-1": {
            "id": "review-1",
            "key": "build:book-1",
            "status": "open",
            "reason": "Verify rights",
        },
    }
    registration_cache = {
        "registration-v2|a captured herbal|a. author|1928|cprs": {
            "result": {
                "found": True,
                "sources": ["cprs"],
                "match": {
                    "source": "cprs",
                    "reg_number": "A123",
                    "title": "A Captured Herbal",
                    "author": "A. Author",
                    "year": "1928",
                    "record_id": "record-1",
                },
            },
        },
        "__renewal__|R123": {
            "id": "R123",
            "registration_date": "24Jun28",
            "renewal_date": "12Jun56",
            "registration_number": "A123",
        },
    }

    rows = server._capture_book_metadata_rows(
        builds=builds,
        manual_entries={"manual-1": source},
        reviews=reviews,
        registration_cache=registration_cache,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["capture_id"] == CAPTURE_ID
    assert row["book_id"] == "book-1"
    data = row["data"]
    assert data["copyright"]["status"] == "Search only"
    assert data["copyright"]["curated_status"] == "Search only"
    assert data["copyright"]["automated_status"] == \
        "In copyright (renewal R123)"
    assert data["copyright"]["rights_label"] == "Search only"
    assert data["copyright"]["registration_records"] == [{
        "number": "A123",
        "date": "1928",
        "source": "cprs",
        "title": "A Captured Herbal",
        "author": "A. Author",
        "record_id": "record-1",
    }]
    assert data["copyright"]["renewal_records"][0]["renewal_id"] == "R123"
    assert data["availability"]["whl"] == {
        "state": "available",
        "url": "https://worldherblibrary.org/book/1",
        "identifier": "",
        "detail": "yes",
    }
    assert data["availability"]["internet_archive"]["state"] == "available"
    assert data["availability"]["internet_archive"]["identifier"] == \
        "captured-herbal"
    assert data["scan_status"] == "approved"
    assert data["remarks"] == ["Shelf note", "Binding is fragile"]
    assert data["bibliography"] == {
        "title": "A Captured Herbal",
        "author": "A. Author",
        "year": "1928",
    }
    assert "review" not in data


def test_snapshot_uses_newest_registered_build_and_skips_non_cloud_ids():
    rows = server._capture_book_metadata_rows(
        builds={
            "old": {"capture_id": CAPTURE_ID, "updated_at": "2026-01-01"},
            "new": {"capture_id": CAPTURE_ID, "updated_at": "2026-02-01"},
            "legacy": {"capture_id": "capture-01", "updated_at": "2026-03-01"},
        },
        manual_entries={},
        reviews={},
        registration_cache={},
    )

    assert [(row["capture_id"], row["book_id"]) for row in rows] == [
        (CAPTURE_ID, "new"),
    ]


def test_digitization_candidate_projects_from_the_selected_active_item_only():
    manual = {
        "capture_id": CAPTURE_ID,
        "digitization_candidate": True,
        "updated_at": "2026-08-09T10:00:00Z",
    }
    unregistered = server._capture_book_metadata_rows(
        builds={}, manual_entries={"manual-1": manual}, reviews={},
        registration_cache={},
    )[0]
    assert unregistered["book_id"] == ""
    assert unregistered["data"]["digitization_candidate"] is True

    # A promoted build is the active book. Its explicit clear wins over the
    # stale originating manual row; the projection must never OR the two.
    cleared = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "digitization_candidate": False,
            "updated_at": "2026-08-09T10:01:00Z",
        }},
        manual_entries={"manual-1": manual}, reviews={},
        registration_cache={},
    )[0]
    assert cleared["data"]["digitization_candidate"] is False

    # Absence remains absence for compatibility with old item records. It is
    # distinct from an explicit false during cross-desktop projection merges.
    legacy = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "updated_at": "2026-08-09T10:01:00Z",
        }},
        manual_entries={"manual-1": manual}, reviews={},
        registration_cache={},
    )[0]
    assert "digitization_candidate" not in legacy["data"]


def test_scan_priority_projects_only_a_strict_candidate_ordinal():
    def projected(priority, *, candidate=True, include=True):
        build = {
            "capture_id": CAPTURE_ID,
            "digitization_candidate": candidate,
            "updated_at": "2026-08-21T10:00:00Z",
        }
        if include:
            build["scan_priority"] = priority
        return server._capture_book_metadata_rows(
            builds={"book-1": build}, manual_entries={}, reviews={},
            registration_cache={},
        )[0]["data"]

    assert projected("1")["scan_priority"] == "1"
    assert projected(5)["scan_priority"] == "5"
    for malformed in (True, 1.0, "01", "0", "6", "n/s", None):
        assert projected(malformed)["scan_priority"] is None

    assert "scan_priority" not in projected("2", candidate=False)
    assert "scan_priority" not in projected(None, include=False)


def test_scan_priority_merge_preserves_unknown_but_honors_an_explicit_clear():
    def row(build):
        return server._capture_book_metadata_rows(
            builds={"book-1": {
                "capture_id": CAPTURE_ID,
                "updated_at": "2026-08-21T10:00:00Z",
                **build,
            }},
            manual_entries={}, reviews={}, registration_cache={},
        )[0]

    known = row({"digitization_candidate": True, "scan_priority": "2"})
    legacy = row({"digitization_candidate": True})
    preserved = server._merge_capture_projection_with_existing(
        legacy, {**known, "revision": 2},
    )
    assert preserved["data"]["scan_priority"] == "2"

    explicit_clear = row({
        "digitization_candidate": True,
        "scan_priority": "n/s",
    })
    cleared = server._merge_capture_projection_with_existing(
        explicit_clear, {**known, "revision": 2},
    )
    assert cleared["data"]["scan_priority"] is None

    tombstone = server._capture_book_metadata_rows(
        builds={}, manual_entries={}, reviews={}, registration_cache={},
        tombstone_capture_ids=[CAPTURE_ID],
        tombstone_updated_at={CAPTURE_ID: "2026-08-21T10:02:00Z"},
    )[0]
    retained = server._merge_capture_projection_with_existing(
        tombstone, {**known, "revision": 2},
    )["data"]["projection_source"]["_retained_desktop_evidence"]
    assert retained["scan_priority"] == "2"


def test_candidate_merge_preserves_legacy_unknown_but_explicit_false_clears():
    known = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "digitization_candidate": True,
            "updated_at": "2026-08-09T10:00:00Z",
        }},
        manual_entries={"manual-1": {
            "capture_id": CAPTURE_ID,
            "updated_at": "2026-08-09T09:00:00Z",
        }},
        reviews={}, registration_cache={},
    )[0]
    legacy_record = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "updated_at": "2026-08-09T10:00:00Z",
        }},
        # Presence of the originating row must not change missing-vs-false
        # semantics for the selected active build.
        manual_entries={"manual-1": {
            "capture_id": CAPTURE_ID,
            "updated_at": "2026-08-09T09:00:00Z",
        }},
        reviews={}, registration_cache={},
    )[0]

    preserved = server._merge_capture_projection_with_existing(
        legacy_record, {**known, "revision": 2},
    )
    assert preserved["data"]["digitization_candidate"] is True

    explicit_clear = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "digitization_candidate": False,
            "updated_at": "2026-08-09T10:01:00Z",
        }},
        manual_entries={}, reviews={}, registration_cache={},
    )[0]
    cleared = server._merge_capture_projection_with_existing(
        explicit_clear, {**known, "revision": 2},
    )
    assert cleared["data"]["digitization_candidate"] is False

    tombstone = server._capture_book_metadata_rows(
        builds={}, manual_entries={}, reviews={}, registration_cache={},
        tombstone_capture_ids=[CAPTURE_ID],
        tombstone_updated_at={CAPTURE_ID: "2026-08-09T10:02:00Z"},
    )[0]
    merged_tombstone = server._merge_capture_projection_with_existing(
        tombstone, {**known, "revision": 2},
    )
    retained = merged_tombstone["data"]["projection_source"][
        "_retained_desktop_evidence"
    ]
    assert retained["digitization_candidate"] is True


def test_bibliography_reads_both_author_spellings_and_prefers_the_build():
    """The phone's box listing has no other source of a title.

    A capture's own ``meta`` carries title/author only when the capturing phone
    held an extraction API key, so this block is it.  The two desktop stores spell
    the author field differently — ``author`` in lib.MANUAL_ENTRY_FIELDS,
    ``authors`` in _BUILD_FIELDS — and both must be read.

    The BUILD wins: for a phone capture the manual row is the machine-generated
    ingest record, while the build is where a curator types.
    """
    build = {"title": "Curated Title", "authors": "B. Builder", "year": "1930"}
    source = {"title": "Ingested Title", "author": "S. Source", "year": "1928"}

    assert server._capture_bibliography(build, source) == {
        "title": "Curated Title", "author": "B. Builder", "year": "1930",
    }
    # A build-only replica still yields a real author despite the plural spelling.
    assert server._capture_bibliography(build, None) == {
        "title": "Curated Title", "author": "B. Builder", "year": "1930",
    }
    # Blank build fields fall through to the manual row rather than winning as "".
    assert server._capture_bibliography(
        {"title": "", "authors": "  ", "year": ""}, source,
    ) == {"title": "Ingested Title", "author": "S. Source", "year": "1928"}
    # Always empty strings, never None, so the phone's strict string parse
    # cannot reject the block.
    assert server._capture_bibliography({}, None) == {
        "title": "", "author": "", "year": "",
    }


def test_bibliography_never_publishes_the_untitled_capture_placeholder():
    """`_ingest_capture_locked` stamps "(untitled capture <8 hex>)" so a capture
    with no extracted title is not lost from the Catalogs table. That is a
    placeholder, not a title: putting it on a phone's shelf listing is worse than
    publishing nothing, because a blank renders as "Untitled book".

    This owner's phones have no extraction API key, so this is the DEFAULT path,
    not an edge case.
    """
    placeholder = "(untitled capture 2ec86526)"
    # The exact shape ingest writes, verified against server.py's own stamp.
    assert placeholder == f"(untitled capture {CAPTURE_ID[:8]})"

    source = {"title": placeholder, "author": "", "year": ""}
    build = {"title": "Materia Medica Americana", "authors": "J. Schoepf",
             "year": "1787"}
    assert server._capture_bibliography(build, source) == {
        "title": "Materia Medica Americana", "author": "J. Schoepf",
        "year": "1787",
    }
    # With nothing curated anywhere the title is BLANK, not the placeholder.
    assert server._capture_bibliography({}, source)["title"] == ""
    assert server._capture_bibliography({"title": placeholder}, None)["title"] == ""
    # A real title that merely looks similar is not suppressed.
    assert server._capture_bibliography(
        {"title": "Untitled capture notes, 1873"}, None,
    )["title"] == "Untitled capture notes, 1873"


def test_unregistered_capture_is_projected_with_an_empty_book_id():
    """A freshly imported crate must list WITH titles on the phone.

    The only capture -> build path in the UI is the BUILD button over
    `approvedSources()`, which needs a located scan or PDF, so a photos-only
    capture would otherwise never get a projection at all.
    """
    source = {"id": "manual-1", "capture_id": CAPTURE_ID,
              "title": "Curated By Hand", "author": "S. Source", "year": "1928",
              "created_at": "2026-07-19T10:00:00+00:00"}
    rows = server._capture_book_metadata_rows(
        builds={}, manual_entries={"manual-1": source},
        reviews={}, registration_cache={},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["capture_id"] == CAPTURE_ID
    assert row["book_id"] == ""            # the phone reads this as unregistered
    assert row["data"]["bibliography"] == {
        "title": "Curated By Hand", "author": "S. Source", "year": "1928",
    }
    # A live row is not a tombstone.
    assert "registered" not in row["data"]
    # A manual row's own stamp must never land in the BUILD clock component, and
    # a catalogue status belongs to a build.
    assert row["data"]["projection_source"]["build_updated_at"] == ""
    assert row["data"]["catalog_status"] == ""


def test_register_then_unregister_keeps_the_projection_clock_moving_forward():
    """Unregistering must reach the cloud.

    If a manual row's stamp were read into `build_updated_at`, unregistering
    would move that component BACKWARDS, the merge would score the row "stale",
    `push_capture_book_metadata` would refuse it, and the phone would show the
    book as registered forever.
    """
    source = {"id": "manual-1", "capture_id": CAPTURE_ID, "title": "Herbal",
              "author": "S. Source", "created_at": "2026-07-19T10:00:00+00:00"}
    build = {"id": "book-1", "capture_id": CAPTURE_ID, "title": "Herbal",
             "status": "ready", "updated_at": "2026-07-20T12:00:00+00:00"}

    def rows_for(builds):
        return server._capture_book_metadata_rows(
            builds=builds, manual_entries={"manual-1": source},
            reviews={}, registration_cache={},
        )[0]

    unregistered = rows_for({})
    registered = rows_for({"book-1": build})
    assert registered["book_id"] == "book-1"
    assert registered["data"]["catalog_status"] == "ready"

    # (a) live row -> (b) registered
    merged_registered = server._merge_capture_projection_with_existing(
        registered,
        {**unregistered, "revision": 1, "updated_at": "2026-07-19T10:00:01Z"},
    )
    assert supabase_sync._projection_freshness(
        merged_registered["data"], unregistered["data"]) == "newer"

    # (b) registered -> (c) unregistered again
    merged_unregistered = server._merge_capture_projection_with_existing(
        rows_for({}),
        {**merged_registered, "revision": 2,
         "updated_at": "2026-07-20T12:00:01Z"},
    )
    assert merged_unregistered["book_id"] == ""
    assert supabase_sync._projection_freshness(
        merged_unregistered["data"], merged_registered["data"]) == "newer"
    # The build component is carried forward, never regressed (stamps are
    # canonicalized to microsecond precision).
    assert merged_unregistered["data"]["projection_source"][
        "build_updated_at"] == registered["data"]["projection_source"][
        "build_updated_at"] != ""

    # (d) the entry is deleted entirely -> tombstone, still moving forward
    tombstone = server._capture_book_metadata_rows(
        builds={}, manual_entries={}, reviews={}, registration_cache={},
        tombstone_capture_ids=[CAPTURE_ID],
        tombstone_updated_at={CAPTURE_ID: "2026-07-21T12:00:00+00:00"},
    )[0]
    merged_tombstone = server._merge_capture_projection_with_existing(
        tombstone,
        {**merged_unregistered, "revision": 3,
         "updated_at": "2026-07-20T12:00:02Z"},
    )
    assert merged_tombstone["data"]["registered"] is False
    assert supabase_sync._projection_freshness(
        merged_tombstone["data"], merged_unregistered["data"]) == "newer"


def test_a_live_unregistered_row_never_carries_the_retained_envelope():
    """The envelope is a tombstone handoff. Writing it onto a LIVE unregistered
    row would make it re-snapshot from itself, freeze, and later feed stale facts
    to a replica that re-registers the capture."""
    source = {"id": "manual-1", "capture_id": CAPTURE_ID, "title": "Herbal",
              "created_at": "2026-07-19T10:00:00+00:00",
              "checks": {"copyright_status": "NEW status"}}
    live = server._capture_book_metadata_rows(
        builds={}, manual_entries={"manual-1": source},
        reviews={}, registration_cache={},
    )[0]
    older_cloud = server._capture_book_metadata_rows(
        builds={}, manual_entries={"manual-1": {
            **source, "checks": {"copyright_status": "OLD status"}}},
        reviews={}, registration_cache={},
    )[0]

    merged = server._merge_capture_projection_with_existing(
        live, {**older_cloud, "revision": 1, "updated_at": "2026-07-19T10:00:01Z"},
    )
    assert "_retained_desktop_evidence" not in merged["data"]["projection_source"]
    assert merged["data"]["copyright"]["automated_status"] == "NEW status"


def test_manual_entry_edits_advance_the_projection_clock(tmp_path, monkeypatch):
    """Without a monotonic `updated_at` on the manual store, correcting a title
    changes `data` while every clock component stays equal, and
    `push_capture_book_metadata` refuses the row as "equal projection source" —
    so the correction can never reach the phone."""
    manual_path = tmp_path / "manual_entries.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    entry = {"id": "manual-1", "capture_id": CAPTURE_ID, "title": "Wrong Title",
             "created_at": "2026-07-19T10:00:00+00:00"}
    server._save_manual_entries({"manual-1": dict(entry)})
    first = lib.load_json(manual_path, {})["manual-1"]
    assert first["updated_at"]

    before = server._capture_book_metadata_rows(
        builds={}, manual_entries={"manual-1": first},
        reviews={}, registration_cache={},
    )[0]

    server._save_manual_entries(
        {"manual-1": {**first, "title": "Corrected Title"}})
    second = lib.load_json(manual_path, {})["manual-1"]
    assert second["updated_at"] > first["updated_at"]

    after = server._capture_book_metadata_rows(
        builds={}, manual_entries={"manual-1": second},
        reviews={}, registration_cache={},
    )[0]
    assert after["data"]["bibliography"]["title"] == "Corrected Title"
    assert supabase_sync._projection_freshness(
        after["data"], before["data"]) == "newer"

    # An unchanged save must NOT churn the token, or every sync would republish.
    server._save_manual_entries({"manual-1": dict(second)})
    assert lib.load_json(manual_path, {})["manual-1"]["updated_at"] == \
        second["updated_at"]


def test_bibliography_survives_an_unregister_reregister_cycle():
    """A tombstone carries the block internally, so a replica that re-registers
    the capture does not blank author/year permanently — the owning desktop's
    replay would compare "equal" and be refused."""
    build = {"id": "book-1", "capture_id": CAPTURE_ID, "title": "Herbal",
             "updated_at": "2026-07-20T12:00:00Z"}
    source = {"id": "manual-1", "capture_id": CAPTURE_ID, "title": "Herbal",
              "author": "S. Source", "year": "1928"}
    rich = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={"manual-1": source},
        reviews={}, registration_cache={},
    )[0]
    assert rich["data"]["bibliography"]["author"] == "S. Source"

    tombstone = server._capture_book_metadata_rows(
        builds={}, manual_entries={}, reviews={}, registration_cache={},
        tombstone_capture_ids=[CAPTURE_ID],
        tombstone_updated_at={CAPTURE_ID: "2026-07-23T12:00:00+00:00"},
    )[0]
    merged_tombstone = server._merge_capture_projection_with_existing(
        tombstone, {**rich, "revision": 3, "updated_at": "2026-07-22T12:00:00Z"},
    )
    # Carried in the underscored desktop envelope, NOT in the phone-visible data.
    assert "bibliography" not in merged_tombstone["data"]
    retained = merged_tombstone["data"]["projection_source"][
        "_retained_desktop_evidence"]
    assert retained["bibliography"]["author"] == "S. Source"

    # A replica re-registers: it has the build but never had the manual row.
    replica = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={},
        reviews={}, registration_cache={},
    )[0]
    restored = server._merge_capture_projection_with_existing(
        replica,
        {**merged_tombstone, "revision": 4,
         "updated_at": "2026-07-23T12:01:00Z"},
    )
    assert restored["data"]["bibliography"] == {
        "title": "Herbal", "author": "S. Source", "year": "1928",
    }


def test_bibliography_clamps_match_the_phones_parser():
    long_title = "T" * 900
    biblio = server._capture_bibliography(
        {}, {"title": long_title, "author": "A" * 900, "year": "Y" * 90},
    )
    # DESKTOP_BIBLIOGRAPHY_FIELD_MAX / _YEAR_MAX in CaptureMetadata.kt reject a
    # longer value outright, so an over-long field must be clamped here.
    assert len(biblio["title"]) == 500
    assert len(biblio["author"]) == 500
    assert len(biblio["year"]) == 40


def test_build_only_replica_never_blanks_a_richer_cloud_bibliography():
    build = {"id": "book-1", "capture_id": CAPTURE_ID, "title": "Herbal",
             "updated_at": "2026-07-20T12:00:00Z"}
    source = {"id": "manual-1", "capture_id": CAPTURE_ID, "title": "Herbal",
              "author": "S. Source", "year": "1928",
              "updated_at": "2026-07-21T12:00:00Z"}
    rich = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={"manual-1": source},
        reviews={}, registration_cache={},
    )[0]
    assert rich["data"]["bibliography"]["author"] == "S. Source"

    # The replica has the build but not the manual row, so its own block has no
    # author/year. Publishing it must not erase what the owning desktop wrote.
    build_only = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={},
        reviews={}, registration_cache={},
    )[0]
    assert build_only["data"]["bibliography"]["author"] == ""

    merged = server._merge_capture_projection_with_existing(
        build_only,
        {**rich, "revision": 4, "updated_at": "2026-07-21T12:01:00Z"},
    )
    assert merged["data"]["bibliography"] == {
        "title": "Herbal", "author": "S. Source", "year": "1928",
    }


def test_build_only_replica_may_still_advance_a_bibliography_field():
    build = {"id": "book-1", "capture_id": CAPTURE_ID, "title": "Corrected Title",
             "updated_at": "2026-07-22T12:00:00Z"}
    stale = server._capture_book_metadata_rows(
        builds={"book-1": {**build, "title": "Old Title"}},
        manual_entries={"manual-1": {
            "id": "manual-1", "capture_id": CAPTURE_ID, "title": "Old Title",
            "author": "S. Source",
        }},
        reviews={}, registration_cache={},
    )[0]
    build_only = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={},
        reviews={}, registration_cache={},
    )[0]

    merged = server._merge_capture_projection_with_existing(
        build_only, {**stale, "revision": 2, "updated_at": "2026-07-22T12:01:00Z"},
    )
    # Retention is per field and only fills BLANKS: a non-empty newer title wins,
    # while the author it cannot see is carried forward.
    assert merged["data"]["bibliography"]["title"] == "Corrected Title"
    assert merged["data"]["bibliography"]["author"] == "S. Source"


def test_build_only_second_desktop_preserves_source_rich_cloud_projection():
    build = {
        "id": "book-1", "capture_id": CAPTURE_ID,
        "title": "Herbal", "rights": "searchable-only",
        "updated_at": "2026-07-20T12:00:00Z", "notes": "Build note",
    }
    source = {
        "id": "manual-1", "capture_id": CAPTURE_ID,
        "title": "Herbal", "updated_at": "2026-07-21T12:00:00Z",
        "checks": {
            "copyright_status": "In copyright",
            "in_whl": "yes",
            "whl_match": {"permalink": "https://worldherblibrary.org/herbal"},
        },
        "scans": {"internet_archive": {
            "available": True,
            "best_match": {"identifier": "herbal-1"},
        }},
        "extra": {"remark": "Source-only remark"},
    }
    rich = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={"manual-1": source},
        reviews={}, registration_cache={},
    )[0]
    build_only = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={},
        reviews={}, registration_cache={},
    )[0]

    merged = server._merge_capture_projection_with_existing(
        build_only,
        {**rich, "revision": 4, "updated_at": "2026-07-21T12:01:00Z"},
    )

    data = merged["data"]
    assert data["copyright"]["automated_status"] == "In copyright"
    assert data["availability"]["whl"]["state"] == "available"
    assert data["availability"]["internet_archive"]["identifier"] == "herbal-1"
    assert "Source-only remark" in data["remarks"]
    assert data["projection_source"]["manual_updated_at"] == \
        rich["data"]["projection_source"]["manual_updated_at"]
    assert data["projection_source"]["manual_present"] is True


def test_tombstone_build_only_reregistration_and_source_retry_are_monotonic():
    build = {
        "id": "book-1", "capture_id": CAPTURE_ID, "title": "Herbal",
        "author": "A. Author", "year": "1928",
        "updated_at": "2026-07-20T12:00:00Z",
    }
    source = {
        "id": "manual-1", "capture_id": CAPTURE_ID, "title": "Herbal",
        "author": "A. Author", "year": "1928",
        "updated_at": "2026-07-21T12:00:00Z",
        "checks": {"copyright_status": "In copyright", "in_whl": "yes"},
        "extra": {"remark": "Source evidence"},
    }
    first_cache = {
        "registration-v2|herbal|a. author|1928|cprs": {
            "cached_at": "2026-07-21T13:00:00Z",
            "result": {"found": True, "sources": ["cprs"],
                       "match": {"reg_number": "A1"}},
        },
    }
    rich = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={"manual-1": source},
        reviews={}, registration_cache=first_cache,
    )[0]
    tombstone_raw = server._capture_book_metadata_rows(
        builds={}, manual_entries={}, reviews={}, registration_cache={},
        tombstone_capture_ids=[CAPTURE_ID],
        tombstone_updated_at={CAPTURE_ID: "2026-07-22T12:00:00Z"},
    )[0]
    tombstone = server._merge_capture_projection_with_existing(
        tombstone_raw, {**rich, "revision": 1})

    assert tombstone["data"]["registered"] is False
    assert "copyright" not in tombstone["data"]
    retained = tombstone["data"]["projection_source"][
        "_retained_desktop_evidence"]
    assert retained["copyright"]["registration_records"][0]["number"] == "A1"
    assert supabase_sync._projection_freshness(
        tombstone["data"], rich["data"]) == "newer"
    repeated_tombstone = server._merge_capture_projection_with_existing(
        tombstone_raw, {**tombstone, "revision": 2})
    assert repeated_tombstone["data"] == tombstone["data"]

    build_only_raw = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={}, reviews={},
        registration_cache={},
    )[0]
    build_only = server._merge_capture_projection_with_existing(
        build_only_raw, {**tombstone, "revision": 2})
    assert build_only["data"]["copyright"]["registration_records"][0][
        "number"] == "A1"
    assert "_retained_desktop_evidence" not in \
        build_only["data"]["projection_source"]
    assert supabase_sync._projection_freshness(
        build_only["data"], tombstone["data"]) == "newer"
    repeated_build_only = server._merge_capture_projection_with_existing(
        build_only_raw, {**build_only, "revision": 3})
    assert repeated_build_only["data"] == build_only["data"]

    source_replay_raw = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={"manual-1": source},
        reviews={}, registration_cache=first_cache,
    )[0]
    source_replay = server._merge_capture_projection_with_existing(
        source_replay_raw, {**build_only, "revision": 3})
    assert source_replay["data"] == build_only["data"]
    assert supabase_sync._projection_freshness(
        source_replay["data"], build_only["data"]) == "equal"

    enriched_cache = deepcopy(first_cache)
    enriched_cache["registration-v2|herbal|a. author|1928|cce"] = {
        "cached_at": "2026-07-23T12:00:00Z",
        "result": {"found": True, "sources": ["cce"],
                   "match": {"reg_number": "A2"}},
    }
    source_retry_raw = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={"manual-1": source},
        reviews={}, registration_cache=enriched_cache,
    )[0]
    source_retry = server._merge_capture_projection_with_existing(
        source_retry_raw, {**source_replay, "revision": 3})
    assert [record["number"] for record in
            source_retry["data"]["copyright"]["registration_records"]] == [
        "A1", "A2",
    ]
    assert supabase_sync._projection_freshness(
        source_retry["data"], build_only["data"]) == "newer"


def test_registration_cache_enrichment_advances_without_book_edit():
    build = {
        "capture_id": CAPTURE_ID, "title": "Herbal", "author": "A. Author",
        "year": "1928", "updated_at": "2026-07-20T12:00:00Z",
    }
    before = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={}, reviews={},
        registration_cache={},
    )[0]
    after = server._capture_book_metadata_rows(
        builds={"book-1": build}, manual_entries={}, reviews={},
        registration_cache={
            "registration-v2|herbal|a. author|1928|cprs": {
                "cached_at": "2026-07-21T12:00:00Z",
                "result": {"found": True, "sources": ["cprs"],
                           "match": {"reg_number": "A1"}},
            },
        },
    )[0]

    assert before["data"]["projection_source"]["build_updated_at"] == \
        after["data"]["projection_source"]["build_updated_at"]
    assert after["data"]["projection_source"]["evidence_updated_at"]
    assert supabase_sync._projection_freshness(
        after["data"], before["data"]) == "newer"


def test_older_distinct_cache_evidence_unions_once_with_monotonic_clock():
    build = {
        "capture_id": CAPTURE_ID, "title": "Herbal", "author": "A. Author",
        "year": "1928", "updated_at": "2026-07-20T12:00:00Z",
    }

    def projection(number: str, source: str, cached_at: str):
        return server._capture_book_metadata_rows(
            builds={"book-1": build}, manual_entries={}, reviews={},
            registration_cache={
                f"registration-v2|herbal|a. author|1928|{source}": {
                    "cached_at": cached_at,
                    "result": {
                        "found": True, "sources": [source],
                        "match": {"reg_number": number},
                    },
                },
            },
        )[0]

    cloud = projection("A1", "cprs", "2026-07-22T12:00:00Z")
    older_desktop = projection("A2", "cce", "2026-07-21T12:00:00Z")
    merged = server._merge_capture_projection_with_existing(
        older_desktop, {**cloud, "revision": 4})

    assert [record["number"] for record in
            merged["data"]["copyright"]["registration_records"]] == [
        "A1", "A2",
    ]
    assert supabase_sync._projection_freshness(
        merged["data"], cloud["data"]) == "newer"

    replay = server._merge_capture_projection_with_existing(
        older_desktop, {**merged, "revision": 5})
    assert replay["data"] == merged["data"]
    assert supabase_sync._projection_freshness(
        replay["data"], merged["data"]) == "equal"


def test_scan_status_treats_a_rejected_whl_match_as_absent():
    assert server._capture_scan_status({
        "checks": {
            "in_whl": "yes",
            "copyright_status": "Public domain (no renewal found)",
        },
        "verify": {"whl": "rejected"},
        "scans": {
            "internet_archive": {"available": False},
            "hathitrust": {"available": False},
        },
    }) == "scan"


def test_availability_respects_desktop_verification_and_manual_override():
    rejected = {
        "checks": {"in_whl": "yes"},
        "verify": {"whl": "rejected", "internet_archive": "rejected"},
        "scans": {"internet_archive": {"available": True}},
    }
    assert server._capture_source_availability(rejected, "whl")["state"] == \
        "unavailable"
    assert server._capture_source_availability(
        rejected, "internet_archive",
    )["state"] == "unavailable"

    manual = deepcopy(rejected)
    manual["manual_urls"] = {
        "whl": "https://worldherblibrary.org/manual",
        "internet_archive": "https://archive.org/details/manual",
    }
    assert server._capture_source_availability(manual, "whl")["state"] == \
        "available"
    assert server._capture_source_availability(
        manual, "internet_archive",
    )["url"] == "https://archive.org/details/manual"


def test_whl_draft_is_not_confirmed_available():
    availability = server._capture_source_availability({
        "checks": {
            "in_whl": "draft",
            "whl_match": {"permalink": "https://worldherblibrary.org/draft"},
        },
    }, "whl")

    assert availability == {
        "state": "unknown",
        "url": "https://worldherblibrary.org/draft",
        "identifier": "",
        "detail": "draft",
    }


def test_registration_projection_ignores_malformed_cache_shapes():
    rows = server._capture_book_metadata_rows(
        builds={"book-1": {
            "capture_id": CAPTURE_ID,
            "title": "Herbal",
            "updated_at": "2026-01-01",
        }},
        manual_entries={},
        reviews={},
        registration_cache={
            "registration-v2|herbal|||bad-list": {"result": ["not", "an", "object"]},
            "registration-v2|herbal|||bad-sources": {
                "result": {
                    "found": True,
                    "sources": [{"not": "scalar"}, "usable"],
                    "match": {"reg_number": "A1"},
                },
            },
        },
    )

    assert rows[0]["data"]["copyright"]["registration_records"] == [{
        "number": "A1", "date": "", "source": "usable", "title": "",
        "author": "", "record_id": "",
    }]


def test_snapshot_emits_unregister_tombstone():
    rows = server._capture_book_metadata_rows(
        builds={}, manual_entries={}, reviews={}, registration_cache={},
        tombstone_capture_ids=[CAPTURE_ID, "not-a-uuid"],
    )

    assert rows == [{
        "capture_id": CAPTURE_ID,
        "book_id": "",
        "data": {
            "schema": "org.whl.capture.desktop-book-metadata",
            "version": 1,
            "registered": False,
            "source_updated_at": "",
                "projection_source": {
                    "build_updated_at": "",
                    "manual_updated_at": "",
                    "evidence_updated_at": "",
                    "registration_updated_at": "",
                    "tombstone_updated_at": "",
                "manual_present": False,
            },
        },
    }]


def test_capture_snapshot_transport_is_scoped_and_omits_unchanged_rows(monkeypatch):
    rows = [
        {"capture_id": CAPTURE_ID, "book_id": "book-1", "data": {"status": 1}},
        {
            "capture_id": SECOND_CAPTURE_ID,
            "book_id": "book-2",
            "data": {"status": 2},
        },
        {"capture_id": "legacy-id", "book_id": "bad", "data": {}},
    ]
    original = deepcopy(rows)
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, deepcopy(payload), prefer))
        if method == "GET":
            return [{
                "capture_id": CAPTURE_ID,
                "book_id": "book-1",
                "data": {"status": 1},
                "revision": 4,
                "updated_at": "2026-07-22T12:00:00Z",
            }]
        return [{
            **payload[0],
            "revision": 1,
            "updated_at": "2026-07-22T12:01:00Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(supabase_sync.SyncError, match="1 capture metadata row"):
        supabase_sync.push_capture_book_metadata({"url": "test"}, rows)
    assert rows == original
    get = calls[0]
    assert get[1] == "GET"
    assert "capture_id=in.(" in get[2]
    assert CAPTURE_ID in get[2]
    assert SECOND_CAPTURE_ID in get[2]
    assert "legacy-id" not in get[2]
    post = calls[1]
    assert post[1:] == (
        "POST",
        "capture_book_metadata?on_conflict=capture_id&select="
        "capture_id,book_id,data,revision,updated_at",
        [{
            "capture_id": SECOND_CAPTURE_ID,
            "book_id": "book-2",
            "data": {"status": 2},
        }],
        "resolution=ignore-duplicates,return=representation",
    )


def test_capture_snapshot_transport_rejects_oversized_data_without_http(monkeypatch):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid rows must not reach PostgREST"),
        ),
    )

    with pytest.raises(supabase_sync.SyncError, match="256 KiB"):
        supabase_sync.push_capture_book_metadata(
            {"url": "test"},
            [{
                "capture_id": CAPTURE_ID,
                "book_id": "book-1",
                "data": {"large": "x" * (257 * 1024)},
            }],
        )


def test_snapshot_batch_failure_retries_rows_individually(monkeypatch):
    rows = [
        {"capture_id": CAPTURE_ID, "book_id": "good", "data": {"ok": True}},
        {"capture_id": SECOND_CAPTURE_ID, "book_id": "bad", "data": {"ok": False}},
    ]
    posts = []

    def rest(_cfg, method, _path, payload=None, prefer=""):
        del prefer
        if method == "GET":
            return []
        posts.append(deepcopy(payload))
        if payload[0]["book_id"] == "bad":
            raise supabase_sync.SyncError("rejected")
        return [{
            **payload[0], "revision": 1,
            "updated_at": "2026-07-22T12:00:00Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    with pytest.raises(supabase_sync.SyncError, match="1 capture metadata row"):
        supabase_sync.push_capture_book_metadata({"url": "test"}, rows, chunk=100)
    assert posts == [[rows[0]], [rows[1]]]


def test_capture_snapshot_update_uses_revision_cas(monkeypatch):
    old_data = {
        "projection_source": {
            "build_updated_at": "2026-01-01T00:00:00+00:00",
            "manual_updated_at": "",
            "tombstone_updated_at": "",
        },
        "status": "old",
    }
    new_data = deepcopy(old_data)
    new_data["projection_source"]["build_updated_at"] = \
        "2026-02-01T00:00:00+00:00"
    new_data["status"] = "new"
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        if method == "GET":
            return [{
                "capture_id": CAPTURE_ID, "book_id": "book-1",
                "data": old_data, "revision": 7,
                "updated_at": "2026-01-01T00:00:00Z",
            }]
        return [{
            "capture_id": CAPTURE_ID, "book_id": "book-1",
            "data": new_data, "revision": 8,
            "updated_at": "2026-02-01T00:00:00Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)

    assert supabase_sync.push_capture_book_metadata({"url": "test"}, [{
        "capture_id": CAPTURE_ID, "book_id": "book-1", "data": new_data,
    }]) == 1
    assert calls[1][0] == "PATCH"
    assert f"capture_id=eq.{CAPTURE_ID}" in calls[1][1]
    assert "revision=eq.7" in calls[1][1]
    assert calls[1][2] == {"book_id": "book-1", "data": new_data}


def test_capture_snapshot_rejects_stale_source_even_after_reread(monkeypatch):
    newer = {
        "projection_source": {
            "build_updated_at": "2026-02-01T00:00:00+00:00",
            "manual_updated_at": "2026-02-02T00:00:00+00:00",
            "tombstone_updated_at": "",
        },
        "status": "newer",
    }
    stale = deepcopy(newer)
    stale["projection_source"]["manual_updated_at"] = \
        "2026-01-01T00:00:00+00:00"
    stale["status"] = "stale"
    calls = []
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda _cfg, method, _path, payload=None, prefer="":
            calls.append((method, payload, prefer)) or [{
                "capture_id": CAPTURE_ID, "book_id": "book-1",
                "data": newer, "revision": 8,
                "updated_at": "2026-02-02T00:00:00Z",
            }],
    )

    with pytest.raises(supabase_sync.SyncError, match="stale projection"):
        supabase_sync.push_capture_book_metadata({"url": "test"}, [{
            "capture_id": CAPTURE_ID, "book_id": "book-1", "data": stale,
        }])
    assert [method for method, _payload, _prefer in calls] == ["GET"]


def test_capture_snapshot_reports_cas_miss(monkeypatch):
    old = {"status": "old"}
    calls = []

    def rest(_cfg, method, _path, payload=None, prefer=""):
        calls.append(method)
        if method == "GET":
            return [{
                "capture_id": CAPTURE_ID, "book_id": "book-1",
                "data": old, "revision": 3,
                "updated_at": "2026-01-01T00:00:00Z",
            }]
        return []

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="compare-and-set"):
        supabase_sync.push_capture_book_metadata({"url": "test"}, [{
            "capture_id": CAPTURE_ID, "book_id": "book-1",
            "data": {"status": "new"},
        }])
    assert calls == ["GET", "PATCH"]


def test_capture_review_transport_is_explicitly_scoped_and_cas_guarded(monkeypatch):
    calls = []

    def rest(_cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, deepcopy(payload), prefer))
        if method == "GET":
            return [{
                "capture_id": CAPTURE_ID,
                "needs_attention": True,
                "attention_reason": "Check",
                "needs_review": False,
                "review_id": "",
                "status": "",
                "revision": 3,
                "updated_at": "2026-07-22T12:00:00Z",
            }]
        return [{
            **payload,
            "revision": 4,
            "updated_at": "2026-07-22T12:01:00Z",
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    listed = supabase_sync.list_capture_reviews(
        {"url": "test"}, [CAPTURE_ID, "not-a-uuid"])
    accepted = supabase_sync.write_capture_review(
        {"url": "test"}, {
            "capture_id": CAPTURE_ID,
            "needs_attention": True,
            "attention_reason": "Check",
            "needs_review": False,
            "review_id": "review-1",
            "status": "open",
        }, 3)

    assert listed[0]["capture_id"] == CAPTURE_ID
    assert "capture_id=in.(" in calls[0][1]
    assert "not-a-uuid" not in calls[0][1]
    assert calls[1][0] == "PATCH"
    assert f"capture_id=eq.{CAPTURE_ID}" in calls[1][1]
    assert "revision=eq.3" in calls[1][1]
    assert accepted["revision"] == 4


@pytest.mark.parametrize("mutation", [
    {"attention_reason": "different"},
    {"revision": 3},
])
def test_capture_review_transport_rejects_unconfirmed_cas(monkeypatch, mutation):
    desired = {
        "capture_id": CAPTURE_ID,
        "needs_attention": True,
        "attention_reason": "Check",
        "needs_review": False,
        "review_id": "review-1",
        "status": "open",
    }
    response = {
        **desired,
        "revision": 4,
        "updated_at": "2026-07-22T12:01:00Z",
        **mutation,
    }
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: [response],
    )

    with pytest.raises(supabase_sync.SyncError):
        supabase_sync.write_capture_review({"url": "test"}, desired, 3)


def test_capture_review_transport_requires_one_fresh_insert_revision(monkeypatch):
    desired = {
        "capture_id": CAPTURE_ID,
        "needs_attention": True,
        "attention_reason": "Check",
        "needs_review": False,
        "review_id": "",
        "status": "",
    }
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: [{
            **desired,
            "revision": 2,
            "updated_at": "2026-07-22T12:01:00Z",
        }],
    )

    with pytest.raises(supabase_sync.SyncError, match="advance"):
        supabase_sync.write_capture_review({"url": "test"}, desired, None)


def test_phone_review_merge_is_additive_idempotent_and_preserves_thread(
        monkeypatch, data_root):
    root = data_root / "capture-review-additive"
    root.mkdir(exist_ok=True)
    manual_path = root / "manual.json"
    reviews_path = root / "reviews.json"
    state_path = root / "capture-sync.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    lib.save_json(manual_path, {"manual-1": {
        "id": "manual-1", "title": "Herbal", "capture_id": CAPTURE_ID,
        "attention": "Desktop reason",
    }})
    existing = {
        "id": "review-1", "key": "row:manual-1", "kind": "row",
        "ref": "manual-1", "label": "Original label",
        "reason": "Desktop review reason", "status": "open",
        "comments": [{"text": "keep me"}],
    }
    lib.save_json(reviews_path, {"review-1": existing})
    target = server._current_capture_targets()[CAPTURE_ID]
    incoming = {
        "capture_id": CAPTURE_ID, "revision": 2, "updated_at": "now",
        "needs_attention": True, "attention_reason": "Phone reason",
        "needs_review": True, "review_id": "", "status": "",
    }

    first = server._merge_phone_review_into_target(target, incoming)
    second = server._merge_phone_review_into_target(target, incoming)
    server._merge_phone_review_into_target(target, {
        **incoming, "revision": 3, "needs_attention": False,
        "attention_reason": "", "needs_review": False,
    })

    assert first["attention_reason"] == \
        "Desktop: Desktop reason\nPhone: Phone reason"
    assert second["review_id"] == "review-1"
    assert lib.load_json(manual_path, {})["manual-1"]["attention"] == \
        "Desktop: Desktop reason\nPhone: Phone reason"
    saved_reviews = lib.load_json(reviews_path, {})
    assert list(saved_reviews) == ["review-1"]
    assert saved_reviews["review-1"] == existing


def test_phone_review_merge_bounds_long_reasons_without_blocking_sync(
        monkeypatch, data_root):
    root = data_root / "capture-review-long-reasons"
    root.mkdir(exist_ok=True)
    manual_path = root / "manual.json"
    reviews_path = root / "reviews.json"
    state_path = root / "capture-sync.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    desktop_reason = "D" * 1000
    phone_reason = "P" * 1000
    lib.save_json(manual_path, {"manual-1": {
        "id": "manual-1", "title": "Herbal", "capture_id": CAPTURE_ID,
        "attention": desktop_reason,
    }})
    cloud = {
        "capture_id": CAPTURE_ID, "revision": 1,
        "updated_at": "2026-07-22T12:00:00Z",
        "needs_attention": True, "attention_reason": phone_reason,
        "needs_review": False, "review_id": "", "status": "",
    }
    writes = []
    monkeypatch.setattr(supabase_sync, "list_capture_ids",
                        lambda _cfg, _ids: [CAPTURE_ID])
    monkeypatch.setattr(supabase_sync, "list_capture_reviews",
                        lambda _cfg, _ids: [deepcopy(cloud)])

    def write(_cfg, desired, expected):
        writes.append((deepcopy(desired), expected))
        return {
            **desired, "revision": 2,
            "updated_at": "2026-07-22T12:01:00Z",
        }

    monkeypatch.setattr(supabase_sync, "write_capture_review", write)

    result = server._sync_capture_reviews(OWNER_CFG, CAPTURE_CFG)

    assert result["errors"] == []
    assert result["merged"] == 1
    assert result["pushed"] == 1
    reason = writes[0][0]["attention_reason"]
    assert len(reason) == 1000
    assert reason.startswith("Desktop: D")
    assert "…\nPhone: P" in reason
    assert reason.endswith("…")
    assert lib.load_json(manual_path, {})["manual-1"]["attention"] == reason
    assert server._combine_review_reasons(reason, phone_reason) == reason


def test_build_attention_cas_race_never_clobbers_newer_desktop_reason(monkeypatch):
    views = iter([
        SimpleNamespace(
            metadata={"attention": ""}, record_revision="r1", title="Herbal"),
        SimpleNamespace(
            metadata={"attention": "newer desktop"},
            record_revision="r2", title="Herbal"),
    ])
    query = SimpleNamespace(get_item=lambda _item_id: next(views))
    calls = []

    def update(command):
        calls.append(command)
        if len(calls) == 1:
            raise server.EngineConflictError("race")

    monkeypatch.setattr(server, "_item_engine", lambda: query)
    monkeypatch.setattr(
        server, "_item_command_engine", lambda: SimpleNamespace(update=update))
    server._set_target_attention(
        {"kind": "build", "id": "book-1"},
        {
            "capture_id": CAPTURE_ID, "revision": 4,
            "needs_attention": True, "attention_reason": "phone",
            "needs_review": False,
        },
        "",
    )

    assert len(calls) == 2
    assert calls[0].patch.metadata_set["attention"] == "phone"
    assert calls[1].patch.metadata_set["attention"] == \
        "Desktop: newer desktop\nPhone: phone"


def test_cloud_review_sync_acks_last_and_does_not_duplicate(monkeypatch, data_root):
    root = data_root / "capture-review-cloud"
    root.mkdir(exist_ok=True)
    manual_path = root / "manual.json"
    reviews_path = root / "reviews.json"
    state_path = root / "capture-sync.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    lib.save_json(manual_path, {"manual-1": {
        "id": "manual-1", "title": "Herbal", "capture_id": CAPTURE_ID,
        "attention": "",
    }})
    cloud = {
        "capture_id": CAPTURE_ID, "revision": 1,
        "updated_at": "2026-07-22T12:00:00Z",
        "needs_attention": True, "attention_reason": "Phone reason",
        "needs_review": True, "review_id": "", "status": "",
    }
    writes = []
    monkeypatch.setattr(supabase_sync, "list_capture_ids",
                        lambda _cfg, _ids: [CAPTURE_ID])
    monkeypatch.setattr(supabase_sync, "list_capture_reviews",
                        lambda _cfg, _ids: [deepcopy(cloud)])

    def write(_cfg, row, expected):
        writes.append((deepcopy(row), expected))
        return {
            **row, "revision": 2,
            "updated_at": "2026-07-22T12:01:00Z",
        }

    monkeypatch.setattr(supabase_sync, "write_capture_review", write)
    result = server._sync_capture_reviews(OWNER_CFG, CAPTURE_CFG)
    accepted = {
        **writes[0][0], "revision": 2,
        "updated_at": "2026-07-22T12:01:00Z",
    }
    monkeypatch.setattr(supabase_sync, "list_capture_reviews",
                        lambda _cfg, _ids: [accepted])
    repeated = server._sync_capture_reviews(OWNER_CFG, CAPTURE_CFG)

    assert result["merged"] == 1
    assert result["pushed"] == 1
    assert repeated["pushed"] == 0
    assert len(writes) == 1
    assert len(lib.load_json(reviews_path, {})) == 1
    shadow = server._capture_phone_sync_state()["review_shadows"][CAPTURE_ID]
    assert shadow["revision"] == 2
    assert shadow["target_key"] == "row:manual-1"


def test_cloud_review_sync_reconciles_recreated_lower_revision(monkeypatch, data_root):
    root = data_root / "capture-review-recreated"
    root.mkdir(exist_ok=True)
    manual_path = root / "manual.json"
    reviews_path = root / "reviews.json"
    state_path = root / "capture-sync.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    lib.save_json(manual_path, {"manual-1": {
        "id": "manual-1", "title": "Herbal", "capture_id": CAPTURE_ID,
        "attention": "Desktop reason",
    }})
    server._review_shadow(CAPTURE_ID, {
        "capture_id": CAPTURE_ID, "revision": 8,
        "updated_at": "2026-07-22T11:00:00Z",
        "needs_attention": False, "attention_reason": "",
        "needs_review": False, "review_id": "", "status": "",
    }, "row:manual-1")
    recreated = {
        "capture_id": CAPTURE_ID, "revision": 1,
        "updated_at": "2026-07-22T12:00:00Z",
        "needs_attention": True, "attention_reason": "Phone reason",
        "needs_review": False, "review_id": "", "status": "",
    }
    writes = []
    monkeypatch.setattr(supabase_sync, "list_capture_ids",
                        lambda _cfg, _ids: [CAPTURE_ID])
    monkeypatch.setattr(supabase_sync, "list_capture_reviews",
                        lambda _cfg, _ids: [deepcopy(recreated)])

    def write(_cfg, desired, expected):
        writes.append((deepcopy(desired), expected))
        return {
            **desired, "revision": 2,
            "updated_at": "2026-07-22T12:01:00Z",
        }

    monkeypatch.setattr(supabase_sync, "write_capture_review", write)

    result = server._sync_capture_reviews(OWNER_CFG, CAPTURE_CFG)

    assert result["merged"] == 1
    assert result["pushed"] == 1
    assert writes[0][1] == 1
    assert writes[0][0]["attention_reason"] == \
        "Desktop: Desktop reason\nPhone: Phone reason"
    assert server._capture_phone_sync_state()["review_shadows"][CAPTURE_ID][
        "revision"
    ] == 2


def test_cloud_review_sync_filters_lan_only_targets_before_fk_writes(
        monkeypatch, data_root):
    root = data_root / "capture-review-mixed-transport"
    root.mkdir(exist_ok=True)
    manual_path = root / "manual.json"
    reviews_path = root / "reviews.json"
    state_path = root / "capture-sync.json"
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    lib.save_json(manual_path, {
        "cloud": {
            "id": "cloud", "capture_id": CAPTURE_ID,
            "title": "Cloud", "attention": "Check cloud",
        },
        "lan": {
            "id": "lan", "capture_id": SECOND_CAPTURE_ID,
            "title": "LAN", "attention": "Check LAN",
        },
    })
    queried = []
    writes = []
    monkeypatch.setattr(
        supabase_sync,
        "list_capture_ids",
        lambda cfg, ids: queried.append((cfg, set(ids))) or [CAPTURE_ID],
    )
    monkeypatch.setattr(
        supabase_sync,
        "list_capture_reviews",
        lambda cfg, ids: queried.append((cfg, set(ids))) or [],
    )
    monkeypatch.setattr(
        supabase_sync,
        "write_capture_review",
        lambda cfg, row, expected: writes.append((cfg, row, expected)) or {
            **row, "revision": 1,
            "updated_at": "2026-07-22T12:00:00Z",
        },
    )

    result = server._sync_capture_reviews(OWNER_CFG, CAPTURE_CFG)

    assert queried[0] == (CAPTURE_CFG, {CAPTURE_ID, SECOND_CAPTURE_ID})
    assert queried[1] == (OWNER_CFG, {CAPTURE_ID})
    assert [row[1]["capture_id"] for row in writes] == [CAPTURE_ID]
    assert writes[0][0] == OWNER_CFG
    assert result["pushed"] == 1


def test_metadata_publish_filters_lan_only_capture_and_tombstones_cloud(
        monkeypatch, data_root):
    root = data_root / "capture-metadata-publish"
    root.mkdir(exist_ok=True)
    builds_path = root / "builds.json"
    manual_path = root / "manual.json"
    state_path = root / "capture-sync.json"
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    lib.save_json(builds_path, {
        "cloud-book": {"id": "cloud-book", "capture_id": CAPTURE_ID,
                       "updated_at": "2026-01-01"},
        "lan-book": {"id": "lan-book", "capture_id": SECOND_CAPTURE_ID,
                     "updated_at": "2026-01-01"},
    })
    lib.save_json(manual_path, {})
    scope_calls = []
    monkeypatch.setattr(
        supabase_sync,
        "list_capture_ids",
        lambda cfg, ids: scope_calls.append(("ids", cfg, set(ids))) or [CAPTURE_ID],
    )
    monkeypatch.setattr(
        supabase_sync,
        "list_capture_book_metadata",
        lambda cfg, ids: scope_calls.append(("metadata", cfg, set(ids))) or [],
    )
    monkeypatch.setattr(
        supabase_sync,
        "list_capture_scan_state",
        lambda cfg, ids: scope_calls.append(("scan-state", cfg, set(ids))) or [],
    )
    batches = []
    monkeypatch.setattr(
        supabase_sync, "push_capture_book_metadata",
        lambda cfg, rows: batches.append((cfg, deepcopy(rows))) or len(rows),
    )

    assert server._publish_capture_book_metadata(OWNER_CFG, CAPTURE_CFG) == 1
    assert [row["capture_id"] for row in batches[-1][1]] == [CAPTURE_ID]
    assert batches[-1][0] == OWNER_CFG
    assert scope_calls[0] == (
        "ids", CAPTURE_CFG, {CAPTURE_ID, SECOND_CAPTURE_ID},
    )
    assert scope_calls[1] == ("scan-state", CAPTURE_CFG, {CAPTURE_ID})
    assert scope_calls[2] == ("metadata", OWNER_CFG, {CAPTURE_ID})
    lib.save_json(builds_path, {})
    assert server._publish_capture_book_metadata(OWNER_CFG, CAPTURE_CFG) == 1
    tombstone = batches[-1][1][0]
    assert tombstone["capture_id"] == CAPTURE_ID
    assert tombstone["book_id"] == ""
    assert tombstone["data"]["registered"] is False
    assert tombstone["data"]["projection_source"]["tombstone_updated_at"]


def test_metadata_projection_failure_does_not_stop_other_cloud_pipelines(monkeypatch):
    reached = []
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(server, "_refresh_collection_aliases", lambda *_args: None)
    monkeypatch.setattr(supabase_sync, "list_pending_captures", lambda _cfg: [])
    monkeypatch.setattr(supabase_sync, "push_books", lambda *_args: 0)
    monkeypatch.setattr(
        server, "_sync_capture_reviews", lambda _owner, _capture: {"errors": []},
    )
    monkeypatch.setattr(
        server, "_publish_capture_book_metadata",
        lambda _owner, _capture: (_ for _ in ()).throw(
            ValueError("bad projection")
        ),
    )
    monkeypatch.setattr(
        server.store_sync, "sync_stores",
        lambda *_args, **_kwargs: reached.append("stores") or {},
    )
    monkeypatch.setattr(server, "_lease_r2_cfg", lambda: contextlib.nullcontext({}))
    monkeypatch.setattr(server.r2, "configured", lambda _cfg: False)

    result = server._cloud_sync_run_with_configs(
        {"url": "owner", "key": "service"},
        {"url": "capture", "key": "user"},
    )

    assert "stores" in reached
    assert any("capture metadata: bad projection" in error
               for error in result["errors"])
