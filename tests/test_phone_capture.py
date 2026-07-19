from __future__ import annotations

import json


def test_phone_result_preserves_unknown_metadata(monkeypatch):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    cap = {
        "ocr": {"title.jpg": "OCR text"},
        "meta": {
            "title": "A Book",
            "extra": {"series": "Library studies"},
            "binding": {"material": "cloth", "colors": ["red", "gold"]},
            "former_owner": "Jane Doe",
        },
    }

    result = server._phone_result(cap, [b"image"], ["phone/title.jpg"])

    assert result["fields"]["title"] == "A Book"
    assert result["extra"] == {
        "series": "Library studies",
        "binding": {"material": "cloth", "colors": ["red", "gold"]},
        "former_owner": "Jane Doe",
    }


def test_clean_extra_preserves_complete_structured_values():
    import server

    long_value = "x" * 700
    cleaned = server._clean_extra({
        " provenance ": {
            "owners": ["Jane Doe", "John Doe"],
            "details": long_value,
            "blank": "  ",
        },
        "empty": None,
    })

    assert cleaned == {
        "provenance": {
            "owners": ["Jane Doe", "John Doe"],
            "details": long_value,
        }
    }
    assert json.loads(json.dumps(cleaned)) == cleaned


def test_phone_collection_and_origin_reach_the_entry(monkeypatch, data_root):
    """Book Capture records which collection a book was scanned into and where
    that batch came from, and sends both as flat strings inside `meta`. They
    have no table columns on purpose — they ride the unknown-key passthrough
    into `extra`, so the phone can add provenance without a desktop migration.
    The Android side of this contract is CollectionsTest's
    `theUploadPayloadCarriesFlatStringsNotTheNestedManifestShape`."""
    import libcommon as lib
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    cap = {
        "id": "9c1f0a52-1d4e-4a77-9a6e-6f2b0c5e77aa",
        "meta": {
            "title": "A Book",
            "scan_collection": "Blue crate",
            "scan_from": "Christopher Office",
        },
    }

    entry_id, errors = server.ingest_capture(cap, [b"image"], "")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert errors == []
    assert entry["title"] == "A Book"
    assert entry["extra"] == {
        "scan_collection": "Blue crate",
        "scan_from": "Christopher Office",
    }
    for key in ("scan_collection", "scan_from"):
        assert key not in lib.MANUAL_ENTRY_FIELDS
        assert key not in {name for name in entry if name != "extra"}


def test_provenance_alone_does_not_look_like_extracted_metadata(monkeypatch):
    """A phone with no API key extracts nothing, so the desktop must run its own
    OCR. Provenance rides in `meta` on EVERY capture, so if it counted as
    metadata the phone would always look like it had extracted something and the
    fallback pipeline would never run — every LAN capture would file blank."""
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)

    assert server._phone_result({"meta": {}, "ocr": {}}, [b"image"], []) is None
    provenance_only = {
        "meta": {"scan_collection": "Blue crate", "scan_from": "Storage"},
        "ocr": {},
    }
    assert server._phone_result(provenance_only, [b"image"], []) is None

    # ...but a single real extracted field still short-circuits the second pass
    with_title = {"meta": {"title": "A Book", "scan_from": "Storage"}, "ocr": {}}
    assert server._phone_result(with_title, [b"image"], []) is not None


def test_provenance_survives_the_desktop_ocr_fallback(monkeypatch, data_root):
    """The fallback path never reads `meta`, so provenance has to be merged back
    in explicitly — otherwise exactly the captures that need the desktop's OCR
    are the ones that lose where they came from."""
    import libcommon as lib
    import server

    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    monkeypatch.setattr(server.capture, "process_capture", lambda raws, key: {
        "photos": list(raws),
        "ocr_text": "desktop OCR",
        "fields": {"title": "Desktop Read This"},
        "extra": {},
        "errors": [],
    })
    cap = {
        "id": "3b7d1e90-55aa-4c31-8f0e-1d2c3b4a5e6f",
        "meta": {"scan_collection": "Blue crate", "scan_from": "Storage"},
    }

    entry_id, _ = server.ingest_capture(cap, [b"image"], "")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert entry["title"] == "Desktop Read This"       # the fallback really ran
    assert entry["extra"] == {
        "scan_collection": "Blue crate",
        "scan_from": "Storage",
    }


def test_scan_provenance_outranks_a_same_named_extracted_field(monkeypatch, data_root):
    """`scan_` prefixes make a collision unlikely, not impossible. If one
    happens, the phone's own record of the shelf it lifted the book off wins
    over whatever a language model inferred from the title page."""
    import libcommon as lib
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    cap = {
        "id": "c0ffee00-1111-2222-3333-444455556666",
        "meta": {
            "title": "A Book",
            "extra": {"scan_collection": "Bibliotheque de la Pleiade"},
            "scan_collection": "Blue crate",
        },
    }

    entry_id, _ = server.ingest_capture(cap, [b"image"], "")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert entry["extra"]["scan_collection"] == "Blue crate"


def test_unknown_phone_metadata_does_not_create_table_fields(monkeypatch, data_root):
    import libcommon as lib
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    cap = {
        "id": "4f262bb1-49c1-40b3-a871-827503f15d40",
        "meta": {"title": "A Book", "binding": "full calf"},
    }

    entry_id, errors = server.ingest_capture(cap, [b"image"], "")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert errors == []
    assert entry["title"] == "A Book"
    assert entry["extra"] == {"binding": "full calf"}
    assert "binding" not in lib.MANUAL_ENTRY_FIELDS
    assert "binding" not in {key for key in entry if key != "extra"}
