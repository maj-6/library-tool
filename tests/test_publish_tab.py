from __future__ import annotations

import json

import server


def _build_file(tmp_path, monkeypatch, builds):
    path = tmp_path / "builds.json"
    path.write_text(json.dumps(builds), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", path)
    return path


def test_publish_catalog_falls_back_to_local_uploaded_builds(
        client, tmp_path, monkeypatch):
    pdf = tmp_path / "volume.pdf"
    pdf.write_bytes(b"%PDF-preview")
    _build_file(tmp_path, monkeypatch, {"local01": {
        "id": "local01", "status": "uploaded", "published_slug": "work-1801",
        "title": "A Work", "authors": "A. Author", "year": "1801",
        "volume": "1", "group_id": "work-set", "categories": "Botany",
        "pdf_file": str(pdf), "bundle": {},
    }})
    monkeypatch.setattr(server, "_auth_cfg", lambda: None)
    monkeypatch.setattr(server, "_cloud_cfg", lambda: (_ for _ in ()).throw(
        AssertionError("Publish preview must not use the service credential")))

    response = client.get("/api/publish/catalog")

    assert response.status_code == 200
    data = response.get_json()
    assert data["source"] == "local"
    assert data["site_url"]
    assert data["entries"] == [data["entries"][0]]
    row = data["entries"][0]
    assert row["slug"] == "work-1801"
    assert row["group_id"] == "work-set"
    assert row["pdf_bytes"] == len(b"%PDF-preview")
    assert row["local_build_id"] == "local01"


def test_publish_catalog_treats_successful_cloud_read_as_authoritative(
        client, tmp_path, monkeypatch):
    _build_file(tmp_path, monkeypatch, {
        "local02": {
            "id": "local02", "status": "uploaded", "published_slug": "same-slug",
            "title": "Cloud Work", "volume": "2", "group_id": "cloud-set",
            "categories": "", "category_ids": [], "bundle": {},
        },
        "local-only": {
            "id": "local-only", "status": "uploaded",
            "published_slug": "unpublished-locally-remembered",
            "title": "No Longer Online", "bundle": {},
        },
    })
    monkeypatch.setattr(server, "_auth_cfg", lambda: {"url": "https://public", "key": "anon"})

    def fake_rest(cfg, method, path, payload=None, prefer=""):
        assert cfg["key"] == "anon"
        assert method == "GET"
        if path.startswith("volumes?"):
            return [{"slug": "same-slug", "title": "Cloud Work",
                     "group_id": "", "pdf_path": "same-slug/book file.pdf",
                     "thumbnail_path": "same-slug/cover.jpg"}]
        raise AssertionError(path)

    monkeypatch.setattr(server.sbase, "_rest", fake_rest)

    data = client.get("/api/publish/catalog").get_json()

    assert data["source"] == "cloud"
    assert "missing book-set metadata" in data["warning"]
    assert [row["slug"] for row in data["entries"]] == ["same-slug"]
    assert data["entries"][0]["group_id"] == ""
    assert "volume" not in data["entries"][0]
    assert "local_build_id" not in data["entries"][0]
    assert data["entries"][0]["pdf_url"] == (
        "https://public/storage/v1/object/public/volumes/"
        "same-slug/book%20file.pdf")
    assert data["entries"][0]["thumbnail_url"].endswith(
        "/volumes/same-slug/cover.jpg")


def test_publish_preview_does_not_fill_successful_cloud_read_from_local_bundle(
        client, tmp_path, monkeypatch):
    build = {"id": "local03", "status": "uploaded", "published_slug": "details",
             "title": "Details", "bundle": {"about": True, "annotations": True}}
    _build_file(tmp_path, monkeypatch, {"local03": build})
    monkeypatch.setattr(server, "_auth_cfg", lambda: {"url": "https://public", "key": "anon"})

    def fake_rest(_cfg, _method, path, payload=None, prefer=""):
        if path.startswith("volume_texts?"):
            return [{"body": "Remote About", "lang": ""}]
        if path.startswith("volume_notes?"):
            return []
        raise AssertionError(path)

    monkeypatch.setattr(server.sbase, "_rest", fake_rest)
    monkeypatch.setattr(server, "_read_entry_text", lambda bid, name: "Local About")
    monkeypatch.setattr(server, "_load_annotations", lambda bid: {"notes": [
        {"id": "n1", "page": 4, "body": "Local note", "status": "approved"},
        {"id": "n2", "page": 5, "body": "Draft", "status": "proposed"},
    ]})

    data = client.get("/api/publish/preview/details").get_json()

    assert data["about"] == "Remote About"
    assert data["notes"] == []
    assert data["source"] == "cloud"


def test_publish_preview_falls_back_to_local_bundle_after_cloud_error(
        client, tmp_path, monkeypatch):
    build = {"id": "local04", "status": "uploaded", "published_slug": "offline",
             "title": "Offline", "bundle": {"about": True, "annotations": True}}
    _build_file(tmp_path, monkeypatch, {"local04": build})
    monkeypatch.setattr(server, "_auth_cfg",
                        lambda: {"url": "https://public", "key": "anon"})
    monkeypatch.setattr(server.sbase, "_rest", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(server, "_read_entry_text",
                        lambda bid, name: "Local About")
    monkeypatch.setattr(server, "_load_annotations", lambda bid: {"notes": [
        {"id": "n1", "page": 4, "body": "Local note", "status": "approved"},
        {"id": "n2", "page": 5, "body": "Draft", "status": "proposed"},
    ]})

    data = client.get("/api/publish/preview/offline").get_json()

    assert data["source"] == "local"
    assert data["about"] == "Local About"
    assert data["notes"] == [{"body": "Local note", "kind": "", "note_id": "n1",
                               "page": 4, "quote": ""}]
    assert "offline" in data["warning"]
