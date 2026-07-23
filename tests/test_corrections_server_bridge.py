"""Production Flask composition for the Corrections artifact read bridge."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from PIL import Image


def _jpeg_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (7, 11), (38, 92, 57)).save(output, format="JPEG")
    return output.getvalue()


def _opaque_identity(namespace: str, *parts) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()[:40]}"


def _bind_engine_session(monkeypatch, server, session) -> None:
    aliases = {
        "_engine_session": session,
        "_engine_write_set": session.write_set,
        "_job_manager": session.jobs,
        "_translation_provenance": session.provenance,
        "_jobs": session.jobs.records,
        "_jobs_events": session.jobs.cancel_events,
        "_jobs_lock": session.jobs.lock,
        "_library_engine_instance": session.engine,
    }
    for name, value in aliases.items():
        monkeypatch.setattr(server, name, value)


@pytest.fixture()
def corrections_workspace(monkeypatch, tmp_path: Path):
    import server

    output = tmp_path / "output"
    builds_path = output / "whl_builds.json"
    entries_dir = output / "entries"
    captures_dir = tmp_path / "captures"
    capture_dir = captures_dir / "capture-1"
    capture_dir.mkdir(parents=True)
    entries_dir.mkdir(parents=True)

    content = _jpeg_bytes()
    digest = hashlib.sha256(content).hexdigest()
    (capture_dir / "original_asset-1.jpg").write_bytes(content)
    (capture_dir / "photo_1.jpg").write_bytes(content)
    manifest = {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": "capture-1",
        "legacy_fallback": False,
        "assets": [
            {
                "asset_id": "asset-1",
                "capture_order": 1,
                "capture_file": "photo_1.jpg",
                "original": {
                    "reference": "original_asset-1.jpg",
                    "sha256": digest,
                    "revision": 1,
                    "width": 7,
                    "height": 11,
                    "orientation": 0,
                },
                "display": {
                    "reference": "photo_1.jpg",
                    "sha256": digest,
                    "revision": 1,
                    "width": 7,
                    "height": 11,
                    "orientation": 0,
                    "recipe": "camera-original",
                    "recipe_version": "1",
                },
                "lifecycle": {"state": "completed"},
                "role": {
                    "manual_override": "cover",
                    "manual_revision": 1,
                    "manual_updated_at": 1,
                },
                "geometry": [],
                "processing_request": {},
            }
        ],
        "selections": {},
        "transport": {"representation": "original", "version": 1},
    }
    (capture_dir / "photo_assets.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    server.lib.save_json(
        builds_path,
        {
            "book-one": {
                "id": "book-one",
                "title": "Captured Herbal",
                "capture_id": "capture-1",
            }
        },
    )

    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures_dir)
    session = server._open_engine_session(output)
    _bind_engine_session(monkeypatch, server, session)
    try:
        yield content
    finally:
        session.close()


def test_production_bridge_lists_and_serves_capture_artifacts(
    client,
    corrections_workspace,
):
    collection = client.get(
        "/api/v1/items/book-one/raster-artifacts"
        "?representation_id=capture"
    )

    assert collection.status_code == 200
    body = collection.get_json()
    assert body["schema"] == "librarytool.raster-artifacts/1"
    capture_namespace = _opaque_identity("capture", "capture-1", "asset-1")
    display_id = f"{capture_namespace}:display"
    original_id = f"{capture_namespace}:original"
    assert [
        artifact["key"]["artifact_id"] for artifact in body["artifacts"]
    ] == [
        display_id,
        original_id,
    ]
    assert all(
        artifact["source"]["representation_id"] == "capture"
        for artifact in body["artifacts"]
    )
    display = body["artifacts"][0]
    assert display["effective_category"] == "cover"
    assert "captures" not in collection.get_data(as_text=True)

    resource = display["resource"]
    response = client.get(
        "/api/v1/items/book-one/raster-artifacts/"
        f"{display_id}/resource?"
        + urlencode({"revision": resource["revision"]})
    )

    assert response.status_code == 200
    assert response.data == corrections_workspace
    assert response.mimetype == "image/jpeg"
    assert response.headers["X-Resource-Revision"] == resource["revision"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
