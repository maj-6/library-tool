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
