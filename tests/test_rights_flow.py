"""The per-build rights decision: validation, the publish gate, and what of
the bundle a non-permitting decision lets out (docs/rights.md)."""
from __future__ import annotations

import json

import server


def _build_file(tmp_path, monkeypatch, builds):
    path = tmp_path / "builds.json"
    path.write_text(json.dumps(builds), encoding="utf-8")
    monkeypatch.setattr(server, "BUILDS_PATH", path)
    return path


def test_build_create_rejects_unknown_rights(client):
    res = client.post("/api/builds", json={"build": {
        "title": "A Work", "rights": "fair-use"}})

    assert res.status_code == 400
    assert "rights" in res.get_json()["error"]


def test_build_update_validates_rights(client):
    build = client.post("/api/builds", json={"build": {
        "title": "A Work"}}).get_json()["build"]
    assert build["rights"] == ""

    bad = client.patch(f"/api/builds/{build['id']}", json={"rights": "maybe"})
    assert bad.status_code == 400
    assert "rights" in bad.get_json()["error"]

    ok = client.patch(f"/api/builds/{build['id']}",
                      json={"rights": "public-domain"})
    assert ok.status_code == 200
    assert ok.get_json()["build"]["rights"] == "public-domain"


def test_publish_requires_a_rights_decision(client, tmp_path, monkeypatch):
    _build_file(tmp_path, monkeypatch, {"b1": {
        "id": "b1", "status": "ready", "title": "A Work", "pdf_file": "a.pdf"}})

    res = client.post("/api/volumes/publish", json={"build_id": "b1"})

    assert res.status_code == 400
    assert "rights" in res.get_json()["error"].lower()


def test_rights_artifacts_strips_text_for_searchable_only(monkeypatch):
    texts = {"about.md": "About body",
             "translations/es.txt": "--- page 1 ---\ntraduccion"}
    monkeypatch.setattr(server, "_read_entry_text",
                        lambda bid, rel: texts.get(rel, ""))
    monkeypatch.setattr(server, "_analyze_doc",
                        lambda bid, b: ("compiled.txt", "--- page 1 ---\nbody"))
    monkeypatch.setattr(server, "_load_annotations", lambda bid: {"notes": [
        {"id": "n1", "page": 1, "quote": "verbatim", "status": "approved"}]})
    b = {"bundle": {"about": True, "annotations": True, "pages_text": True,
                    "translations": ["es"]}}

    art, withheld = server._rights_artifacts("b1", dict(b, rights="searchable-only"))

    assert withheld is True
    assert art["pages"] == {} and art["notes"] == []
    assert art["assets"] == {"about": True}   # the curator's own writing stays
    assert art["about"] == "About body"

    art, withheld = server._rights_artifacts("b1", dict(b, rights="public-domain"))

    assert withheld is False
    assert art["pages"][""] == {1: "body"}
    assert art["pages"]["es"] == {1: "traduccion"}
    assert [n["id"] for n in art["notes"]] == ["n1"]


def test_volume_row_maps_rights_to_public_status():
    for rights, shown in (("public-domain", "Public domain"),
                          ("cleared", "Cleared"),
                          ("searchable-only", "Search only"),
                          ("no-public-text", "Restricted"),
                          ("", "")):
        row = server._volume_row({"title": "A Work", "rights": rights},
                                 "a-work", "", "", 0, "")
        assert row["copyright_status"] == shown
