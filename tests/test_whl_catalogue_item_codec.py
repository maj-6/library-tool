"""Legacy WHL catalogue-row behavior outside the Flask host."""

from __future__ import annotations

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from librarytool.adapters.filesystem.whl_catalogue_codec import (
    WhlCatalogueItemCodec,
)
from librarytool.engine.errors import RepositoryError
from librarytool.engine.item_commands import ItemDraft


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class _RevisionSequence:
    def __init__(self, *values: str) -> None:
        self.values = list(values)
        self.calls = []

    def __call__(self, previous: str) -> str:
        self.calls.append(previous)
        return self.values.pop(0)


class _ManifestValidator:
    def __init__(self) -> None:
        self.rows = []
        self.failure = None

    def __call__(self, raw) -> None:
        self.rows.append(raw)
        if self.failure is not None:
            raise self.failure


def _codec(
    revisions,
    *,
    categories=("plants", "medicine"),
    manifest=None,
) -> WhlCatalogueItemCodec:
    return WhlCatalogueItemCodec(
        advance_revision=revisions,
        category_ids_for=lambda: categories,
        validate_representation_manifest=manifest or (lambda _raw: None),
    )


def _managed_row() -> dict:
    return {
        "id": "book-one",
        "item_id": "book-one",
        "kind": "book",
        "title": "The Old Herbal",
        "status": "ready",
        "created_at": "2026-01-01T00:00:00.000000+00:00",
        "updated_at": "2026-01-02T00:00:00.000000+00:00",
        "revision": "legacy-storage-revision",
        "published_slug": "old-herbal",
        "pdf_file": r"C:\private\primary.pdf",
        "pdf_sources": [
            {"id": "scan", "path": r"C:\private\alternate.pdf"}
        ],
        "ocr_active": "compiled.txt",
        "ocr_verified": "verified.txt",
        "ocr_quality": "reviewed",
        "title_pages": "1,3",
        "thumbnail_source": "page:1",
        "images": ["capture/cover.jpg"],
        "extra": {"workspace_path": r"C:\private"},
        "capture_id": "phone-1",
        "relevance": {"score": 0.75},
        "artifacts": {"about": "about.md"},
        "representations": {"legacy": True},
        "representation_manifest": {
            "version": 1,
            "sources": {},
            "detached": [],
        },
        "authors": "Old Author",
        "year": "1600",
        "rights": "public-domain",
        "future.extension": {"nested": [1, True, None]},
    }


def test_create_encoding_matches_the_legacy_row_shape_exactly():
    revisions = _RevisionSequence("2026-07-19T12:34:56.123456+00:00")
    manifest = _ManifestValidator()
    codec = _codec(revisions, manifest=manifest)
    draft = ItemDraft(
        title="A New Herbal",
        metadata={
            "authors": "Ada Curator",
            "rights": "public-domain",
            "category_ids": ["plants"],
            "future.catalogue": {"edition_note": "First state"},
        },
    )

    raw = codec.encode("book-new", draft, None)

    assert raw == {
        "id": "book-new",
        "title": "A New Herbal",
        "status": "draft",
        "created_at": "2026-07-19T12:34:56.123456+00:00",
        "updated_at": "2026-07-19T12:34:56.123456+00:00",
        "published_slug": "",
        "pdf_file": "",
        "pdf_sources": [],
        "ocr_active": "",
        "ocr_verified": "",
        "ocr_quality": "",
        "title_pages": "",
        "thumbnail_source": "",
        "images": [],
        "extra": {},
        "capture_id": "",
        "representation_manifest": {
            "version": 1,
            "sources": {},
            "detached": [],
        },
        "authors": "Ada Curator",
        "rights": "public-domain",
        "category_ids": ("plants",),
        "future.catalogue": {"edition_note": "First state"},
    }
    assert revisions.calls == [""]
    snapshot = codec.decode("book-new", raw)
    assert snapshot.as_draft() == draft
    assert snapshot.revision == raw["updated_at"]
    assert manifest.rows == [raw]


def test_update_preserves_managed_state_and_unknown_metadata_exactly():
    revisions = _RevisionSequence("2026-07-19T12:35:00.000000+00:00")
    manifest = _ManifestValidator()
    codec = _codec(revisions, manifest=manifest)
    previous = _managed_row()
    before = deepcopy(previous)
    current = codec.decode("book-one", previous)
    metadata = current.as_draft().as_dict()["metadata"]
    metadata.pop("year")
    metadata["authors"] = "New Author"
    metadata["notes"] = "Reviewed"
    draft = ItemDraft(
        title="The Revised Herbal",
        metadata=metadata,
    )

    updated = codec.encode("book-one", draft, previous)

    assert previous == before
    assert updated["title"] == "The Revised Herbal"
    assert updated["updated_at"] == "2026-07-19T12:35:00.000000+00:00"
    assert updated["authors"] == "New Author"
    assert updated["notes"] == "Reviewed"
    assert "year" not in updated
    assert draft.as_dict()["metadata"]["future.extension"] == (
        before["future.extension"]
    )
    for field in codec.managed_fields - {"title", "updated_at"}:
        if field in before:
            assert updated[field] == before[field]
    assert revisions.calls == [before["updated_at"]]
    assert codec.decode("book-one", updated).as_draft() == draft


def test_record_revision_preserves_explicit_tokens_and_stable_fallbacks():
    raw = {
        "title": "Legacy",
        "future.extension": {"b": 2, "a": 1},
    }
    reordered = {
        "future.extension": {"a": 1, "b": 2},
        "title": "Legacy",
    }

    assert WhlCatalogueItemCodec.record_revision("book-one", raw) == (
        "ir-77e96dda1bae934d7b93499f"
    )
    assert WhlCatalogueItemCodec.record_revision("book-one", reordered) == (
        "ir-77e96dda1bae934d7b93499f"
    )
    assert WhlCatalogueItemCodec.record_revision(
        "book-one",
        {**raw, "updated_at": "record-1:+valid"},
    ) == "record-1:+valid"
    assert WhlCatalogueItemCodec.record_revision("book-two", raw) != (
        WhlCatalogueItemCodec.record_revision("book-one", raw)
    )


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        ({"id": "other"}, ValueError, "embedded build id"),
        ({"status": "unknown"}, ValueError, "build status"),
        ({"pdf_sources": [{"id": "scan"}]}, TypeError, "ids and paths"),
        ({"images": [1]}, TypeError, "array of strings"),
        ({"extra": []}, TypeError, "extra must be an object"),
        ({"relevance": []}, TypeError, "relevance must be an object"),
        ({"capture_id": "bad!"}, ValueError, "capture_id is invalid"),
    ],
)
def test_managed_row_validation_retains_strict_legacy_failures(
    change,
    error,
    message,
):
    row = _managed_row()
    row.update(change)
    codec = _codec(lambda _previous: "unused")

    with pytest.raises(error, match=message):
        codec.decode("book-one", row)


def test_manifest_validation_remains_an_explicit_host_dependency():
    manifest = _ManifestValidator()
    manifest.failure = ValueError("manifest rejected by representation adapter")
    codec = _codec(lambda _previous: "unused", manifest=manifest)
    row = _managed_row()

    with pytest.raises(ValueError, match="representation adapter"):
        codec.decode("book-one", row)

    assert manifest.rows == [row]


def test_strict_metadata_writes_use_the_injected_category_vocabulary():
    codec = _codec(
        lambda _previous: "2026-07-19T12:34:56.000000+00:00",
        categories=("plants",),
    )

    with pytest.raises(ValueError, match="unknown ids"):
        codec.encode(
            "book-new",
            ItemDraft(metadata={"category_ids": ["medicine"]}),
            None,
        )
    with pytest.raises(ValueError, match="outer whitespace"):
        codec.encode(
            "book-new",
            ItemDraft(metadata={"authors": " Padded "}),
            None,
        )


def test_restore_detaches_exact_raw_state_and_retries_revision_advancement():
    row = _managed_row()
    before = deepcopy(row)
    revisions = _RevisionSequence(
        row["updated_at"],
        "2026-07-19T12:36:00.000000+00:00",
    )
    codec = _codec(revisions)

    restored = codec.advance_restored_record("book-one", row)

    assert row == before
    assert restored["updated_at"] == "2026-07-19T12:36:00.000000+00:00"
    assert restored["id"] == "book-one"
    for key, value in before.items():
        if key != "updated_at":
            assert restored[key] == value
    assert revisions.calls == [
        before["updated_at"],
        before["updated_at"],
    ]


def test_restore_reports_invalid_rows_and_a_stalled_revision_clock():
    codec = _codec(lambda previous: previous)
    row = _managed_row()

    with pytest.raises(RepositoryError) as stalled:
        codec.advance_restored_record("book-one", row)
    assert stalled.value.code == "item_restore_revision_not_advanced"

    invalid = {**row, "id": "other"}
    with pytest.raises(RepositoryError) as rejected:
        codec.advance_restored_record("book-one", invalid)
    assert rejected.value.code == "invalid_item_restore_record"
    assert rejected.value.details == {"cause_type": "ValueError"}

    with pytest.raises(RepositoryError) as non_object:
        codec.advance_restored_record("book-one", [])
    assert non_object.value.code == "invalid_item_restore_record"
    assert non_object.value.details == {}


def test_codec_import_is_framework_free_and_has_no_cwd_side_effects(tmp_path):
    script = """
from pathlib import Path
import sys
before = tuple(Path.cwd().iterdir())
import librarytool.adapters.filesystem.whl_catalogue_codec
after = tuple(Path.cwd().iterdir())
assert before == after
assert 'flask' not in sys.modules
assert 'server' not in sys.modules
assert 'libcommon' not in sys.modules
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(SRC)
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
    )
