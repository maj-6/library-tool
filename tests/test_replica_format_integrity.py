"""Focused identity and translation invariants for the Replica format layer."""
from __future__ import annotations

import json
import zipfile

import pytest

import layout_roles
import libformat


def _item(*, rid: str = "", text: str = "text") -> dict:
    item = {
        "role": "body",
        "order": 0,
        "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        "text": text,
    }
    if rid:
        item["rid"] = rid
    return item


def _document(*pages: libformat.LibPage) -> libformat.LibDocument:
    return libformat.LibDocument(
        format=(2, 0),
        book={"format_version": "2.0", "book_id": "b-test"},
        pages=list(pages),
    )


def test_new_region_ids_carry_full_uuid_entropy():
    ids = {libformat.new_rid() for _ in range(32)}

    assert len(ids) == 32
    assert all(len(rid) == 32 and libformat.RID_RE.fullmatch(rid)
               for rid in ids)
    assert all(set(rid) <= set("0123456789abcdef") for rid in ids)


def test_repeated_write_preserves_minted_region_ids(tmp_path):
    doc = _document(libformat.LibPage(page=1, items=[_item()]))
    first = tmp_path / "first.lib"
    second = tmp_path / "second.lib"

    libformat.write_lib(doc, first)
    minted = doc.pages[0].items[0]["rid"]
    libformat.write_lib(doc, second)

    with zipfile.ZipFile(first) as archive:
        first_rid = json.loads(archive.read("pages/1.json"))["items"][0]["rid"]
    with zipfile.ZipFile(second) as archive:
        second_rid = json.loads(archive.read("pages/1.json"))["items"][0]["rid"]

    assert len(minted) == 32
    assert first_rid == minted == second_rid


def test_write_rejects_cross_page_duplicate_region_id(tmp_path):
    shared = "shared-region-id"
    doc = _document(
        libformat.LibPage(page=1, items=[_item(rid=shared, text="one")]),
        libformat.LibPage(page=2, items=[_item(rid=shared, text="two")]),
    )
    destination = tmp_path / "ambiguous.lib"

    with pytest.raises(libformat.LibError, match="duplicate rid.*pages 1 and 2"):
        libformat.write_lib(doc, destination)

    assert not destination.exists()


def test_late_write_failure_preserves_existing_archive(tmp_path):
    destination = tmp_path / "existing.lib"
    destination.write_bytes(b"known-good-archive")
    doc = _document(libformat.LibPage(page=1, items=[_item()]))
    doc.translations = {"en": {"pages": {"1": float("nan")}}}

    with pytest.raises(ValueError):
        libformat.write_lib(doc, destination)

    assert destination.read_bytes() == b"known-good-archive"
    assert not list(tmp_path.glob("existing.lib.tmp-*"))


def test_equal_weight_translation_uses_each_region_in_order():
    assert layout_roles.distribute_text(
        "a\n\nb\n\nc", [1, 1, 1]) == ["a", "b", "c"]
