from __future__ import annotations

import hashlib
import io
import json

from PIL import Image


def _jpeg(seed: str = "capture") -> bytes:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    stream = io.BytesIO()
    Image.new("RGB", (1, 1), tuple(digest[:3])).save(stream, format="JPEG")
    return stream.getvalue()


def _android_photo_contract(capture_id: str, raw: bytes) -> dict:
    return {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": capture_id,
        "legacy_fallback": False,
        "assets": [{
            "asset_id": "asset-1",
            "capture_order": 1,
            "capture_file": "photo_1.jpg",
            "original": {
                "reference": "original_asset-1.jpg",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "revision": 1,
                "width": 1,
                "height": 1,
                "orientation": 0,
            },
            "display": {
                "reference": "photo_1.jpg",
                "sha256": hashlib.sha256(b"display-" + raw).hexdigest(),
                "revision": 1,
                "width": 1,
                "height": 1,
                "orientation": 0,
                "recipe": "android-standardize",
                "recipe_version": "1",
            },
            "lifecycle": {"state": "completed"},
            "role": {"suggested": "title_page", "confidence": 0.7},
            "geometry": [],
        }],
        "selections": {
            "primary_title": {"asset_id": "asset-1"},
            "thumbnail": {"asset_id": None},
        },
        "transport": {"representation": "original", "version": 1},
    }


def _android_capture_notes(capture_id: str) -> dict:
    return {
        "schema": "org.whl.bookcapture.capture-notes",
        "version": 1,
        "capture_id": capture_id,
        "notes": [{
            "id": "note-1",
            "status": "completed",
            "transcript": (
                "Auction copy. Price: $12. Pages: 240. "
                "Condition: sound. Illustrations: twelve plates. Remark: signed"
            ),
            "unclassified_text": "Auction copy.",
            "rows": [
                {"field": "price", "label": "Price", "value": "$12"},
                {"field": "pages", "label": "Pages", "value": "240"},
                {"field": "condition", "label": "Condition", "value": "sound"},
                {"field": "illustrations", "label": "Illustrations",
                 "value": "twelve plates"},
                {"field": "remark", "label": "Remark", "value": "signed"},
            ],
            "started_at_ms": 1000,
            "updated_at_ms": 2000,
            "completed_at_ms": 2000,
            "provider": "mistral",
            "model": "voxtral-mini-transcribe-realtime-2602",
        }],
    }


def test_phone_result_preserves_unknown_metadata(monkeypatch):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    cap = {
        "ocr": {"title.jpg": "OCR text"},
        "meta": {
            "title": "A Book",
            "spine_title": "A Short Spine Title",
            "extra": {"series": "Library studies"},
            "binding": {"material": "cloth", "colors": ["red", "gold"]},
            "former_owner": "Jane Doe",
        },
    }

    result = server._phone_result(cap, [b"image"], ["phone/title.jpg"])

    assert result["fields"]["title"] == "A Book"
    assert result["extra"] == {
        "series": "Library studies",
        "spine_title": "A Short Spine Title",
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
    that batch came from, and sends flat strings inside `meta`. They ride the
    unknown-key passthrough into `extra`; the desktop's display columns are
    derived from these immutable snapshot keys, not stored editable fields.
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
            "extra": {
                "scan_collection_id": "nested-forged-id",
                "scan_collection": "Nested forged name",
                "scan_from": "Nested forged origin",
            },
            "scan_collection_id": "11111111-2222-3333-4444-555555555555",
            "scan_collection": "Blue crate",
            "scan_from": "Christopher Office",
        },
    }

    entry_id, errors = server.ingest_capture(cap, [_jpeg("collection")], "")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert errors == []
    assert entry["title"] == "A Book"
    assert entry["extra"] == {
        "scan_collection_id": "11111111-2222-3333-4444-555555555555",
        "scan_collection": "Blue crate",
        "scan_from": "Christopher Office",
    }
    for key in ("scan_collection_id", "scan_collection", "scan_from"):
        assert key not in lib.MANUAL_ENTRY_FIELDS
        assert key not in {name for name in entry if name != "extra"}


def test_photo_asset_contract_is_transport_metadata_not_catalog_metadata(monkeypatch):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    capture_id = "2ec86526-1133-4e74-a2c7-497886201d76"
    contract = _android_photo_contract(capture_id, b"image")
    cap = {
        "id": capture_id,
        "ocr": {},
        "meta": {server.PHONE_PHOTO_ASSETS_KEY: contract},
    }

    assert server._phone_result(cap, [b"image"], []) is None
    cap["meta"]["title"] = "A Book"
    result = server._phone_result(cap, [b"image"], [])
    assert result["fields"]["title"] == "A Book"
    assert server.PHONE_PHOTO_ASSETS_KEY not in result["extra"]


def test_capture_notes_are_internal_and_cannot_select_phone_ocr(monkeypatch):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    capture_id = "ed3cb24e-490a-49b1-a066-4e9768bf3f00"
    notes = _android_capture_notes(capture_id)
    cap = {
        "id": capture_id,
        "ocr": {},
        "meta": {server.PHONE_CAPTURE_NOTES_KEY: notes},
    }

    assert server._phone_result(cap, [b"image"], []) is None

    cap["meta"]["title"] = "A Book"
    result = server._phone_result(cap, [b"image"], [])
    assert result["fields"]["title"] == "A Book"
    assert server.PHONE_CAPTURE_NOTES_KEY not in result["extra"]
    assert "price" not in result["extra"]


def test_notes_merge_only_after_desktop_ocr_selection_and_keep_raw_sidecar(
        monkeypatch, data_root):
    import libcommon as lib
    import server

    called = []

    def desktop_ocr(raws, key):
        called.append((raws, key))
        return {
            "photos": list(raws),
            "ocr_text": "desktop OCR",
            "fields": {"title": "Desktop Read This"},
            "extra": {"condition": "OCR guess", "binding": "cloth"},
            "errors": [],
        }

    monkeypatch.setattr(server.capture, "process_capture", desktop_ocr)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    capture_id = "221a5882-b861-424f-b3ef-029ea1eb62e3"
    notes = _android_capture_notes(capture_id)
    cap = {
        "id": capture_id,
        "meta": {server.PHONE_CAPTURE_NOTES_KEY: notes},
    }

    raw = _jpeg("notes")
    entry_id, errors = server.ingest_capture(cap, [raw], "desktop-key")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert errors == []
    assert called == [([raw], "desktop-key")]
    assert entry["title"] == "Desktop Read This"
    assert entry["extra"] == {
        "binding": "cloth",
        "price": "$12",
        "pages": "240",
        "condition": "sound",
        "illustrations": "twelve plates",
        "remark": "signed",
    }
    assert server.PHONE_CAPTURE_NOTES_KEY not in entry["extra"]
    stored = json.loads(
        (server.CAPTURES_DIR / capture_id / "capture_notes.json").read_text("utf-8")
    )
    assert stored == notes
    assert stored["notes"][0]["provider"] == "mistral"
    assert stored["notes"][0]["transcript"].startswith("Auction copy.")


def test_ingest_preserves_versioned_photo_contract_and_desktop_lineage(
        monkeypatch, data_root):
    import server

    corrected = _jpeg("corrected")
    monkeypatch.setattr(server.capture, "process_photo", lambda _raw: corrected)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    raw = _jpeg("raw")
    capture_id = "aa7f0ec0-fb63-4b8d-850e-d88f7054c113"
    contract = _android_photo_contract(capture_id, raw)
    cap = {
        "id": capture_id,
        "photo_assets": contract,
        "meta": {"title": "A Book"},
    }

    server.ingest_capture(cap, [raw], "", ["photo_1.jpg"])
    sidecar = json.loads((server.CAPTURES_DIR / cap["id"] /
                          "photo_assets.json").read_text("utf-8"))

    assert sidecar["assets"] == contract["assets"]
    imported = sidecar["desktop_import"]["assets"][0]
    assert imported["raw_ref"] == "orig_1.jpg"
    assert imported["display_ref"] == "photo_1.jpg"
    assert imported["asset_id"] == "asset-1"
    assert imported["transport_representation"] == "original"
    assert imported["lifecycle"] == "completed"
    assert imported["source_checksum"] != imported["derivative_checksum"]


def test_ingest_rejects_a_valid_contract_with_the_wrong_original_bytes(
        monkeypatch, data_root):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    capture_id = "8e056b0c-8d94-4ad6-91cf-0b40f15dfdb2"
    cap = {
        "id": capture_id,
        "photo_assets": _android_photo_contract(capture_id, b"expected-original"),
        "meta": {"title": "A Book"},
    }

    try:
        server.ingest_capture(cap, [b"different-original"], "", ["photo_1.jpg"])
    except ValueError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("mismatched original must not produce an import receipt")
    assert not (server.CAPTURES_DIR / capture_id).exists()


def test_ingest_rejects_display_bytes_advertised_as_original(
        monkeypatch, data_root):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    capture_id = "1df82465-61c4-43da-b549-1dc1cb2fe623"
    raw = b"expected-original"
    cap = {
        "id": capture_id,
        "photo_assets": _android_photo_contract(capture_id, raw),
        "meta": {"title": "A Book"},
    }

    try:
        server.ingest_capture(cap, [b"display-" + raw], "", ["photo_1.jpg"])
    except ValueError as exc:
        assert "display derivative" in str(exc)
    else:
        raise AssertionError("display bytes must not satisfy an original transport contract")


def test_ingest_fails_closed_for_an_advertised_malformed_contract(
        monkeypatch, data_root):
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    capture_id = "a56435da-e719-4187-89a7-40fd2ac02af3"
    cap = {
        "id": capture_id,
        "photo_assets": {
            "schema": "org.whl.bookcapture.photo-assets",
            "version": 1,
            "capture_id": capture_id,
            "assets": [{"asset_id": "asset-1"}],
        },
        "meta": {"title": "A Book"},
    }

    try:
        server.ingest_capture(cap, [b"bytes"], "", ["photo_1.jpg"])
    except ValueError as exc:
        assert "invalid photo asset contract" in str(exc)
    else:
        raise AssertionError("malformed advertised contract must not fall back to legacy")
    assert not (server.CAPTURES_DIR / capture_id).exists()


def test_malformed_wire_provenance_is_ignored_as_non_string_metadata(
        monkeypatch, data_root):
    import libcommon as lib
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    entry_id, errors = server.ingest_capture({
        "id": "8a1f0a52-1d4e-4a77-9a6e-6f2b0c5e77bb",
        "meta": {
            "title": "Malformed provenance",
            "extra": {
                "series": "Kept metadata",
                "scan_collection_id": "nested-forged-id",
                "scan_collection": "Nested forged name",
                "scan_from": "Nested forged origin",
            },
            "scan_collection_id": {"not": "a flat UUID string"},
            "scan_collection": ["not", "flat"],
            "scan_from": 42,
        },
    }, [_jpeg("malformed-provenance")], "")

    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]
    assert errors == []
    assert entry["title"] == "Malformed provenance"
    assert entry["extra"]["series"] == "Kept metadata"
    assert not (server.PHONE_PROVENANCE_KEYS & set(entry.get("extra") or {}))


def test_provenance_alone_does_not_look_like_extracted_metadata(monkeypatch):
    """A phone with no API key extracts nothing, so the desktop must run its own
    OCR. Provenance rides in `meta` on EVERY capture, so if it counted as
    metadata the phone would always look like it had extracted something and the
    fallback pipeline would never run — every LAN capture would file blank."""
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)

    assert server._phone_result({"meta": {}, "ocr": {}}, [b"image"], []) is None
    provenance_only = {
        "meta": {
            "scan_collection_id": "11111111-2222-3333-4444-555555555555",
            "scan_collection": "Blue crate",
            "scan_from": "Storage",
        },
        "ocr": {},
    }
    assert server._phone_result(provenance_only, [b"image"], []) is None

    # ...but a single real extracted field still short-circuits the second pass
    with_title = {"meta": {
        "title": "A Book",
        "scan_collection_id": "11111111-2222-3333-4444-555555555555",
        "scan_from": "Storage",
    }, "ocr": {}}
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
        "meta": {
            "scan_collection_id": "11111111-2222-3333-4444-555555555555",
            "scan_collection": "Blue crate",
            "scan_from": "Storage",
        },
    }

    entry_id, _ = server.ingest_capture(cap, [_jpeg("desktop-fallback")], "")
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert entry["title"] == "Desktop Read This"       # the fallback really ran
    assert entry["extra"] == {
        "scan_collection_id": "11111111-2222-3333-4444-555555555555",
        "scan_collection": "Blue crate",
        "scan_from": "Storage",
    }


def test_older_phone_collection_without_id_stays_unlinked():
    """Pre-upgrade captures remain valid snapshots, but gain no invented id."""
    import server

    assert server._capture_provenance({"meta": {
        "scan_collection": "Blue crate",
        "scan_from": "Storage",
    }}) == {
        "scan_collection": "Blue crate",
        "scan_from": "Storage",
    }


def test_ingest_reresolves_collection_alias_at_final_save(monkeypatch, data_root):
    """A merge landing during photo work cannot reintroduce its loser id."""
    import libcommon as lib
    import server

    old = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    survivor = "11111111-2222-3333-4444-555555555555"
    calls = []
    def resolve(cid):
        calls.append(cid)
        return old if len(calls) == 1 else survivor
    monkeypatch.setattr(server, "_resolve_collection_alias", resolve)
    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})

    entry_id, _ = server.ingest_capture({
        "id": "cabba9e0-1111-2222-3333-444455556666",
        "meta": {
            "title": "Arrived during merge",
            "scan_collection_id": old,
            "scan_collection": "Old snapshot",
            "scan_from": "Office",
        },
    }, [_jpeg("collection-alias")], "")

    extra = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]["extra"]
    assert calls == [old, old]  # before processing, then inside final _manual_lock
    assert extra == {
        "scan_collection_id": survivor,
        "scan_collection": "Old snapshot",
        "scan_from": "Office",
    }


def test_generic_manual_patch_cannot_rewrite_or_drop_capture_snapshot(
        client, monkeypatch, data_root):
    import libcommon as lib
    import server

    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    entries = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})
    entries["snapshot-guard"] = {
        "id": "snapshot-guard", "title": "A Book", "extra": {
            "scan_collection_id": "11111111-2222-3333-4444-555555555555",
            "scan_collection": "Blue crate",
            "scan_from": "Christopher Office",
            "series": "Old series",
        },
    }
    lib.save_json(lib.MANUAL_ENTRIES_PATH, entries)

    response = client.patch("/api/manual/snapshot-guard", json={"extra": {
        "scan_collection_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "scan_collection": "Relabelled",
        "scan_from": "Changed origin",
        "series": "New series",
    }})

    assert response.status_code == 200
    assert response.get_json()["entry"]["extra"] == {
        "scan_collection_id": "11111111-2222-3333-4444-555555555555",
        "scan_collection": "Blue crate",
        "scan_from": "Christopher Office",
        "series": "New series",
    }

    # Replacing generic extra with an empty object still cannot remove the
    # snapshot; only the merge helper is allowed to change its id.
    response = client.patch("/api/manual/snapshot-guard", json={"extra": {}})
    assert response.get_json()["entry"]["extra"] == {
        "scan_collection_id": "11111111-2222-3333-4444-555555555555",
        "scan_collection": "Blue crate",
        "scan_from": "Christopher Office",
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

    entry_id, _ = server.ingest_capture(cap, [_jpeg("provenance-wins")], "")
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

    entry_id, errors = server.ingest_capture(
        cap,
        [_jpeg("unknown-metadata")],
        "",
    )
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert errors == []
    assert entry["title"] == "A Book"
    assert entry["extra"] == {"binding": "full calf"}
    assert "binding" not in lib.MANUAL_ENTRY_FIELDS
    assert "binding" not in {key for key in entry if key != "extra"}


def test_spine_title_is_retained_as_distinct_manual_metadata(monkeypatch, data_root):
    import libcommon as lib
    import server

    monkeypatch.setattr(server.capture, "process_photo", lambda raw: raw)
    monkeypatch.setattr(server, "_entry_checks", lambda entry: {})
    cap = {
        "id": "4f262bb1-49c1-40b3-a871-827503f15d41",
        "meta": {
            "title": "The Complete Herbal",
            "spine_title": "Culpeper's Herbal",
        },
    }

    entry_id, errors = server.ingest_capture(
        cap,
        [_jpeg("spine-title")],
        "",
    )
    entry = lib.load_json(lib.MANUAL_ENTRIES_PATH, {})[entry_id]

    assert errors == []
    assert entry["title"] == "The Complete Herbal"
    assert entry["extra"]["spine_title"] == "Culpeper's Herbal"
    assert "spine_title" not in lib.MANUAL_ENTRY_FIELDS
