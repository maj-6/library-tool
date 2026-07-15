from __future__ import annotations

import json

import server


def test_build_api_preserves_volume_group_metadata(client):
    response = client.post("/api/builds", json={"build": {
        "title": "A Work",
        "volume": "2",
        "group_id": "a-work",
        "status": "ready",
    }})

    assert response.status_code == 200
    build = response.get_json()["build"]
    assert build["volume"] == "2"
    assert build["group_id"] == "a-work"


def test_verified_build_without_folder_is_listed_for_ocr(client):
    created = client.post("/api/builds", json={"build": {
        "title": "Ready for OCR",
        "status": "ready",
    }}).get_json()["build"]

    entries = client.get("/api/entries").get_json()["entries"]

    assert created["id"] in entries
    assert entries[created["id"]]["ocr"] == []


def test_captured_metadata_survives_build_creation_and_manifest(client, data_root):
    photo = data_root / "captures" / "capture-01" / "photo_1.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"captured-photo")

    response = client.post("/api/builds", json={"build": {
        "title": "Captured Work",
        "status": "ready",
        "capture_id": "capture-01",
        "images": [
            "captures/capture-01/photo_1.jpg",
            "captures/capture-01/photo_1.jpg",
            "../outside.jpg",
            "/captures/capture-01/photo_1.jpg",
            "C:/captures/capture-01/photo_1.jpg",
            "captures/capture-01/not-an-image.txt",
        ],
        "extra": {
            "binding": {"material": "  cloth  ", "empty": " "},
            "copy_count": 2,
            "missing": None,
        },
    }})

    assert response.status_code == 200
    build = response.get_json()["build"]
    assert build["capture_id"] == "capture-01"
    assert build["images"] == ["captures/capture-01/photo_1.jpg"]
    assert build["extra"] == {
        "binding": {"material": "cloth"},
        "copy_count": 2,
    }

    manifest = client.get(f"/api/builds/{build['id']}/folder").get_json()
    assert manifest["captured_images"] == [{
        "name": "photo_1.jpg",
        "path": "captures/capture-01/photo_1.jpg",
        "size": len(b"captured-photo"),
        "available": True,
    }]
    served = client.get(
        "/api/capture/image?path=captures%2Fcapture-01%2Fphoto_1.jpg")
    assert served.status_code == 200
    assert served.data == b"captured-photo"


def test_full_text_manifest_disambiguates_root_and_directory_files(client):
    build = client.post("/api/builds", json={"build": {
        "title": "Two full text artifacts",
        "status": "ready",
    }}).get_json()["build"]
    entry = server._entry_dir(build["id"])
    (entry / "full_text").mkdir(parents=True, exist_ok=True)
    (entry / "full_text.txt").write_text("root text", encoding="utf-8")
    (entry / "full_text" / "full_text.txt").write_text(
        "directory text", encoding="utf-8")

    manifest = client.get(f"/api/builds/{build['id']}/folder").get_json()
    assert [(row["name"], row["artifact"]) for row in manifest["full_text"]] == [
        ("full_text.txt", "full_text.txt"),
        ("full_text.txt", "full_text/full_text.txt"),
    ]

    root = client.get(
        f"/api/builds/{build['id']}/artifact/full_text/full_text.txt")
    nested = client.get(
        f"/api/builds/{build['id']}/artifact/full_text/full_text/full_text.txt")
    assert root.get_json()["text"] == "root text"
    assert nested.get_json()["text"] == "directory text"


def test_analyze_summary_updates_editor_description(data_root):
    server.BUILDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.BUILDS_PATH.write_text(json.dumps({
        "summary01": {"id": "summary01", "title": "Summarized", "description": "old"},
    }), encoding="utf-8")

    server._save_analyze_summary("summary01", "New catalog description.\n")

    builds = json.loads(server.BUILDS_PATH.read_text(encoding="utf-8"))
    assert builds["summary01"]["description"] == "New catalog description."
    assert (server._entry_dir("summary01") / "summary.md").read_text(
        encoding="utf-8") == "New catalog description.\n"


def test_published_volume_row_carries_volume_group_metadata():
    row = server._volume_row({
        "title": "A Work", "volume": "3", "group_id": "a-work",
    }, "a-work-3", "https://example.test/a.pdf", "a.pdf", 10, "tester")

    assert row["volume"] == "3"
    assert row["group_id"] == "a-work"
