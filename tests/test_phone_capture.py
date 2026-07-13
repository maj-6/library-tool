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
