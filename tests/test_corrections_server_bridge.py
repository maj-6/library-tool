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

from librarytool.adapters.filesystem import (
    FilesystemCorrectionTransformStore,
    FilesystemCorrectionsArtifactRepository,
    FilesystemItemQueryRepository,
)
from librarytool.engine import (
    CORRECTION_TRANSFORM_SERVICE,
    RASTER_ARTIFACT_QUERY_SERVICE,
    TEXT_LAYER_AGGREGATE_SERVICE,
)
from librarytool.engine.correction_projection import CorrectionProjectionService
from librarytool.engine.correction_transforms import (
    CorrectionTransformCommand,
    OcrFollowupOutcome,
    OcrFollowupState,
)
from librarytool.engine.items import ItemQueryService
from librarytool.engine.text_layer_aggregate import (
    CreateTextLayerCommand,
    TextLayerDraft,
    TextLayerProvenance,
    TextLayerSourcePin,
    TextLayerUnitDraft,
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


def _transform_command(source, operation_id: str) -> CorrectionTransformCommand:
    assert source.resource is not None
    return CorrectionTransformCommand(
        item_id=source.key.item_id,
        artifact_id=source.key.artifact_id,
        artifact_revision=source.revision,
        source_revision=source.resource.revision,
        source_sha256=source.content_sha256,
        quad=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
        adjustment=None,
        rerun_ocr=False,
        operation_id=operation_id,
    )


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


def _unsupported_uncaptured_manual_rows() -> dict:
    return {
        "legacy-valid": {
            "id": "legacy-valid",
            "title": ["unsupported", "legacy", "shape"],
            "images": "C:/private/legacy-photo.jpg",
            "extra": ["unsupported", "legacy", "metadata"],
        },
        "legacy row / invalid identity": [
            "non-object legacy row",
            {"path": "C:/private/legacy-source.pdf"},
        ],
    }


def _use_capture_only_target(server, *, title: str = "Captured Herbal") -> None:
    """Keep the canonical capture item while removing its promoted build."""

    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                "manual-capture": {
                    "id": "manual-capture",
                    "title": title,
                    "capture_id": CAPTURE_ID,
                    "images": [
                        f"captures/{CAPTURE_ID}/photo_1.jpg",
                    ],
                }
            },
        )


@pytest.fixture()
def corrections_workspace(monkeypatch, tmp_path: Path):
    import server

    output = tmp_path / "output"
    builds_path = output / "whl_builds.json"
    manual_entries_path = output / "manual_entries.json"
    entries_dir = output / "entries"
    captures_dir = tmp_path / "captures"
    capture_dir = captures_dir / CAPTURE_ID
    capture_dir.mkdir(parents=True)
    entries_dir.mkdir(parents=True)
    identity_dir = entries_dir / BOOK_ID / "ocr"
    identity_dir.mkdir(parents=True)
    server.lib.save_json(identity_dir / "lib-id.json", {"book_id": BOOK_ID})

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
        "desktop_import": {
            "version": 1,
            "imported_at": "2026-08-04T12:34:56Z",
            "assets": [
                {
                    "order": 0,
                    "asset_id": "asset-1",
                    "raw_ref": "orig_1.jpg",
                    "display_ref": "photo_1.jpg",
                    "source_checksum": digest,
                    "derivative_checksum": digest,
                    "transport_representation": "original",
                    "recipe": "desktop_perspective_standardize_v1",
                    "lifecycle": "completed",
                }
            ],
        },
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
    server.lib.save_json(manual_entries_path, {})

    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures_dir)
    monkeypatch.setattr(
        server,
        "CAPTURE_CLOUD_ASSOCIATION_STATE_PATH",
        output / "capture_cloud_association_state.json",
    )
    monkeypatch.setattr(
        server.lib,
        "MANUAL_ENTRIES_PATH",
        manual_entries_path,
    )
    session = server._open_engine_session(output)
    _bind_engine_session(monkeypatch, server, session)
    server._ensure_capture_archive(
        CAPTURE_ID,
        {
            "id": BOOK_ID,
            "book_id": BOOK_ID,
            "capture_id": CAPTURE_ID,
            "title": "Captured Herbal",
        },
    )
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


def test_corrections_projection_keeps_runtime_workbench_policies(
    corrections_workspace,
):
    del corrections_workspace
    import server

    native = server._item_engine()
    corrections = server._corrections_item_engine()
    assert [policy.policy_id for policy in corrections.policies] == [
        policy.policy_id for policy in native.policies
    ]
    assert corrections.policies

    native_state = native.get_item(BOOK_ID).workbench_state
    corrections_state = corrections.get_item(BOOK_ID).workbench_state
    assert corrections_state.readiness == native_state.readiness
    assert corrections_state.available_commands == (
        native_state.available_commands
    )


def test_capture_only_target_is_visible_and_stays_canonical_after_promotion(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    manual = {
        "id": manual_id,
        "title": "Manual Capture",
        "author": "A. Botanist",
        "notes": "First observation",
        "capture_id": CAPTURE_ID,
        "images": [f"captures/{CAPTURE_ID}/photo_1.jpg"],
    }
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {manual_id: manual},
        )

    first = server._corrections_item_snapshot()
    index_response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    item = first[BOOK_ID]
    query_item = ItemQueryService(
        FilesystemItemQueryRepository(lambda: first)
    ).get_item(BOOK_ID)
    rasters = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts"
        "?representation_id=capture"
    )

    assert list(first) == [BOOK_ID]
    assert index_response.status_code == 200
    assert [
        (row["id"], row["kind"])
        for row in index_response.get_json()["books"]
    ] == [(BOOK_ID, "capture")]
    assert item["kind"] == "capture"
    assert item["title"] == "Manual Capture"
    assert query_item.kind == "capture"
    assert query_item.metadata["authors"] == "A. Botanist"
    assert item["metadata"]["origin"] == "captured_entry"
    assert item["metadata"]["association_state"] == "current"
    assert item["metadata"]["active_storage_kind"] == "manual"
    assert item["metadata"]["active_storage_id"].startswith("manual:")
    assert rasters.status_code == 200
    assert len(rasters.get_json()["artifacts"]) == 2
    display = rasters.get_json()["artifacts"][0]
    classified = client.put(
        (
            f"/api/v1/items/{BOOK_ID}/raster-artifacts/"
            f"{display['key']['artifact_id']}/category"
        ),
        json={"category": "title_page"},
        headers={
            "Idempotency-Key": "capture-only-title-page",
            "If-Artifact-Match": f'"{display["revision"]}"',
        },
    )
    assert classified.status_code == 200
    placeholder = server._corrections_entry_directory(BOOK_ID)
    assert placeholder == (
        server.ENTRIES_DIR
        / server._CORRECTIONS_CAPTURE_ONLY_DIRECTORY
        / BOOK_ID
    )
    assert not placeholder.exists()

    manual["notes"] = "Corrected without an updated_at token"
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {manual_id: manual},
        )
    second = server._corrections_item_snapshot()
    assert second[BOOK_ID]["revision"] != item["revision"]

    active_entry = server.ENTRIES_DIR / "promoted-local"
    (active_entry / "ocr").mkdir(parents=True)
    (active_entry / "ocr" / "layout.json").write_text(
        json.dumps(
            {
                "regions": {
                    "primary": {
                        "1": {
                            "doc": "compiled.txt",
                            "dims": {"w": 100, "h": 200},
                            "origin": "machine",
                            "items": [
                                {
                                    "id": "r1",
                                    "rid": "margin-1",
                                    "role": "marginalia",
                                    "order": 0,
                                    "box": {
                                        "x": 0.1,
                                        "y": 0.2,
                                        "w": 0.3,
                                        "h": 0.1,
                                    },
                                    "text": "Handwritten note",
                                }
                            ],
                        }
                    }
                },
                "images": {},
            }
        ),
        encoding="utf-8",
    )
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": "Promoted Build Wins",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                    "pdf_file": "missing-source.pdf",
                }
            },
        )
    promoted = server._corrections_item_snapshot()

    assert list(promoted) == [BOOK_ID]
    assert promoted[BOOK_ID]["kind"] == "book"
    assert promoted[BOOK_ID]["title"] == "Promoted Build Wins"
    assert promoted[BOOK_ID]["metadata"]["origin"] == "promoted_capture"
    assert promoted[BOOK_ID]["metadata"]["active_storage_kind"] == "build"
    assert promoted[BOOK_ID]["metadata"]["active_storage_id"] == (
        "promoted-local"
    )
    assert server._corrections_entry_directory(BOOK_ID) == (
        server.ENTRIES_DIR / "promoted-local"
    )
    promoted_rasters = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts"
        "?representation_id=capture"
    ).get_json()["artifacts"]
    assert promoted_rasters[0]["effective_category"] == "title_page"
    boxes = client.get(
        f"/api/v1/items/{BOOK_ID}/spatial-annotations"
        "?representation_id=primary"
    )
    assert boxes.status_code == 200
    assert boxes.get_json()["total"] == 1
    assert boxes.get_json()["annotations"][0]["effective_role"] == (
        "marginalia"
    )


def test_corrections_index_resolves_capture_authority_once_per_capture(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    capture_ids = tuple(
        f"00000000-0000-4000-8000-{index:012d}"
        for index in range(1, 7)
    )
    builds = {
        f"bulk-capture-{index}": {
            "id": f"bulk-capture-{index}",
            "title": f"Bulk capture {index}",
            "capture_id": capture_id,
        }
        for index, capture_id in enumerate(capture_ids, start=1)
    }
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, builds)
    with server._manual_lock:
        server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, {})

    original = (
        server.FilesystemCaptureArchiveRepository.inspect_association_identity
    )
    inspected: list[str] = []

    def inspect_once(_cls, workspace_root, capture_id):
        inspected.append(capture_id)
        return original(workspace_root, capture_id)

    monkeypatch.setattr(
        server.FilesystemCaptureArchiveRepository,
        "inspect_association_identity",
        classmethod(inspect_once),
    )
    monkeypatch.setattr(
        server.FilesystemCaptureArchiveRepository,
        "inspect_association",
        classmethod(
            lambda _cls, _workspace_root, _capture_id: pytest.fail(
                "the Corrections index hashed a capture archive payload"
            )
        ),
    )

    first = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert first.status_code == 200
    assert len(first.get_json()["books"]) == len(capture_ids)
    assert inspected == list(capture_ids)

    second = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert second.status_code == 200
    assert len(second.get_json()["books"]) == len(capture_ids)
    # A fresh request gets a fresh bulk snapshot; nothing is cached globally.
    assert inspected == [*capture_ids, *capture_ids]


def test_capture_only_transform_loads_and_queues_without_native_text_layers(
    corrections_workspace,
):
    del corrections_workspace
    import server

    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                "manual-capture": {
                    "id": "manual-capture",
                    "title": "Capture Awaiting Promotion",
                    "capture_id": CAPTURE_ID,
                }
            },
        )

    engine = server._library_engine()
    raster = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts(BOOK_ID)
        if value.key.artifact_id.endswith(":display")
    )
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    worker = transforms._executor.__self__

    loaded = worker._store.load_source(source.key)
    command = _transform_command(
        source,
        "capture-only-canonical-transform",
    )
    queued = transforms.queue(command)
    result = transforms.execute_queued(command)

    assert server._corrections_text_layer_item_id(BOOK_ID) is None
    assert loaded.human_text_assertions == ()
    assert queued.created is True
    assert result.image_commit is not None


def test_promoted_transform_uses_active_build_text_layers_under_canonical_id(
    corrections_workspace,
):
    del corrections_workspace
    import server

    entry = server.ENTRIES_DIR / "promoted-local"
    images = entry / "ocr" / "images"
    images.mkdir(parents=True)
    (images / "figure.jpg").write_bytes(_jpeg_bytes())
    (entry / "ocr" / "layout.json").write_text(
        json.dumps(
            {
                "regions": {
                    "primary": {
                        "1": {
                            "doc": "compiled.txt",
                            "dims": {"w": 7, "h": 11},
                            "origin": "machine",
                            "items": [],
                        }
                    }
                },
                "images": {
                    "figure.jpg": {
                        "src_key": "primary",
                        "page": 1,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with server._manual_lock:
        server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, {})
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": "Promoted Capture",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                    "pdf_file": "missing-source.pdf",
                }
            },
        )

    representation_revision = server._engine_representation_revision(
        "promoted-local",
        "primary",
    )
    assert representation_revision is not None
    engine = server._library_engine()
    text_layers = engine.require_service(TEXT_LAYER_AGGREGATE_SERVICE)
    text_layers.create(
        CreateTextLayerCommand(
            "promoted-local",
            TextLayerDraft(
                source=TextLayerSourcePin(
                    "primary",
                    representation_revision,
                ),
                units=(
                    TextLayerUnitDraft(
                        "verified-caption",
                        0,
                        "Human verified plate",
                        provenance=TextLayerProvenance(
                            origin="human",
                            review_state="approved",
                        ),
                    ),
                ),
                label="Verified",
                language="en",
            ),
            "promoted-native-text-layer",
        )
    )
    raster = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    source = next(
        value
        for value in raster.list_raster_artifacts(BOOK_ID)
        if value.kind == "extracted-figure"
    )
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    worker = transforms._executor.__self__

    loaded = worker._store.load_source(source.key)
    command = _transform_command(
        source,
        "promoted-canonical-transform",
    )
    queued = transforms.queue(command)
    result = transforms.execute_queued(command)

    assert server._corrections_text_layer_item_id(BOOK_ID) == (
        "promoted-local"
    )
    assert [value.text for value in loaded.human_text_assertions] == [
        "Human verified plate"
    ]
    assert queued.created is True
    assert result.image_commit is not None


def test_capture_only_metadata_edit_is_conditional_idempotent_and_path_free(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                manual_id: {
                    "id": manual_id,
                    "title": "Captured Herbal",
                    "author": "A. Botanist",
                    "city": "Bath",
                    "year": "1799",
                    "capture_id": CAPTURE_ID,
                    "images": [
                        f"captures/{CAPTURE_ID}/private-photo.jpg"
                    ],
                    "local_pdf": "C:/private/capture.pdf",
                    "extra": {
                        "scan_collection_id": "private-collection",
                        "generated": {
                            "caption": "Automatically assigned caption",
                            "local_path": "C:/private/plate.jpg",
                        },
                    },
                }
            },
        )

    endpoint = f"/api/v1/corrections/items/{BOOK_ID}"
    detail = client.get(endpoint)
    assert detail.status_code == 200
    before = detail.get_json()["item"]
    assert before["kind"] == "capture"
    assert before["metadata"]["authors"] == "A. Botanist"
    assert before["metadata"]["publisher_city"] == "Bath"
    assert before["metadata"]["extra"] == {
        "generated": {
            "caption": "Automatically assigned caption",
        },
    }
    assert "local_pdf" not in detail.get_data(as_text=True)
    assert "private-photo.jpg" not in detail.get_data(as_text=True)
    assert "private-collection" not in detail.get_data(as_text=True)
    assert "private/plate.jpg" not in detail.get_data(as_text=True)

    document = {
        "patch": {
            "title": "Captured Herbal, corrected",
            "metadata_set": {
                "authors": "B. Botanist",
                "publisher_city": "Edinburgh",
                "condition": "Good",
                "extra": {
                    "generated": {
                        "caption": "Corrected botanical caption",
                    },
                },
            },
            "metadata_remove": ["year"],
        }
    }
    headers = {
        "Idempotency-Key": "bridge-capture-metadata-edit",
        "If-Record-Match": f'"{before["record_revision"]}"',
    }
    updated = client.patch(endpoint, json=document, headers=headers)
    replay = client.patch(endpoint, json=document, headers=headers)

    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["replayed"] is False
    after = updated.get_json()["item"]
    assert after["id"] == BOOK_ID
    assert after["title"] == "Captured Herbal, corrected"
    assert after["metadata"]["authors"] == "B. Botanist"
    assert after["metadata"]["publisher_city"] == "Edinburgh"
    assert after["metadata"]["condition"] == "Good"
    assert after["metadata"]["extra"] == {
        "generated": {
            "caption": "Corrected botanical caption",
        },
    }
    assert "year" not in after["metadata"]
    assert after["record_revision"] != before["record_revision"]
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["item"] == after

    with server._manual_lock:
        stored = server.lib.load_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {},
        )[manual_id]
    assert stored["title"] == "Captured Herbal, corrected"
    assert stored["author"] == "B. Botanist"
    assert stored["city"] == "Edinburgh"
    assert "year" not in stored
    assert "authors" not in stored
    assert "publisher_city" not in stored
    assert stored["capture_id"] == CAPTURE_ID
    assert stored["images"] == [
        f"captures/{CAPTURE_ID}/private-photo.jpg"
    ]
    assert stored["local_pdf"] == "C:/private/capture.pdf"
    assert stored["extra"] == {
        "scan_collection_id": "private-collection",
        "generated": {
            "caption": "Corrected botanical caption",
            "local_path": "C:/private/plate.jpg",
        },
    }

    second_client = client.application.test_client()
    stale = second_client.patch(
        endpoint,
        json={
            "patch": {
                "title": None,
                "metadata_set": {"condition": "Poor"},
                "metadata_remove": [],
            }
        },
        headers={
            "Idempotency-Key": "bridge-capture-metadata-stale",
            "If-Record-Match": f'"{before["record_revision"]}"',
        },
    )
    managed = second_client.patch(
        endpoint,
        json={
            "patch": {
                "title": None,
                "metadata_set": {"capture_id": "another-capture"},
                "metadata_remove": [],
            }
        },
        headers={
            "Idempotency-Key": "bridge-capture-metadata-managed",
            "If-Record-Match": f'"{after["record_revision"]}"',
        },
    )
    identity_claim = second_client.patch(
        endpoint,
        json={
            "patch": {
                "title": None,
                "metadata_set": {
                    "extra": {
                        "book_id": (
                            "b-22222222222222222222222222222222"
                        )
                    }
                },
                "metadata_remove": [],
            }
        },
        headers={
            "Idempotency-Key": "bridge-capture-metadata-identity",
            "If-Record-Match": f'"{after["record_revision"]}"',
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "item_revision_conflict"
    assert managed.status_code == 400
    assert managed.get_json()["code"] == "managed_item_fields_not_writable"
    assert managed.get_json()["details"]["fields"] == ["capture_id"]
    assert identity_claim.status_code == 400
    assert identity_claim.get_json()["code"] == (
        "managed_item_fields_not_writable"
    )
    assert identity_claim.get_json()["details"]["fields"] == [
        "extra.book_id"
    ]

    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": after["title"],
                    "authors": after["metadata"]["authors"],
                    "publisher_city": (
                        after["metadata"]["publisher_city"]
                    ),
                    "condition": after["metadata"]["condition"],
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                }
            },
        )
        promoted_before = server.lib.load_json(
            server.BUILDS_PATH,
            {},
        )["promoted-local"]
    promoted_replay = second_client.patch(
        endpoint,
        json=document,
        headers=headers,
    )
    assert promoted_replay.status_code == 200, promoted_replay.get_json()
    assert promoted_replay.get_json()["replayed"] is True
    assert promoted_replay.get_json()["item"]["id"] == BOOK_ID
    with server._builds_lock:
        promoted_after = server.lib.load_json(
            server.BUILDS_PATH,
            {},
        )["promoted-local"]
    assert promoted_after == promoted_before


def test_capture_only_patch_ignores_unsupported_uncaptured_manual_rows(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    excluded = _unsupported_uncaptured_manual_rows()
    rows = {
        manual_id: {
            "id": manual_id,
            "title": "Captured title",
            "capture_id": CAPTURE_ID,
        },
        **excluded,
    }
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, rows)

    endpoint = f"/api/v1/corrections/items/{BOOK_ID}"
    before = client.get(endpoint).get_json()["item"]
    response = client.patch(
        endpoint,
        json={
            "patch": {
                "title": "Corrected captured title",
                "metadata_set": {},
                "metadata_remove": [],
            }
        },
        headers={
            "Idempotency-Key": "bridge-capture-excluded-manual-rows",
            "If-Record-Match": f'"{before["record_revision"]}"',
        },
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["replayed"] is False
    with server._manual_lock:
        stored = server.lib.load_json(server.lib.MANUAL_ENTRIES_PATH, {})
    assert stored[manual_id]["title"] == "Corrected captured title"
    for entry_id, expected in excluded.items():
        assert stored[entry_id] == expected


def test_promoted_patch_and_replay_ignore_unsupported_uncaptured_manual_rows(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    source = {
        "id": manual_id,
        "title": "Compatibility source",
        "capture_id": CAPTURE_ID,
    }
    excluded = _unsupported_uncaptured_manual_rows()
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {manual_id: source, **excluded},
        )
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": "Promoted title",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                }
            },
        )

    endpoint = f"/api/v1/corrections/items/{BOOK_ID}"
    before = client.get(endpoint).get_json()["item"]
    document = {
        "patch": {
            "title": "Corrected promoted title",
            "metadata_set": {},
            "metadata_remove": [],
        }
    }
    headers = {
        "Idempotency-Key": "bridge-promoted-excluded-manual-rows",
        "If-Record-Match": f'"{before["record_revision"]}"',
    }
    updated = client.patch(endpoint, json=document, headers=headers)
    replay = client.patch(endpoint, json=document, headers=headers)

    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["replayed"] is False
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["replayed"] is True
    assert replay.get_json()["item"] == updated.get_json()["item"]
    with server._builds_lock:
        build = server.lib.load_json(server.BUILDS_PATH, {})[
            "promoted-local"
        ]
    with server._manual_lock:
        stored = server.lib.load_json(server.lib.MANUAL_ENTRIES_PATH, {})
    assert build["title"] == "Corrected promoted title"
    assert stored[manual_id] == source
    for entry_id, expected in excluded.items():
        assert stored[entry_id] == expected


def test_promoted_capture_metadata_edit_targets_only_the_active_build(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    manual = {
        "id": manual_id,
        "title": "Compatibility source",
        "author": "Original author",
        "capture_id": CAPTURE_ID,
    }
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {manual_id: manual},
        )
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": "Promoted title",
                    "authors": "Build author",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                }
            },
        )

    endpoint = f"/api/v1/corrections/items/{BOOK_ID}"
    before = client.get(endpoint).get_json()["item"]
    response = client.patch(
        endpoint,
        json={
            "patch": {
                "title": "Corrected promoted title",
                "metadata_set": {"authors": "Corrected build author"},
                "metadata_remove": [],
            }
        },
        headers={
            "Idempotency-Key": "bridge-promoted-metadata-edit",
            "If-Record-Match": f'"{before["record_revision"]}"',
        },
    )

    assert response.status_code == 200, response.get_json()
    item = response.get_json()["item"]
    assert item["id"] == BOOK_ID
    assert item["kind"] == "book"
    assert item["title"] == "Corrected promoted title"
    assert item["metadata"]["authors"] == "Corrected build author"
    with server._builds_lock:
        build = server.lib.load_json(server.BUILDS_PATH, {})[
            "promoted-local"
        ]
    with server._manual_lock:
        source = server.lib.load_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {},
        )[manual_id]
    assert build["title"] == "Corrected promoted title"
    assert build["authors"] == "Corrected build author"
    assert source == manual


def test_promoted_metadata_edit_does_not_commit_after_authority_conflict(
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                manual_id: {
                    "id": manual_id,
                    "title": "Capture source",
                    "capture_id": CAPTURE_ID,
                }
            },
        )
    original = {
        "id": "promoted-a",
        "title": "Promoted A",
        "capture_id": CAPTURE_ID,
        "capture_book_id": BOOK_ID,
    }
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {"promoted-a": original},
        )
    before = server._corrections_item_snapshot()[BOOK_ID]
    entered = threading.Event()
    release = threading.Event()

    def pause_before_command(_command):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(
        server,
        "_corrections_item_invalidate_capture",
        pause_before_command,
    )
    result = {}

    def run_edit():
        with server.app.test_client() as edit_client:
            response = edit_client.patch(
                f"/api/v1/corrections/items/{BOOK_ID}",
                json={
                    "patch": {
                        "title": "Must not commit",
                        "metadata_set": {},
                        "metadata_remove": [],
                    }
                },
                headers={
                    "Idempotency-Key": (
                        "bridge-promoted-authority-conflict"
                    ),
                    "If-Record-Match": (
                        f'"{before["revision"]}"'
                    ),
                },
            )
            result["status"] = response.status_code
            result["body"] = response.get_json()

    worker = threading.Thread(target=run_edit)
    worker.start()
    assert entered.wait(5)
    with server._builds_lock:
        builds = server.lib.load_json(server.BUILDS_PATH, {})
        builds["promoted-b"] = {
            "id": "promoted-b",
            "title": "Conflicting authority",
            "capture_id": CAPTURE_ID,
            "capture_book_id": BOOK_ID,
        }
        server.lib.save_json(server.BUILDS_PATH, builds)
    release.set()
    worker.join(5)

    assert not worker.is_alive()
    assert result["status"] == 500
    assert result["body"]["code"] == "correction_item_update_unavailable"
    with server._builds_lock:
        stored = server.lib.load_json(server.BUILDS_PATH, {})
    assert stored["promoted-a"] == original


def test_capture_metadata_edit_does_not_commit_when_archive_invalidation_fails(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    manual_id = "manual-capture"
    manual = {
        "id": manual_id,
        "title": "Captured title",
        "capture_id": CAPTURE_ID,
    }
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {manual_id: manual},
        )
    endpoint = f"/api/v1/corrections/items/{BOOK_ID}"
    before = client.get(endpoint).get_json()["item"]

    def fail_invalidation(_capture_id):
        raise server.EngineRepositoryError(
            "archive store unavailable",
            code="capture_archive_repository_unavailable",
            retryable=True,
        )

    monkeypatch.setattr(
        server,
        "_mark_capture_archive_stale",
        fail_invalidation,
    )
    response = client.patch(
        endpoint,
        json={
            "patch": {
                "title": "Must not commit",
                "metadata_set": {},
                "metadata_remove": [],
            }
        },
        headers={
            "Idempotency-Key": "bridge-capture-invalidation-failure",
            "If-Record-Match": f'"{before["record_revision"]}"',
        },
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "correction_item_update_unavailable"
    with server._manual_lock:
        stored = server.lib.load_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {},
        )[manual_id]
    assert stored == manual


def test_capture_target_duplicates_and_identity_conflicts_fail_closed(
    corrections_workspace,
):
    del corrections_workspace
    import server

    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                "manual-one": {
                    "title": "First",
                    "capture_id": CAPTURE_ID,
                },
                "manual-two": {
                    "title": "Second",
                    "capture_id": CAPTURE_ID,
                },
            },
        )

    with pytest.raises(server.EngineRepositoryError) as duplicate:
        server._corrections_item_snapshot()
    assert duplicate.value.code == "duplicate_corrections_target_claim"

    with server._manual_lock:
        server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, {})
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "conflicting-build": {
                    "id": "conflicting-build",
                    "title": "Conflict",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": (
                        "b-22222222222222222222222222222222"
                    ),
                }
            },
        )

    with pytest.raises(server.EngineRepositoryError) as conflict:
        server._corrections_item_snapshot()
    assert conflict.value.code == "corrections_target_identity_conflict"


def test_capture_fallback_is_deterministic_and_uncaptured_manual_is_omitted(
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    persisted_association = server._corrections_association(CAPTURE_ID)
    assert persisted_association is not None
    assert persisted_association.book_id == BOOK_ID
    monkeypatch.setattr(
        server,
        "_corrections_association",
        lambda _value: None,
    )
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                "manual-capture": {
                    "id": "manual-capture",
                    "title": "Unassociated Capture",
                    "capture_id": CAPTURE_ID,
                }
            },
        )

    capture_snapshot = server._corrections_item_snapshot()
    capture_identity = server.capture_book_id(CAPTURE_ID)
    assert list(capture_snapshot) == [capture_identity]
    assert capture_snapshot[capture_identity]["metadata"][
        "association_state"
    ] == "missing"

    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                "manual-capture": {
                    "id": "manual-capture",
                    "title": "Legacy identity capture",
                    "capture_id": CAPTURE_ID,
                    "extra": {"lib_book_id": BOOK_ID},
                }
            },
        )
    legacy_snapshot = server._corrections_item_snapshot()
    assert list(legacy_snapshot) == [BOOK_ID]
    assert legacy_snapshot[BOOK_ID]["metadata"][
        "association_state"
    ] == "missing"
    monkeypatch.setattr(
        server,
        "_corrections_association",
        lambda _value: persisted_association,
    )
    associated_snapshot = server._corrections_item_snapshot()
    assert list(associated_snapshot) == [BOOK_ID]
    assert associated_snapshot[BOOK_ID]["metadata"][
        "association_state"
    ] == "current"

    monkeypatch.setattr(
        server,
        "_corrections_association",
        lambda _value: None,
    )
    with server._manual_lock:
        server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, {})
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": "Promoted legacy identity",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                }
            },
        )
    promoted_snapshot = server._corrections_item_snapshot()
    assert list(promoted_snapshot) == [BOOK_ID]
    assert promoted_snapshot[BOOK_ID]["metadata"][
        "association_state"
    ] == "missing"

    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                "legacy manual/key": {
                    "title": "Uncaptured Manual",
                },
                "capture-alias": {
                    "title": "Aliased Capture",
                    "capture_id": (
                        "{11111111-1111-4111-8111-111111111111}"
                    ),
                }
            },
        )
    manual_snapshot = server._corrections_item_snapshot()
    assert manual_snapshot == {}


def test_manual_storage_alias_is_private_and_invalid_unicode_fails_closed(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    raw_manual_id = "../private/" + ("secret-alias-" * 40)
    with server._builds_lock:
        server.lib.save_json(server.BUILDS_PATH, {})
    with server._manual_lock:
        server.lib.save_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {
                raw_manual_id: {
                    "title": "Legacy Capture",
                    "capture_id": CAPTURE_ID,
                }
            },
        )
    snapshot = server._corrections_item_snapshot()
    serialized = json.dumps(snapshot, sort_keys=True)

    assert list(snapshot) == [BOOK_ID]
    assert raw_manual_id not in serialized
    assert snapshot[BOOK_ID]["metadata"]["active_storage_id"].startswith(
        "manual:"
    )
    endpoint = f"/api/v1/corrections/items/{BOOK_ID}"
    before = client.get(endpoint).get_json()["item"]
    document = {
        "patch": {
            "title": "Corrected legacy capture",
            "metadata_set": {},
            "metadata_remove": [],
        }
    }
    headers = {
        "Idempotency-Key": "legacy-storage-alias-edit",
        "If-Record-Match": f'"{before["record_revision"]}"',
    }
    edited = client.patch(endpoint, json=document, headers=headers)
    assert edited.status_code == 200, edited.get_json()
    assert edited.get_json()["item"]["title"] == (
        "Corrected legacy capture"
    )
    with server._manual_lock:
        stored = server.lib.load_json(
            server.lib.MANUAL_ENTRIES_PATH,
            {},
        )
    assert list(stored) == [raw_manual_id]
    assert stored[raw_manual_id]["title"] == "Corrected legacy capture"

    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "promoted-local": {
                    "id": "promoted-local",
                    "title": "Corrected legacy capture",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                }
            },
        )
    replay = client.patch(endpoint, json=document, headers=headers)
    assert replay.status_code == 200, replay.get_json()
    assert replay.get_json()["replayed"] is True
    assert raw_manual_id not in replay.get_data(as_text=True)

    real_load_json = server.lib.load_json

    def invalid_unicode_snapshot(path, default):
        if Path(path) == Path(server.BUILDS_PATH):
            return {}
        if Path(path) == Path(server.lib.MANUAL_ENTRIES_PATH):
            return {
                "\ud800": {
                    "title": "Invalid Unicode",
                    "capture_id": CAPTURE_ID,
                }
            }
        return real_load_json(path, default)

    monkeypatch.setattr(server.lib, "load_json", invalid_unicode_snapshot)
    with pytest.raises(server.EngineRepositoryError) as invalid:
        server._corrections_item_snapshot()
    assert invalid.value.code == "invalid_corrections_target_snapshot"
    assert "\ud800" not in json.dumps(invalid.value.details)


def test_build_id_text_layer_source_resolution_is_not_canonicalized(
    corrections_workspace,
):
    del corrections_workspace
    import server

    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "local-build": {
                    "id": "local-build",
                    "title": "Captured Build",
                    "capture_id": CAPTURE_ID,
                    "capture_book_id": BOOK_ID,
                    "pdf_file": "missing-source.pdf",
                }
            },
        )

    source = server._engine_text_layer_source_snapshot(
        "local-build",
        "primary",
    )

    assert source is not None
    assert source.item_id == "local-build"
    assert source.representation_id == "primary"
    assert source.revision.startswith("sr-")


def test_normal_build_only_corrections_target_stays_addressable(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    with server._manual_lock:
        server.lib.save_json(server.lib.MANUAL_ENTRIES_PATH, {})
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "plain-book": {
                    "id": "plain-book",
                    "title": "Plain Book",
                }
            },
        )

    snapshot = server._corrections_item_snapshot()
    response = client.get("/api/v1/items/plain-book/raster-artifacts")

    assert list(snapshot) == ["plain-book"]
    assert snapshot["plain-book"]["title"] == "Plain Book"
    assert snapshot["plain-book"]["metadata"]["origin"] == "catalogue"
    assert response.status_code == 200
    assert response.get_json()["artifacts"] == []


def test_uncorrected_build_index_never_reads_capture_or_layout_bytes(
    client,
    corrections_workspace,
    monkeypatch,
):

    monkeypatch.setattr(
        FilesystemCorrectionsArtifactRepository,
        "_observe_resource",
        lambda *_args, **_kwargs: pytest.fail(
            "an uncorrected build index inspected capture image bytes"
        ),
    )
    monkeypatch.setattr(
        CorrectionProjectionService,
        "get_correction_review",
        lambda *_args, **_kwargs: pytest.fail(
            "an uncorrected build index loaded a correction aggregate"
        ),
    )

    response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200
    books = response.get_json()["books"]
    assert [book["id"] for book in books] == [BOOK_ID]
    assert len(books[0]["captures"]) == 1
    assert books[0]["captures"][0]["revision"].startswith("index:")
    assert books[0]["captures"][0]["thumbnail"]["url"].endswith("/preview")
    assert books[0]["captures"][0]["imported_at"] == (
        "2026-08-04T12:34:56Z"
    )
    assert books[0]["latest_imported_at"] == "2026-08-04T12:34:56Z"


def test_lazy_and_full_indexes_match_missing_original_and_import_time(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    (server.CAPTURES_DIR / CAPTURE_ID / "orig_1.jpg").unlink()

    lazy = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    monkeypatch.setattr(
        server,
        "_corrections_uses_lazy_capture_index",
        lambda _item_id: False,
    )
    full = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert lazy.status_code == full.status_code == 200
    lazy_book = lazy.get_json()["books"][0]
    full_book = full.get_json()["books"][0]
    for field in ("import_state", "resource_state", "imported_at"):
        assert lazy_book["captures"][0][field] == full_book["captures"][0][field]
    assert lazy_book["captures"][0]["import_state"] == "partial"
    assert lazy_book["latest_imported_at"] == full_book["latest_imported_at"]
    assert lazy_book["latest_imported_at"] == "2026-08-04T12:34:56Z"


def test_lazy_and_full_indexes_match_legacy_capture_state(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    (server.CAPTURES_DIR / CAPTURE_ID / "photo_assets.json").unlink()

    lazy = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    monkeypatch.setattr(
        server,
        "_corrections_uses_lazy_capture_index",
        lambda _item_id: False,
    )
    full = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert lazy.status_code == full.status_code == 200
    lazy_capture = lazy.get_json()["books"][0]["captures"][0]
    full_capture = full.get_json()["books"][0]["captures"][0]
    assert lazy_capture["import_state"] == "legacy"
    assert full_capture["import_state"] == "legacy"
    assert lazy_capture["resource_state"] == full_capture["resource_state"]
    assert lazy_capture["imported_at"] == full_capture["imported_at"] == ""


def test_lazy_and_full_indexes_match_malformed_legacy_geometry(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    manifest_path = server.CAPTURES_DIR / CAPTURE_ID / "photo_assets.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["legacy_fallback"] = True
    manifest.pop("desktop_import")
    manifest["assets"][0]["geometry"] = ["invalid-geometry"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    lazy = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    monkeypatch.setattr(
        server,
        "_corrections_uses_lazy_capture_index",
        lambda _item_id: False,
    )
    full = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert lazy.status_code == full.status_code == 200
    lazy_book = lazy.get_json()["books"][0]
    full_book = full.get_json()["books"][0]
    assert lazy_book["import_state"] == full_book["import_state"] == "partial"
    assert lazy_book["captures"][0]["import_state"] == "partial"
    assert full_book["captures"][0]["import_state"] == "partial"
    assert "Captured image geometry is incomplete" in lazy_book["issues"]
    assert lazy_book["issues"] == full_book["issues"]


def test_malformed_capture_does_not_hide_healthy_index_item(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    manifest = server.CAPTURES_DIR / CAPTURE_ID / "photo_assets.json"
    manifest.write_text("{", encoding="utf-8")
    with server._builds_lock:
        builds = server.lib.load_json(server.BUILDS_PATH, {})
        builds["plain-book"] = {
            "id": "plain-book",
            "title": "Unaffiliated Healthy Book",
        }
        server.lib.save_json(server.BUILDS_PATH, builds)

    response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200
    books = {book["id"]: book for book in response.get_json()["books"]}
    assert set(books) == {BOOK_ID, "plain-book"}
    assert books["plain-book"]["captures"] == []
    assert books[BOOK_ID]["captures"][0]["resource_state"] == "unavailable"
    assert books[BOOK_ID]["captures"][0]["import_state"] == "unavailable"


def test_manual_archive_uses_lazy_index_then_byte_verified_preview(
    client,
    corrections_workspace,
    monkeypatch,
    tmp_path,
):
    import server

    manual_book_id = "b-22222222222222222222222222222222"
    manual_path = tmp_path / "manual_entries.json"
    server.lib.save_json(
        manual_path,
        {
            "manual-row": {
                "id": "manual-row",
                "title": "Archived Manual Herbal",
                "capture_id": CAPTURE_ID,
                "created_at": "2026-08-04T00:00:00Z",
            }
        },
    )
    server.lib.save_json(server.BUILDS_PATH, {})
    monkeypatch.setattr(server.lib, "MANUAL_ENTRIES_PATH", manual_path)

    class AssociationIdentity:
        book_id = manual_book_id

        class state:
            value = "current"

    monkeypatch.setattr(
        server.FilesystemCaptureArchiveRepository,
        "inspect_association_identity",
        lambda _root, capture_id: (
            AssociationIdentity() if capture_id == CAPTURE_ID else None
        ),
    )
    read_json = FilesystemCorrectionsArtifactRepository._read_json

    def reject_layout_read(repository, path, *args, **kwargs):
        if kwargs.get("section") == "layout":
            pytest.fail("an uncorrected build index read layout bytes")
        return read_json(repository, path, *args, **kwargs)

    monkeypatch.setattr(
        FilesystemCorrectionsArtifactRepository,
        "_read_json",
        reject_layout_read,
    )
    monkeypatch.setattr(
        server.FilesystemCaptureArchiveRepository,
        "inspect_association",
        lambda _root, capture_id: (
            AssociationIdentity() if capture_id == CAPTURE_ID else None
        ),
    )
    original_observe = FilesystemCorrectionsArtifactRepository._observe_resource
    original_review = CorrectionProjectionService.get_correction_review

    def reject_image_read(*_args, **_kwargs):
        raise AssertionError("the navigation index read capture bytes")

    monkeypatch.setattr(
        FilesystemCorrectionsArtifactRepository,
        "_observe_resource",
        reject_image_read,
    )
    monkeypatch.setattr(
        CorrectionProjectionService,
        "get_correction_review",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the lazy index loaded a correction aggregate")
        ),
    )

    index = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert index.status_code == 200
    books = index.get_json()["books"]
    assert [book["id"] for book in books] == [manual_book_id]
    capture = books[0]["captures"][0]
    assert capture["revision"].startswith("index:")
    assert capture["thumbnail"]["url"].endswith("/preview")
    assert books[0]["latest_imported_at"] == "2026-08-04T12:34:56Z"

    monkeypatch.setattr(
        FilesystemCorrectionsArtifactRepository,
        "_observe_resource",
        original_observe,
    )
    preview = client.get(capture["thumbnail"]["url"])
    bad_preview = client.get(capture["thumbnail"]["url"] + "?revision=index")

    assert preview.status_code == 200
    assert preview.data == corrections_workspace
    assert preview.mimetype == "image/jpeg"
    assert bad_preview.status_code == 400
    assert bad_preview.get_json()["code"] == "invalid_raster_preview_query"

    monkeypatch.setattr(
        CorrectionProjectionService,
        "get_correction_review",
        original_review,
    )
    aggregate = (
        server._ensure_engine_session().write_set.root
        / ".engine"
        / "corrections"
        / "aggregates"
        / f"{hashlib.sha256(manual_book_id.encode('utf-8')).hexdigest()}.json"
    )
    aggregate.parent.mkdir(parents=True, exist_ok=True)
    aggregate.write_text("{}", encoding="utf-8")
    corrupt = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    assert corrupt.status_code == 500
    assert corrupt.is_json
    assert corrupt.get_json()["code"] == "invalid_correction_snapshot"


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


def test_capture_only_review_mark_resolve_and_reopen_use_canonical_item(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    _use_capture_only_target(server, title="Capture Awaiting Review")
    monkeypatch.setattr(
        server,
        "_auth_doc",
        lambda: {"session": {"user_id": "capture-reviewer"}},
    )
    review_path = f"/api/v1/items/{BOOK_ID}/corrections/review"
    initial = client.get(review_path).get_json()["review"]

    marked = client.put(
        f"{review_path}/attention",
        json={
            "reason": "Verify the handwritten notes",
            "comment": "Capture-only review",
        },
        headers={
            "Idempotency-Key": "capture-review-mark",
            "If-Review-Match": f'"{initial["revision"]}"',
        },
    )
    assert marked.status_code == 200, marked.get_json()
    marked_review = client.get(review_path).get_json()["review"]
    assert marked_review["state"] == "needs_attention"
    assert marked_review["history_tail"][-1]["actor_id"] == (
        "capture-reviewer"
    )

    other_window = client.application.test_client()
    resolved = other_window.post(
        f"{review_path}/resolve",
        json={"comment": "Checked against the source capture"},
        headers={
            "Idempotency-Key": "capture-review-resolve",
            "If-Review-Match": f'"{marked_review["revision"]}"',
        },
    )
    assert resolved.status_code == 200, resolved.get_json()
    resolved_review = client.get(review_path).get_json()["review"]
    assert resolved_review["state"] == "resolved"

    reopened = client.post(
        f"{review_path}/reopen",
        json={"comment": "Caption still needs correction"},
        headers={
            "Idempotency-Key": "capture-review-reopen",
            "If-Review-Match": f'"{resolved_review["revision"]}"',
        },
    )
    assert reopened.status_code == 200, reopened.get_json()

    index = other_window.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    ).get_json()
    assert [(row["id"], row["kind"]) for row in index["books"]] == [
        (BOOK_ID, "capture"),
    ]
    assert index["books"][0]["review"]["state"] == "needs_attention"
    assert index["attention"][0]["target"] == {
        "kind": "book",
        "item_id": BOOK_ID,
    }
    assert index["attention"][0]["review"]["history_count"] == 3


def test_capture_only_raster_category_and_caption_round_trip(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    _use_capture_only_target(server)
    display = next(
        artifact
        for artifact in client.get(
            f"/api/v1/items/{BOOK_ID}/raster-artifacts"
            "?representation_id=capture"
        ).get_json()["artifacts"]
        if artifact["key"]["artifact_id"].endswith(":display")
    )
    artifact_id = display["key"]["artifact_id"]
    detail_path = f"/api/v1/items/{BOOK_ID}/raster-artifacts/{artifact_id}"

    categorized = client.put(
        f"{detail_path}/category",
        json={"category": "title_page"},
        headers={
            "Idempotency-Key": "capture-category-title",
            "If-Artifact-Match": f'"{display["revision"]}"',
        },
    )
    assert categorized.status_code == 200, categorized.get_json()
    categorized_detail = client.get(detail_path).get_json()["artifact"]
    assert categorized_detail["effective_category"] == "title_page"

    uncategorized = client.delete(
        f"{detail_path}/category",
        json={},
        headers={
            "Idempotency-Key": "capture-category-clear",
            "If-Artifact-Match": (
                f'"{categorized_detail["revision"]}"'
            ),
        },
    )
    assert uncategorized.status_code == 200, uncategorized.get_json()
    caption_target = client.get(detail_path).get_json()["artifact"]
    assert caption_target["effective_category"] == "cover"

    captioned = client.put(
        f"{detail_path}/caption",
        json={
            "text": "Hand-colored medicinal plant",
            "language": "en",
        },
        headers={
            "Idempotency-Key": "capture-caption-set",
            "If-Artifact-Match": f'"{caption_target["revision"]}"',
        },
    )
    assert captioned.status_code == 200, captioned.get_json()
    captioned_detail = client.get(detail_path).get_json()["artifact"]
    assert captioned_detail["effective_caption"]["text"] == (
        "Hand-colored medicinal plant"
    )
    assert captioned_detail["effective_caption"]["origin"] == "manual"
    assert captioned_detail["effective_caption"]["language"] == "en"

    cleared = client.delete(
        f"{detail_path}/caption",
        json={},
        headers={
            "Idempotency-Key": "capture-caption-clear",
            "If-Artifact-Match": f'"{captioned_detail["revision"]}"',
        },
    )
    assert cleared.status_code == 200, cleared.get_json()
    assert client.get(detail_path).get_json()["artifact"][
        "effective_caption"
    ] is None


def test_capture_only_mistral_region_role_assignment_and_clear(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    _use_capture_only_target(server)
    annotations = client.get(
        f"/api/v1/items/{BOOK_ID}/spatial-annotations"
        "?representation_id=capture"
    ).get_json()["annotations"]
    annotation = next(
        row for row in annotations if row["label"] == "Materia medica"
    )
    annotation_id = annotation["key"]["annotation_id"]
    linked_id = annotation["linked_artifact_ids"][0]
    linked = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/{linked_id}"
    ).get_json()["artifact"]
    role_path = (
        f"/api/v1/items/{BOOK_ID}/spatial-annotations/"
        f"{annotation_id}/role"
    )

    assigned = client.put(
        role_path,
        json={
            "role": "MAR",
            "linked_artifact_id": linked_id,
        },
        headers={
            "Idempotency-Key": "capture-region-mar",
            "If-Annotation-Match": f'"{annotation["revision"]}"',
            "If-Linked-Artifact-Match": f'"{linked["revision"]}"',
        },
    )
    assert assigned.status_code == 200, assigned.get_json()
    assigned_annotation = client.get(
        f"/api/v1/items/{BOOK_ID}/spatial-annotations/{annotation_id}"
    ).get_json()["annotation"]
    assigned_link = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/{linked_id}"
    ).get_json()["artifact"]
    assert assigned_annotation["effective_role"] == "marginalia"
    assert assigned_link["effective_role"] == "marginalia"

    cleared = client.delete(
        role_path,
        json={"linked_artifact_id": linked_id},
        headers={
            "Idempotency-Key": "capture-region-role-clear",
            "If-Annotation-Match": (
                f'"{assigned_annotation["revision"]}"'
            ),
            "If-Linked-Artifact-Match": f'"{assigned_link["revision"]}"',
        },
    )
    assert cleared.status_code == 200, cleared.get_json()
    refreshed = client.get(
        f"/api/v1/items/{BOOK_ID}/spatial-annotations/{annotation_id}"
    ).get_json()["annotation"]
    refreshed_link = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/{linked_id}"
    ).get_json()["artifact"]
    assert refreshed["effective_role"] == "text"
    assert refreshed_link["effective_role"] == ""


def test_capture_promotion_preserves_human_corrections_and_one_identity(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    _use_capture_only_target(
        server,
        title="Capture With Human Corrections",
    )
    monkeypatch.setattr(
        server,
        "_auth_doc",
        lambda: {"session": {"user_id": "promotion-reviewer"}},
    )
    review_path = f"/api/v1/items/{BOOK_ID}/corrections/review"
    initial_review = client.get(review_path).get_json()["review"]
    marked = client.put(
        f"{review_path}/attention",
        json={
            "reason": "Preserve this review during promotion",
            "comment": "Human review before build creation",
        },
        headers={
            "Idempotency-Key": "promotion-preserve-review",
            "If-Review-Match": f'"{initial_review["revision"]}"',
        },
    )
    assert marked.status_code == 200, marked.get_json()

    display = next(
        artifact
        for artifact in client.get(
            f"/api/v1/items/{BOOK_ID}/raster-artifacts"
            "?representation_id=capture"
        ).get_json()["artifacts"]
        if artifact["key"]["artifact_id"].endswith(":display")
    )
    artifact_id = display["key"]["artifact_id"]
    detail_path = f"/api/v1/items/{BOOK_ID}/raster-artifacts/{artifact_id}"
    categorized = client.put(
        f"{detail_path}/category",
        json={"category": "title_page"},
        headers={
            "Idempotency-Key": "promotion-preserve-category",
            "If-Artifact-Match": f'"{display["revision"]}"',
        },
    )
    assert categorized.status_code == 200, categorized.get_json()
    category_detail = client.get(detail_path).get_json()["artifact"]
    captioned = client.put(
        f"{detail_path}/caption",
        json={
            "text": "Human caption preserved through promotion",
            "language": "en",
        },
        headers={
            "Idempotency-Key": "promotion-preserve-caption",
            "If-Artifact-Match": f'"{category_detail["revision"]}"',
        },
    )
    assert captioned.status_code == 200, captioned.get_json()

    annotation = next(
        row
        for row in client.get(
            f"/api/v1/items/{BOOK_ID}/spatial-annotations"
            "?representation_id=capture"
        ).get_json()["annotations"]
        if row["label"] == "Materia medica"
    )
    annotation_id = annotation["key"]["annotation_id"]
    linked_id = annotation["linked_artifact_ids"][0]
    linked = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts/{linked_id}"
    ).get_json()["artifact"]
    assigned = client.put(
        (
            f"/api/v1/items/{BOOK_ID}/spatial-annotations/"
            f"{annotation_id}/role"
        ),
        json={
            "role": "MAR",
            "linked_artifact_id": linked_id,
        },
        headers={
            "Idempotency-Key": "promotion-preserve-role",
            "If-Annotation-Match": f'"{annotation["revision"]}"',
            "If-Linked-Artifact-Match": f'"{linked["revision"]}"',
        },
    )
    assert assigned.status_code == 200, assigned.get_json()

    engine = server._library_engine()
    rasters = engine.require_service(RASTER_ARTIFACT_QUERY_SERVICE)
    transform_source = next(
        value
        for value in rasters.list_raster_artifacts(BOOK_ID)
        if value.key.artifact_id == artifact_id
    )
    transforms = engine.require_service(CORRECTION_TRANSFORM_SERVICE)
    transform_command = _transform_command(
        transform_source,
        "promotion-preserve-transform",
    )
    assert transforms.queue(transform_command).created is True
    transform_result = transforms.execute_queued(transform_command)
    assert transform_result.image_commit is not None

    source_item = client.get(
        f"/api/v1/corrections/items/{BOOK_ID}"
    ).get_json()["item"]
    promoted = client.post(
        "/api/v1/capture-promotions",
        json={
            "promotion": {
                "capture_id": CAPTURE_ID,
                "source_revision": source_item["record_revision"],
                "item": {
                    "kind": "book",
                    "title": "",
                    "metadata": {},
                    "representations": [],
                },
                "primary_source": "",
            }
        },
        headers={
            "Idempotency-Key": "promotion-preserve-human-corrections",
        },
    )
    assert promoted.status_code == 201, promoted.get_json()
    assert promoted.get_json()["capture_id"] == CAPTURE_ID
    assert promoted.get_json()["build"]["capture_book_id"] == BOOK_ID

    index = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    ).get_json()
    assert [(row["id"], row["kind"]) for row in index["books"]] == [
        (BOOK_ID, "book"),
    ]
    review = client.get(review_path).get_json()["review"]
    assert review["state"] == "needs_attention"
    assert len(review["history_tail"]) == 1
    assert review["history_tail"][-1]["actor_id"] == "promotion-reviewer"

    artifact = client.get(detail_path).get_json()["artifact"]
    assert artifact["effective_category"] == "title_page"
    assert artifact["effective_role"] == "marginalia"
    assert artifact["effective_caption"]["origin"] == "manual"
    assert artifact["effective_caption"]["text"] == (
        "Human caption preserved through promotion"
    )
    assert artifact["effective_caption"]["language"] == "en"
    region = client.get(
        (
            f"/api/v1/items/{BOOK_ID}/spatial-annotations/"
            f"{annotation_id}"
        )
    ).get_json()["annotation"]
    assert region["effective_role"] == "marginalia"
    correction_outputs = [
        value
        for value in rasters.list_raster_artifacts(BOOK_ID)
        if value.extensions.get("correction_transform", {}).get(
            "operation_id"
        ) == transform_command.operation_id
    ]
    assert {
        value.kind for value in correction_outputs
    } == {
        "corrected-image",
        "processed-image",
        "processed-source",
    }


def test_capture_only_index_isolates_partial_legacy_and_corrupt_assets(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    _use_capture_only_target(server, title="Capture With Damaged Assets")
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "healthy-book": {
                    "id": "healthy-book",
                    "title": "Healthy Catalogue Book",
                }
            },
        )
    manifest_path = (
        server.CAPTURES_DIR / CAPTURE_ID / "photo_assets.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first = manifest["assets"][0]
    missing = json.loads(json.dumps(first))
    missing["asset_id"] = "asset-missing"
    missing["capture_order"] = 2
    missing["capture_file"] = "photo_missing.jpg"
    missing["original"]["reference"] = "original_missing.jpg"
    missing["display"]["reference"] = "photo_missing.jpg"
    missing["geometry"] = []
    manifest["assets"].append(missing)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    partial_response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    assert partial_response.status_code == 200
    partial_books = {
        row["id"]: row for row in partial_response.get_json()["books"]
    }
    partial = partial_books[BOOK_ID]
    assert partial["kind"] == "capture"
    assert partial["import_state"] == "partial"
    assert [
        row["resource_state"] for row in partial["captures"]
    ] == ["available", "missing"]
    assert "1 captured image is missing" in partial["issues"]
    assert partial_books["healthy-book"]["import_state"] == "ready"

    manifest_path.unlink()
    legacy_response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    assert legacy_response.status_code == 200
    legacy_books = {
        row["id"]: row for row in legacy_response.get_json()["books"]
    }
    legacy_capture = legacy_books[BOOK_ID]
    assert legacy_capture["import_state"] == "legacy"
    assert len(legacy_capture["captures"]) == 1
    assert legacy_capture["captures"][0]["resource_state"] == "available"
    assert legacy_capture["captures"][0]["thumbnail"] is not None
    assert "1 captured image is missing" not in legacy_capture["issues"]
    assert "healthy-book" in legacy_books

    manifest_path.write_text('{"assets": [', encoding="utf-8")
    corrupt_response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    assert corrupt_response.status_code == 200, corrupt_response.get_json()
    corrupt_books = {
        row["id"]: row for row in corrupt_response.get_json()["books"]
    }
    corrupt_capture = corrupt_books[BOOK_ID]
    assert corrupt_capture["import_state"] == "unavailable"
    assert len(corrupt_capture["captures"]) == 1
    assert corrupt_capture["captures"][0]["resource_state"] == (
        "unavailable"
    )
    assert corrupt_capture["captures"][0]["thumbnail"] is None
    assert "1 captured image is unavailable" in (
        corrupt_capture["issues"]
    )
    assert corrupt_books["healthy-book"]["title"] == (
        "Healthy Catalogue Book"
    )
    assert str(server.CAPTURES_DIR) not in corrupt_response.get_data(
        as_text=True
    )


def test_capture_only_index_marks_malformed_rendition_and_geometry_partial(
    client,
    corrections_workspace,
    monkeypatch,
):
    del corrections_workspace
    import server

    _use_capture_only_target(server, title="Capture With Partial Metadata")
    with server._builds_lock:
        server.lib.save_json(
            server.BUILDS_PATH,
            {
                "healthy-book": {
                    "id": "healthy-book",
                    "title": "Healthy Catalogue Book",
                }
            },
        )
    manifest_path = (
        server.CAPTURES_DIR / CAPTURE_ID / "photo_assets.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["assets"][0]["original"] = ["invalid", "rendition"]
    manifest["assets"][0]["geometry"] = ["invalid-geometry"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    monkeypatch.setattr(
        server,
        "_corrections_uses_lazy_capture_index",
        lambda _item_id: False,
    )
    full_response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200, response.get_json()
    assert full_response.status_code == 200, full_response.get_json()
    books = {row["id"]: row for row in response.get_json()["books"]}
    full_books = {
        row["id"]: row for row in full_response.get_json()["books"]
    }
    damaged = books[BOOK_ID]
    full_damaged = full_books[BOOK_ID]
    assert damaged["kind"] == "capture"
    assert damaged["import_state"] == "partial"
    assert len(damaged["captures"]) == 1
    assert damaged["captures"][0]["resource_state"] == "available"
    assert damaged["captures"][0]["import_state"] == "partial"
    assert "Captured image geometry is incomplete" in damaged["issues"]
    assert "1 captured image record is incomplete" in damaged["issues"]
    assert damaged["import_state"] == full_damaged["import_state"]
    assert damaged["captures"][0]["import_state"] == (
        full_damaged["captures"][0]["import_state"]
    )
    assert damaged["issues"] == full_damaged["issues"]
    assert books["healthy-book"]["import_state"] == "ready"

    artifacts_response = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts"
    )
    assert artifacts_response.status_code == 200
    artifacts = artifacts_response.get_json()["artifacts"]
    scopes = {
        diagnostic["scope"]
        for artifact in artifacts
        for diagnostic in artifact["extensions"].get(
            "artifact_diagnostics",
            [],
        )
    }
    assert scopes == {"capture_geometry", "capture_rendition"}
    assert str(server.CAPTURES_DIR) not in response.get_data(as_text=True)
    assert str(server.CAPTURES_DIR) not in (
        artifacts_response.get_data(as_text=True)
    )


def test_corrupt_mistral_layout_isolated_in_real_index_and_artifact_routes(
    client,
    corrections_workspace,
):
    del corrections_workspace
    import server

    with server._builds_lock:
        builds = server.lib.load_json(server.BUILDS_PATH, {})
        builds["healthy-book"] = {
            "id": "healthy-book",
            "title": "Healthy Catalogue Book",
        }
        server.lib.save_json(server.BUILDS_PATH, builds)
    layout_path = server.ENTRIES_DIR / BOOK_ID / "ocr" / "layout.json"
    layout_path.write_text('{"regions": [', encoding="utf-8")

    response = client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
    )

    assert response.status_code == 200, response.get_json()
    books = {row["id"]: row for row in response.get_json()["books"]}
    damaged = books[BOOK_ID]
    assert damaged["import_state"] == "ready"
    assert "Mistral artifact layout is unavailable" not in damaged["issues"]
    assert books["healthy-book"]["import_state"] == "ready"

    artifacts_response = client.get(
        f"/api/v1/items/{BOOK_ID}/raster-artifacts"
    )
    assert artifacts_response.status_code == 200
    artifacts = artifacts_response.get_json()["artifacts"]
    diagnostic = next(
        artifact
        for artifact in artifacts
        if artifact["kind"] == "artifact-diagnostic"
    )
    assert diagnostic["resource_state"] == "unavailable"
    assert diagnostic["resource"] is None
    assert diagnostic["extensions"]["artifact_diagnostics"] == [
        {
            "scope": "mistral_layout",
            "code": "invalid_mistral_layout",
            "state": "unavailable",
        }
    ]
    assert len(
        [
            artifact
            for artifact in artifacts
            if artifact["source"]["representation_id"] == "capture"
        ]
    ) == 2
    assert str(layout_path) not in response.get_data(as_text=True)
    assert str(layout_path) not in artifacts_response.get_data(as_text=True)


def test_production_review_bridge_owns_actor_and_reconciles_cas(
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
        "/api/v1/corrections/index?workspace_id=local-library"
    )
    wrong_workspace = client.get(
        "/api/v1/corrections/index?workspace_id=workspace-1"
    )

    assert initial_detail.status_code == 200
    assert initial_index.status_code == 200
    assert wrong_workspace.status_code == 409
    assert wrong_workspace.get_json()["code"] == (
        "corrections_workspace_mismatch"
    )
    initial_review = initial_detail.get_json()["review"]
    index_body = initial_index.get_json()
    assert index_body["schema"] == "librarytool.corrections-index/2"
    assert [book["id"] for book in index_body["books"]] == [BOOK_ID]
    assert [
        capture["artifact_id"].endswith(":display")
        for capture in index_body["books"][0]["captures"]
    ] == [True]
    assert index_body["attention"] == []

    spoofed = client.put(
        f"{review_path}/attention",
        json={
            "reason": "Verify the cover",
            "actor_id": "spoofed-client",
            "comment": "",
        },
        headers={
            "Idempotency-Key": "bridge-review-spoof",
            "If-Review-Match": f'"{initial_review["revision"]}"',
        },
    )
    marked = client.put(
        f"{review_path}/attention",
        json={"reason": "Verify the cover", "comment": "Window one"},
        headers={
            "Idempotency-Key": "bridge-review-mark",
            "If-Review-Match": f'"{initial_review["revision"]}"',
        },
    )

    assert spoofed.status_code == 400
    assert marked.status_code == 200

    second_client = client.application.test_client()
    observed = second_client.get(
        "/api/v1/corrections/index?workspace_id=local-library"
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
        "/api/v1/corrections/index?workspace_id=local-library"
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
