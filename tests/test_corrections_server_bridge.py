"""Production Flask composition for the Corrections artifact read bridge."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest
from PIL import Image
from werkzeug.serving import make_server

from librarytool.adapters.filesystem import FilesystemCorrectionTransformStore
from librarytool.engine import CORRECTION_TRANSFORM_SERVICE
from librarytool.engine.correction_transforms import (
    OcrFollowupOutcome,
    OcrFollowupState,
)


BOOK_ID = "b-11111111111111111111111111111111"
CAPTURE_ID = "11111111-1111-4111-8111-111111111111"


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
    capture_dir = captures_dir / CAPTURE_ID
    capture_dir.mkdir(parents=True)
    entries_dir.mkdir(parents=True)

    content = _jpeg_bytes()
    digest = hashlib.sha256(content).hexdigest()
    (capture_dir / "original_asset-1.jpg").write_bytes(content)
    (capture_dir / "orig_1.jpg").write_bytes(content)
    (capture_dir / "photo_1.jpg").write_bytes(content)
    manifest = {
        "schema": "org.whl.bookcapture.photo-assets",
        "version": 1,
        "capture_id": CAPTURE_ID,
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
                "geometry": [
                    {
                        "asset_id": "asset-1",
                        "source_sha256": digest,
                        "source_revision": 1,
                        "display_revision": 1,
                        "coordinate_space": "display_normalized",
                        "width": 7,
                        "height": 11,
                        "orientation": 0,
                        "engine": "mistral",
                        "model": "mistral-ocr-latest",
                        "engine_version": "release-e2e",
                        "regions": [
                            {
                                "id": "margin-1",
                                "type": "text",
                                "text": "Materia medica",
                                "confidence": 0.97,
                                "polygon": [
                                    [0.05, 0.12],
                                    [0.31, 0.12],
                                    [0.31, 0.42],
                                    [0.05, 0.42],
                                ],
                            },
                            {
                                "id": "illustration-1",
                                "type": "image",
                                "text": "Botanical plate",
                                "confidence": 0.94,
                                "polygon": [
                                    [0.38, 0.18],
                                    [0.91, 0.18],
                                    [0.91, 0.88],
                                    [0.38, 0.88],
                                ],
                            },
                        ],
                    }
                ],
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
            BOOK_ID: {
                "id": BOOK_ID,
                "title": "Captured Herbal",
                "capture_id": CAPTURE_ID,
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
        f"/api/v1/items/{BOOK_ID}/raster-artifacts"
        "?representation_id=capture"
    )

    assert collection.status_code == 200
    body = collection.get_json()
    assert body["schema"] == "librarytool.raster-artifacts/1"
    capture_namespace = _opaque_identity("capture", CAPTURE_ID, "asset-1")
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
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/"
        f"{display_id}/resource?"
        + urlencode({"revision": resource["revision"]})
    )

    assert response.status_code == 200
    assert response.data == corrections_workspace
    assert response.mimetype == "image/jpeg"
    assert response.headers["X-Resource-Revision"] == resource["revision"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_production_bridge_mutations_converge_across_clients(
    client,
    corrections_workspace,
):
    del corrections_workspace
    capture_namespace = _opaque_identity("capture", CAPTURE_ID, "asset-1")
    display_id = f"{capture_namespace}:display"
    path = f"/api/v1/items/{BOOK_ID}/raster-artifacts/{display_id}/category"
    original = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/{display_id}"
    ).get_json()["artifact"]
    headers = {
        "Idempotency-Key": "bridge-category-op",
        "If-Artifact-Match": f'"{original["revision"]}"',
    }

    first = client.put(
        path,
        json={"category": "title_page"},
        headers=headers,
    )
    second_client = client.application.test_client()
    observed = second_client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/{display_id}"
    )
    stale = second_client.put(
        path,
        json={"category": "spine"},
        headers={
            "Idempotency-Key": "bridge-category-stale",
            "If-Artifact-Match": f'"{original["revision"]}"',
        },
    )
    replay = second_client.put(
        path,
        json={"category": "title_page"},
        headers=headers,
    )

    assert first.status_code == 200
    assert observed.status_code == 200
    assert observed.get_json()["artifact"]["effective_category"] == "title_page"
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "artifact_revision_conflict"
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["receipt"] == first.get_json()["receipt"]


def test_production_review_bridge_composes_index_and_reconciles_cas(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    monkeypatch.setattr(
        server,
        "_auth_doc",
        lambda: {"session": {"user_id": "user-bridge"}},
    )
    review_path = f"/api/v1/items/{BOOK_ID}/corrections/review"
    initial_detail = client.get(review_path)
    initial_index = client.get(
        "/api/v1/corrections/index?workspace_id=workspace-1"
    )

    assert initial_detail.status_code == 200
    assert initial_index.status_code == 200
    initial_review = initial_detail.get_json()["review"]
    index_body = initial_index.get_json()
    assert index_body["schema"] == "librarytool.corrections-index/1"
    assert [book["id"] for book in index_body["books"]] == [BOOK_ID]
    assert [
        capture["artifact_id"].endswith(":display")
        for capture in index_body["books"][0]["captures"]
    ] == [True]
    assert index_body["attention"] == []

    marked = client.put(
        f"{review_path}/attention",
        json={"reason": "Verify the cover", "comment": "Window one"},
        headers={
            "Idempotency-Key": "bridge-review-mark",
            "If-Review-Match": f'"{initial_review["revision"]}"',
        },
    )
    assert marked.status_code == 200

    second_client = client.application.test_client()
    observed = second_client.get(
        "/api/v1/corrections/index?workspace_id=workspace-1"
    ).get_json()
    attention = observed["attention"][0]
    assert attention["review"]["state"] == "needs_attention"
    assert attention["review"]["latest_event"]["actor_id"] == "user-bridge"

    resolved = second_client.post(
        f"{review_path}/resolve",
        json={"comment": "Window two verified it"},
        headers={
            "Idempotency-Key": "bridge-review-resolve",
            "If-Review-Match": f'"{attention["review"]["revision"]}"',
        },
    )
    stale = client.post(
        f"{review_path}/resolve",
        json={"comment": "Stale window"},
        headers={
            "Idempotency-Key": "bridge-review-stale",
            "If-Review-Match": f'"{attention["review"]["revision"]}"',
        },
    )
    reconciled = client.get(
        "/api/v1/corrections/index?workspace_id=workspace-1"
    ).get_json()

    assert resolved.status_code == 200
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "review_revision_conflict"
    assert reconciled["attention"][0]["review"]["state"] == "resolved"
    assert reconciled["attention"][0]["review"]["latest_event"][
        "actor_id"
    ] == "user-bridge"


def test_representative_flow_crosses_ui_client_flask_engine_and_worker(
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    class SuccessfulOcr:
        def run_ocr_followup(self, request, _hooks):
            return OcrFollowupOutcome(
                OcrFollowupState.SUCCEEDED,
                source=request.source,
                proposal_ref="ocr-proposal-release-e2e",
            )

    transform_service = server._library_engine().require_service(
        CORRECTION_TRANSFORM_SERVICE
    )
    transform_worker = getattr(transform_service._executor, "__self__", None)
    assert transform_worker is not None
    monkeypatch.setattr(transform_worker, "_ocr", SuccessfulOcr())

    commit_started = threading.Event()
    release_commit = threading.Event()
    original_commit = FilesystemCorrectionTransformStore.commit_transform

    def blocking_commit(instance, draft):
        commit_started.set()
        if not release_commit.wait(15):
            raise RuntimeError("release E2E did not reopen before commit")
        return original_commit(instance, draft)

    monkeypatch.setattr(
        FilesystemCorrectionTransformStore,
        "commit_transform",
        blocking_commit,
    )

    association = server._ensure_capture_archive(
        CAPTURE_ID,
        {
            "id": BOOK_ID,
            "book_id": BOOK_ID,
            "capture_id": CAPTURE_ID,
            "title": "Captured Herbal",
        },
    )
    assert association.capture_id == CAPTURE_ID
    assert association.book_id == BOOK_ID
    assert association.state.value == "current"

    class ReleaseHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
            if self.path != "/release":
                self.send_error(404)
                return
            release_commit.set()
            self.send_response(204)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    flask_server = make_server("127.0.0.1", 0, server.app, threaded=True)
    control_server = ThreadingHTTPServer(("127.0.0.1", 0), ReleaseHandler)
    flask_thread = threading.Thread(
        target=flask_server.serve_forever,
        daemon=True,
        name="corrections-release-e2e-flask",
    )
    control_thread = threading.Thread(
        target=control_server.serve_forever,
        daemon=True,
        name="corrections-release-e2e-control",
    )
    flask_thread.start()
    control_thread.start()
    node = shutil.which("node")
    assert node, "the release E2E requires the workflow's Node runtime"
    script = Path(__file__).with_name("corrections_live_bridge_e2e.test.js")
    environment = dict(os.environ)
    environment.update(
        {
            "WHL_CORRECTIONS_E2E_BASE_URL": (
                f"http://127.0.0.1:{flask_server.server_port}/api"
            ),
            "WHL_CORRECTIONS_E2E_CONTROL_URL": (
                f"http://127.0.0.1:{control_server.server_port}/release"
            ),
            "WHL_CORRECTIONS_E2E_ITEM_ID": BOOK_ID,
        }
    )
    try:
        completed = subprocess.run(
            [node, "--test", str(script)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    finally:
        release_commit.set()
        flask_server.shutdown()
        control_server.shutdown()
        flask_server.server_close()
        control_server.server_close()
        flask_thread.join(timeout=5)
        control_thread.join(timeout=5)

    assert completed.returncode == 0, (
        "live Corrections EngineClient flow failed\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    assert commit_started.is_set()
    assert server._capture_archive_association(CAPTURE_ID) == association


def test_production_bridge_preserves_not_found_semantics(
    client,
    corrections_workspace,
):
    del corrections_workspace
    collection = client.get("/api/v1/items/missing-book/raster-artifacts")
    mutation = client.put(
        "/api/v1/items/missing-book/raster-artifacts/missing-image/category",
        json={"category": "cover"},
        headers={
            "Idempotency-Key": "missing-category-op",
            "If-Artifact-Match": '"missing-r1"',
        },
    )

    assert collection.status_code == 404
    assert collection.get_json()["code"] == "item_not_found"
    assert mutation.status_code == 404
    assert mutation.get_json()["code"] == "item_not_found"
