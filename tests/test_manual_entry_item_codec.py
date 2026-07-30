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
