"""Reusable manual-entry projection and mutation codec."""

from __future__ import annotations

import copy

import pytest

from librarytool.adapters.filesystem import ManualEntryItemCodec
from librarytool.engine.item_commands import ItemDraft


CAPTURE_ID = "11111111-1111-4111-8111-111111111111"


def _codec() -> ManualEntryItemCodec:
    return ManualEntryItemCodec(
        advance_revision=lambda previous: f"{previous or 'new'}-next",
    )


def _row() -> dict:
    return {
        "id": "manual-row",
        "title": "Captured Herbal",
        "author": "A. Botanist",
        "city": "London",
        "notes": "Shelf copy",
        "capture_id": CAPTURE_ID,
        "capture_transport": "lan",
        "created_at": "2026-07-29T01:00:00+00:00",
        "updated_at": "2026-07-29T01:00:01+00:00",
        "images": ["captures/example/photo_1.jpg"],
        "local_pdf": "private/source.pdf",
        "extra": {"spine_title": "Herbal"},
        "checks": {"whl": []},
    }


def test_record_revision_covers_the_full_row_without_trusting_timestamp():
    before = _row()
    after = copy.deepcopy(before)
    after["extra"]["spine_title"] = "Corrected Herbal"

    first = _codec().record_revision("manual-row", before)
    second = _codec().record_revision("manual-row", after)

    assert first.startswith("mir-")
    assert second.startswith("mir-")
    assert first != second
    assert before["updated_at"] == after["updated_at"]


def test_decode_projects_path_free_metadata_and_capture_kind():
    snapshot = _codec().decode("manual-row", _row())

    assert snapshot.item_id == "manual-row"
    assert snapshot.kind == "capture"
    assert snapshot.title == "Captured Herbal"
    assert snapshot.metadata["authors"] == "A. Botanist"
    assert snapshot.metadata["publisher_city"] == "London"
    assert "author" not in snapshot.metadata
    assert "city" not in snapshot.metadata
    assert snapshot.metadata["extra"] == {"spine_title": "Herbal"}
    assert "images" not in snapshot.metadata
    assert "local_pdf" not in snapshot.metadata
    assert "checks" not in snapshot.metadata


def test_scan_curation_fields_round_trip_without_conflating_prices_or_residuals():
    row = _row()
    row.update(
        {
            "price": "$40 paid at acquisition",
            "marked_price": "£1/10/- (pencil)",
            "scan_priority": "Low",
            "scan_verdict": "Scan the annotated plates before the text.",
            "future_custom": {"retain": [1, True]},
        }
    )
    codec = _codec()
    snapshot = codec.decode("manual-row", row)

    assert snapshot.metadata["price"] == "$40 paid at acquisition"
    assert snapshot.metadata["marked_price"] == "£1/10/- (pencil)"
    assert snapshot.metadata["scan_priority"] == "Low"
    assert snapshot.metadata["scan_verdict"].startswith("Scan the annotated")

    metadata = dict(snapshot.metadata)
    metadata["scan_priority"] = "High"
    updated = codec.encode(
        "manual-row",
        ItemDraft(
            kind="capture",
            title=snapshot.title,
            metadata=metadata,
        ),
        row,
    )

    assert updated["price"] == "$40 paid at acquisition"
    assert updated["marked_price"] == "£1/10/- (pencil)"
    assert updated["scan_priority"] == "High"
    assert updated["scan_verdict"] == row["scan_verdict"]
    assert updated["future_custom"] == {"retain": [1, True]}
    assert updated["extra"] == row["extra"]


def test_legacy_absence_and_blank_scan_priority_remain_unassessed():
    codec = _codec()
    absent = codec.decode("manual-row", _row())
    assert "scan_priority" not in absent.metadata

    row = {**_row(), "scan_priority": "", "scan_verdict": ""}
    blank = codec.decode("manual-row", row)
    assert blank.metadata["scan_priority"] == ""
    assert blank.metadata["scan_verdict"] == ""


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ({"scan_priority": "high"}, ValueError, "scan_priority is invalid"),
        ({"scan_priority": "3"}, ValueError, "scan_priority is invalid"),
        ({"scan_priority": 3}, TypeError, "scan_priority must be a string"),
        (
            {"scan_verdict": "Scan plates.\nThen scan text."},
            ValueError,
            "single line",
        ),
        (
            {"scan_verdict": chr(0x1F33F) * 501},
            ValueError,
            "at most 500",
        ),
    ],
)
def test_manual_rows_reject_invalid_scan_curation_fields(change, error, message):
    row = _row()
    row.update(change)

    with pytest.raises(error, match=message):
        _codec().decode("manual-row", row)


@pytest.mark.parametrize(
    "metadata",
    [
        {"marked_price": "  £1/10/-  "},
        {"scan_priority": " High "},
        {"scan_verdict": " Padded verdict "},
        {"scan_verdict": "First line\rSecond line"},
        {"scan_verdict": "x" * 501},
    ],
)
def test_manual_writes_enforce_short_field_contracts(metadata):
    with pytest.raises(ValueError):
        _codec().encode(
            "manual-row",
            ItemDraft(
                kind="capture",
                title=_row()["title"],
                metadata=metadata,
            ),
            _row(),
        )


def test_manual_write_counts_scan_verdict_length_as_unicode_code_points():
    result = _codec().encode(
        "manual-row",
        ItemDraft(
            kind="capture",
            title=_row()["title"],
            metadata={"scan_verdict": chr(0x1F33F) * 500},
        ),
        _row(),
    )

    assert result["scan_verdict"] == chr(0x1F33F) * 500


def test_digitization_candidate_round_trips_as_first_class_metadata():
    row = _row()
    row["digitization_candidate"] = True
    codec = _codec()

    snapshot = codec.decode("manual-row", row)
    assert snapshot.metadata["digitization_candidate"] is True

    metadata = dict(snapshot.metadata)
    metadata["digitization_candidate"] = False
    disabled = codec.encode(
        "manual-row",
        ItemDraft(
            kind="capture",
            title=snapshot.title,
            metadata=metadata,
        ),
        row,
    )
    assert disabled["digitization_candidate"] is False

    metadata.pop("digitization_candidate")
    removed = codec.encode(
        "manual-row",
        ItemDraft(
            kind="capture",
            title=snapshot.title,
            metadata=metadata,
        ),
        disabled,
    )
    assert "digitization_candidate" not in removed


def test_digitization_candidate_requires_a_boolean():
    row = _row()
    row["digitization_candidate"] = "yes"

    with pytest.raises(TypeError, match="digitization_candidate"):
        _codec().decode("manual-row", row)

    with pytest.raises(TypeError, match="digitization_candidate"):
        _codec().encode(
            "manual-row",
            ItemDraft(
                kind="capture",
                title=row["title"],
                metadata={"digitization_candidate": "yes"},
            ),
            _row(),
        )

    with pytest.raises(TypeError, match="digitization_candidate"):
        _codec().encode(
            "manual-row",
            ItemDraft(
                kind="capture",
                title=row["title"],
                metadata={"digitization_candidate": None},
            ),
            _row(),
        )


def test_decode_recursively_projects_only_public_generated_extra_metadata():
    row = _row()
    row["extra"] = {
        "spine_title": "Herbal",
        "scan_collection": "Private acquisition batch",
        "book_id": "b-11111111111111111111111111111111",
        "generated": {
            "caption": "A sprig of rosemary",
            "local_path": r"C:\captures\private\plate-1.jpg",
            "source": "../private/ocr.json",
            "regions": [
                {
                    "label": "ILL",
                    "assetRef": "captures/private/region-1.jpg",
                },
                "portable note",
                "private-layout.json",
            ],
        },
    }

    snapshot = _codec().decode("manual-row", row)

    assert snapshot.metadata["extra"] == {
        "spine_title": "Herbal",
        "generated": {
            "caption": "A sprig of rosemary",
            "regions": (
                {"label": "ILL"},
                "portable note",
            ),
        },
    }
    assert row["extra"]["scan_collection"] == "Private acquisition batch"
    assert row["extra"]["generated"]["local_path"].startswith("C:")


def test_encode_preserves_capture_images_and_local_state():
    previous = _row()
    before_revision = _codec().record_revision("manual-row", previous)
    result = _codec().encode(
        "manual-row",
        ItemDraft(
            kind="capture",
                title="Corrected Herbal",
                metadata={
                    "authors": "B. Botanist",
                    "publisher_city": "Edinburgh",
                    "notes": "Shelf copy",
                "extra": {"spine_title": "Corrected Herbal"},
            },
        ),
        previous,
    )

    assert result["title"] == "Corrected Herbal"
    assert result["author"] == "B. Botanist"
    assert result["city"] == "Edinburgh"
    assert "authors" not in result
    assert "publisher_city" not in result
    assert result["capture_id"] == CAPTURE_ID
    assert result["images"] == previous["images"]
    assert result["local_pdf"] == previous["local_pdf"]
    assert result["checks"] == previous["checks"]
    assert result["updated_at"].endswith("-next")
    assert _codec().record_revision("manual-row", result) != before_revision


def test_encode_edits_public_extra_while_preserving_private_nested_state():
    previous = _row()
    previous["extra"] = {
        "spine_title": "Herbal",
        "scan_collection_id": "collection-private",
        "scan_from": "donor-private",
        "book_id": "b-11111111111111111111111111111111",
        "generated": {
            "caption": "Old caption",
            "local_path": r"C:\captures\private\plate-1.jpg",
            "regions": [
                {
                    "label": "ILL",
                    "asset_ref": "captures/private/region-1.jpg",
                }
            ],
        },
    }

    result = _codec().encode(
        "manual-row",
        ItemDraft(
            kind="capture",
            title=previous["title"],
            metadata={
                "authors": previous["author"],
                "publisher_city": previous["city"],
                "notes": previous["notes"],
                "extra": {
                    "spine_title": "Corrected Herbal",
                    "generated": {
                        "caption": "Corrected botanical caption",
                        "regions": [{"label": "MAR"}],
                    },
                },
            },
        ),
        previous,
    )

    assert result["extra"] == {
        "spine_title": "Corrected Herbal",
        "scan_collection_id": "collection-private",
        "scan_from": "donor-private",
        "book_id": "b-11111111111111111111111111111111",
        "generated": {
            "caption": "Corrected botanical caption",
            "local_path": r"C:\captures\private\plate-1.jpg",
            "regions": [
                {
                    "label": "MAR",
                    "asset_ref": "captures/private/region-1.jpg",
                }
            ],
        },
    }


def test_encode_removes_public_extra_without_removing_private_state():
    previous = _row()
    previous["extra"] = {
        "spine_title": "Safe to remove",
        "scan_collection_id": "collection-private",
        "generated": {
            "caption": "Safe to remove",
            "local_path": r"C:\captures\private\plate-1.jpg",
        },
    }

    result = _codec().encode(
        "manual-row",
        ItemDraft(
            kind="capture",
            title=previous["title"],
            metadata={
                "authors": previous["author"],
                "publisher_city": previous["city"],
                "notes": previous["notes"],
            },
        ),
        previous,
    )

    assert result["extra"] == {
        "scan_collection_id": "collection-private",
        "generated": {
            "local_path": r"C:\captures\private\plate-1.jpg",
        },
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"scan_from": "forged provenance"},
        {"generated": {"local_path": "C:/private/new.jpg"}},
        {"generated": {"regions": [{"asset_ref": "private/new.jpg"}]}},
        {"generated": {"source": "../private/result.json"}},
    ],
)
def test_encode_rejects_private_extra_metadata_from_an_editor(extra):
    previous = _row()

    with pytest.raises(
        ValueError,
        match="server-managed metadata or a private locator",
    ):
        _codec().encode(
            "manual-row",
            ItemDraft(
                kind="capture",
                title=previous["title"],
                metadata={
                    "authors": previous["author"],
                    "publisher_city": previous["city"],
                    "notes": previous["notes"],
                    "extra": extra,
                },
            ),
            previous,
        )


def test_codec_rejects_capture_aliases_and_embedded_id_conflicts():
    aliased = _row()
    aliased["capture_id"] = " {11111111-1111-4111-8111-111111111111} "
    with pytest.raises(ValueError, match="capture_id"):
        _codec().decode("manual-row", aliased)

    conflicting = _row()
    conflicting["id"] = "another-row"
    with pytest.raises(ValueError, match="embedded"):
        _codec().decode("manual-row", conflicting)

    with pytest.raises(ValueError, match="invalid Unicode"):
        _codec().record_revision("\ud800", _row())
