"""Portable living-edition package, layer graph, and safety contracts."""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "living_edition"))
import whled  # noqa: E402


NOW = "2026-08-12T00:00:00Z"
CANVAS_REVISION = "sha256-synthetic-canvas-r1"
REGION_REVISION = "regions-human-r1"
TEXT_REVISION = "transcription-human-r1"
SNAPSHOT = ROOT / "data" / "plant-authority-poc" / "snapshot.json"


def _actor(kind: str = "human", actor_id: str = "editor-alice") -> dict:
    return {
        "type": kind,
        "id": actor_id,
        "label": "Alice Editor" if kind == "human" else "Synthetic model",
        "version": "1",
        "ext": {},
    }


def _layer(layer_id: str, revision: str, kind: str, label: str, data: dict, *, language=None, actor=None) -> dict:
    actor = actor or _actor()
    return {
        "$schema": whled.LAYER_SCHEMA_ID,
        "id": layer_id,
        "revision": revision,
        "kind": kind,
        "label": label,
        "status": "human-draft" if actor["type"] == "human" else "machine-draft",
        "language": language,
        "provenance": {
            "actor": actor,
            "generated_at": NOW,
            "method": "synthetic-test",
            "parameters": {},
            "sources": [],
            "ext": {},
        },
        "dependencies": [],
        "history": [{
            "id": f"event-{layer_id}",
            "action": "created",
            "actor": actor,
            "at": NOW,
            "message": "Created for format test",
            "base_revision": None,
            "ext": {},
        }],
        "reviews": [],
        "data": data,
        "ext": {},
    }


def _fixture() -> tuple[dict, dict[str, bytes]]:
    image = b"synthetic jpeg evidence; not a manuscript image"
    snapshot = SNAPSHOT.read_bytes()
    region = _layer(
        "regions-human", REGION_REVISION, "region", "Manual regions",
        {
            "regions": [
                {
                    "id": "region-body-1",
                    "canvas_id": "canvas-0001",
                    "canvas_revision": CANVAS_REVISION,
                    "selector": {
                        "type": "polygon",
                        "coordinate_space": "canvas-normalized",
                        "canvas_revision": CANVAS_REVISION,
                        "points": [
                            {"x": 0.1, "y": 0.1},
                            {"x": 0.8, "y": 0.1},
                            {"x": 0.8, "y": 0.7},
                            {"x": 0.1, "y": 0.7},
                        ],
                    },
                    "type_ids": ["layout:body", "hand:hand-a"],
                    "parent_region_id": None,
                    "order": 0,
                    "label": "Main body",
                    "confidence": None,
                    "ext": {},
                },
                {
                    "id": "region-margin-1",
                    "canvas_id": "canvas-0001",
                    "canvas_revision": CANVAS_REVISION,
                    "selector": {
                        "type": "box",
                        "coordinate_space": "canvas-normalized",
                        "canvas_revision": CANVAS_REVISION,
                        "x": 0.01,
                        "y": 0.2,
                        "width": 0.08,
                        "height": 0.2,
                    },
                    "type_ids": ["layout:marginalia", "hand:hand-b"],
                    "parent_region_id": None,
                    "order": 1,
                    "label": "Marginal gloss",
                    "confidence": None,
                    "ext": {},
                },
            ],
            "reading_flows": [
                {
                    "id": "flow-main",
                    "label": "Main reading flow",
                    "direction": "ltr",
                    "ordered_region_ids": ["region-body-1"],
                    "ext": {},
                },
                {
                    "id": "flow-glosses",
                    "label": "Marginal glosses",
                    "direction": "ttb",
                    "ordered_region_ids": ["region-margin-1"],
                    "ext": {},
                },
            ],
            "relations": [{
                "id": "relation-gloss-1",
                "subject_region_id": "region-margin-1",
                "predicate": "marginalia-of",
                "object_region_id": "region-body-1",
                "confidence": "certain",
                "ext": {},
            }],
        },
    )
    transcription = _layer(
        "transcription-human", TEXT_REVISION, "transcription", "Human diplomatic transcription",
        {
            "passages": [{
                "id": "passage-1",
                "canvas_id": "canvas-0001",
                "canvas_revision": CANVAS_REVISION,
                "region_ref": {
                    "layer_id": "regions-human",
                    "revision": REGION_REVISION,
                    "region_id": "region-body-1",
                },
                "order": 0,
                "text": "Take gencyane and grind it.",
                "alignment_refs": [],
                "uncertainties": [],
                "confidence": None,
                "ext": {},
            }],
        },
        language="enm",
    )
    transcription["dependencies"] = [{
        "layer_id": "regions-human",
        "revision": REGION_REVISION,
        "relation": "transcribed-from",
    }]
    entity = _layer(
        "entities-poc", "entities-poc-r1", "entity", "Plant mentions",
        {
            "mentions": [{
                "id": "mention-gencyane-1",
                "canvas_id": "canvas-0001",
                "canvas_revision": CANVAS_REVISION,
                "region_ref": {
                    "layer_id": "regions-human",
                    "revision": REGION_REVISION,
                    "region_id": "region-body-1",
                },
                "selector": None,
                "text_anchor": {
                    "layer_id": "transcription-human",
                    "revision": TEXT_REVISION,
                    "passage_id": "passage-1",
                    "start": 5,
                    "end": 13,
                    "exact": "gencyane",
                    "prefix": "Take ",
                    "suffix": " and grind it.",
                    "status": "current",
                },
                "authority_refs": [{
                    "database_id": "whl-plant-authority-poc",
                    "snapshot_id": "plant-authority-poc-2026-08-12",
                    "node_type": "concept",
                    "node_id": "concept-gentian-western",
                    "role": "candidate-concept",
                    "assertion_ids": ["assert-name-gencyane"],
                }],
                "review_state": "proposed",
                "ext": {},
            }],
        },
    )
    entity["dependencies"] = [
        {"layer_id": "regions-human", "revision": REGION_REVISION, "relation": "anchored-to"},
        {"layer_id": "transcription-human", "revision": TEXT_REVISION, "relation": "mentions-in"},
    ]
    layer_payloads = {
        "layers/regions-human/regions-human-r1.json": json.dumps(region, ensure_ascii=False).encode(),
        "layers/transcription-human/transcription-human-r1.json": json.dumps(transcription, ensure_ascii=False).encode(),
        "layers/entities-poc/entities-poc-r1.json": json.dumps(entity, ensure_ascii=False).encode(),
    }
    payloads = {
        "canvases/canvas-0001.jpeg": image,
        "authority-snapshots/plant-authority-poc-2026-08-12.json": snapshot,
        **layer_payloads,
    }
    descriptors = [
        {
            "id": region["id"], "revision": region["revision"], "kind": region["kind"],
            "label": region["label"], "member": "layers/regions-human/regions-human-r1.json",
            "current": True, "variant": "manual", "ext": {},
        },
        {
            "id": transcription["id"], "revision": transcription["revision"], "kind": transcription["kind"],
            "label": transcription["label"], "member": "layers/transcription-human/transcription-human-r1.json",
            "current": True, "variant": "diplomatic", "language": "enm", "ext": {},
        },
        {
            "id": entity["id"], "revision": entity["revision"], "kind": entity["kind"],
            "label": entity["label"], "member": "layers/entities-poc/entities-poc-r1.json",
            "current": True, "variant": "plant-mentions", "ext": {},
        },
    ]
    resources = [
        {
            "member": member,
            "media_type": "image/jpeg" if member.startswith("canvases/") else "application/json",
            "role": (
                "canvas-image" if member.startswith("canvases/")
                else "authority-snapshot" if member.startswith("authority-snapshots/")
                else "layer"
            ),
            "sha256": "0" * 64,
            "bytes": 0,
            "ext": {},
        }
        for member in payloads
    ]
    manifest = {
        "$schema": whled.MANIFEST_SCHEMA_ID,
        "format": whled.FORMAT,
        "package_id": "pkg-synthetic-herbal",
        "created_at": NOW,
        "generator": "world-herb-library/tests",
        "edition": {
            "id": "herbal-poc",
            "revision": "edition-r1",
            "status": "working",
            "label": "Synthetic test edition",
            "steward": _actor(),
            "previous_revision": None,
            "ext": {},
        },
        "catalog": {
            "record_id": "yale-16156709",
            "title": "Herbal in prose and verse",
            "material_type": "manuscript",
            "repository": "Yale University Library",
            "call_number": "Takamiya MS 46 1",
            "dates": [{"display": "[ca. 1400-1425]", "qualifier": "circa", "start": 1400, "end": 1425}],
            "languages": ["enm"],
            "identifiers": [{"scheme": "yale-dc", "value": "16156709", "uri": "https://collections.library.yale.edu/catalog/16156709"}],
            "source_url": "https://collections.library.yale.edu/catalog/16156709",
            "rights": "Rights statement preserved from the source record.",
            "ext": {},
        },
        "source": {
            "filename": "herbal.pdf",
            "media_type": "application/pdf",
            "sha256": "a" * 64,
            "page_count": 115,
            "excluded_pages": [1],
            "extraction": {"method": "synthetic-test"},
            "ext": {},
        },
        "canvases": [{
            "id": "canvas-0001",
            "revision": CANVAS_REVISION,
            "sequence": 1,
            "label": "1r",
            "source_page": 8,
            "source_label": "1r Image ID: 16156797",
            "image_member": "canvases/canvas-0001.jpeg",
            "dimensions": {"width": 1160, "height": 2000},
            "ext": {},
        }],
        "region_types": [
            {"id": "layout:text", "label": "Text", "facet": "layout", "parent_id": None, "description": "Written text", "custom": False, "ext": {}},
            {"id": "layout:body", "label": "Body", "facet": "layout", "parent_id": "layout:text", "description": "Main text", "custom": False, "ext": {}},
            {"id": "layout:marginalia", "label": "Marginalia", "facet": "layout", "parent_id": "layout:text", "description": "Margin text", "custom": False, "ext": {}},
            {"id": "hand:scribal", "label": "Hand", "facet": "hand", "parent_id": None, "description": "Writing hand", "custom": False, "ext": {}},
            {"id": "hand:hand-a", "label": "Hand A", "facet": "hand", "parent_id": "hand:scribal", "description": "First hand", "custom": True, "ext": {}},
            {"id": "hand:hand-b", "label": "Hand B", "facet": "hand", "parent_id": "hand:scribal", "description": "Second hand", "custom": True, "ext": {}},
        ],
        "layers": descriptors,
        "resources": resources,
        "authority_snapshots": [{
            "id": "plant-authority-poc-2026-08-12",
            "database_id": "whl-plant-authority-poc",
            "release": "poc-2026-08-12",
            "member": "authority-snapshots/plant-authority-poc-2026-08-12.json",
            "created_at": NOW,
            "ext": {},
        }],
        "external_authorities": [{
            "id": "plant-authority",
            "kind": "plant-name-authority",
            "database_id": "whl-plant-authority-poc",
            "snapshot_id": "plant-authority-poc-2026-08-12",
            "resolver_template": "https://worldherblibrary.org/entity/{id}",
            "ext": {},
        }],
        "registries": {
            "layer_kinds": [],
            "selector_types": [],
            "resource_roles": [],
            "region_relation_predicates": [],
            "capabilities": [],
        },
        "capabilities": ["polygon-regions", "parallel-layers", "entity-snapshot", "guided-reprocessing"],
        "ext": {},
    }
    return manifest, payloads


def _seal() -> bytes:
    manifest, payloads = _fixture()
    buffer = io.BytesIO()
    whled.seal_archive(manifest, payloads, buffer)
    return buffer.getvalue()


def test_seals_deterministically_and_round_trips_all_editor_contracts():
    first = _seal()
    second = _seal()
    assert first == second
    assert whled.validate_archive(first) == []

    document = whled.read_archive(first)
    assert document.manifest["format"] == "whled/0.1"
    assert {layer["kind"] for layer in document.manifest["layers"]} == {"region", "transcription", "entity"}
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        names = set(archive.namelist())
        assert "INSTRUCTIONS.md" in names
        assert "checksums.sha256" in names
        assert not any(name.casefold().endswith((".sqlite", ".sqlite3", ".db", ".db3")) for name in names)
        region = json.loads(archive.read("layers/regions-human/regions-human-r1.json"))
        assert region["data"]["regions"][0]["selector"]["type"] == "polygon"
        assert region["data"]["regions"][1]["type_ids"] == ["layout:marginalia", "hand:hand-b"]
        assert region["data"]["reading_flows"][1]["id"] == "flow-glosses"
        assert region["data"]["relations"][0]["predicate"] == "marginalia-of"


def test_checksum_tampering_is_detected():
    sealed = _seal()
    source = zipfile.ZipFile(io.BytesIO(sealed))
    tampered = io.BytesIO()
    with source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            payload = source.read(info.filename)
            if info.filename == "canvases/canvas-0001.jpeg":
                payload += b"tampered"
            target.writestr(info.filename, payload)
    issues = whled.validate_archive(tampered.getvalue())
    assert any("checksum mismatch" in issue.message for issue in issues)


def test_master_sqlite_database_is_never_a_package_resource():
    manifest, payloads = _fixture()
    member = "assets/plant-authority.sqlite3"
    manifest["resources"].append({
        "member": member,
        "media_type": "application/vnd.sqlite3",
        "role": "authority-database",
        "sha256": "0" * 64,
        "bytes": 0,
        "ext": {},
    })
    payloads[member] = b"SQLite format 3\0synthetic"
    with pytest.raises(whled.WhledError, match="forbidden resource member"):
        whled.seal_archive(manifest, payloads, io.BytesIO())


def test_entity_anchor_must_match_exact_pinned_passage():
    manifest, payloads = _fixture()
    member = "layers/entities-poc/entities-poc-r1.json"
    entity = json.loads(payloads[member])
    entity["data"]["mentions"][0]["text_anchor"]["exact"] = "gentiana"
    entity["data"]["mentions"][0]["text_anchor"]["end"] = 13
    payloads[member] = json.dumps(entity).encode()
    with pytest.raises(whled.WhledError, match="does not match the pinned passage revision"):
        whled.seal_archive(manifest, payloads, io.BytesIO())


def test_model_cannot_approve_a_layer_revision():
    layer = _layer("translation-model", "translation-r1", "translation", "Model translation", {"passages": []}, language="en", actor=_actor("model", "model-ocr"))
    layer["status"] = "approved"
    layer["reviews"] = [{
        "id": "review-model-self",
        "decision": "approve",
        "reviewer": _actor("model", "model-reviewer"),
        "at": NOW,
        "rationale": "Self approval",
        "applies_to_revision": "translation-r1",
        "ext": {},
    }]
    issues = whled.validate_layer(layer)
    messages = [issue.message for issue in issues]
    assert "only a human may approve or reject scholarly content" in messages
    assert "approved or frozen content requires a human approval of this revision" in messages


def test_region_graph_rejects_unknown_flow_nodes_and_self_relations():
    manifest, payloads = _fixture()
    member = "layers/regions-human/regions-human-r1.json"
    region = json.loads(payloads[member])
    region["data"]["reading_flows"][0]["ordered_region_ids"].append("missing-region")
    region["data"]["relations"][0]["object_region_id"] = "region-margin-1"
    descriptor = next(item for item in manifest["layers"] if item["kind"] == "region")
    issues = whled.validate_layer(region, manifest, descriptor)
    assert any("references missing region" in issue.message for issue in issues)
    assert any("may not point to itself" in issue.message for issue in issues)


def test_declared_namespaced_extensions_remain_safe_and_inspectable():
    manifest, payloads = _fixture()
    registry_entry = lambda identifier, hint: {  # noqa: E731 - compact fixture
        "id": identifier,
        "label": identifier,
        "description": "Synthetic declared extension",
        "schema_uri": "https://example.org/schema/extension.json",
        "renderer_hint": hint,
        "ext": {},
    }
    manifest["registries"] = {
        "layer_kinds": [registry_entry("example:analysis", "json-inspector")],
        "selector_types": [registry_entry("example:bezier-zone", "polygon-fallback")],
        "resource_roles": [registry_entry("example:analysis-data", "download")],
        "region_relation_predicates": [registry_entry("example:comments-on", "directed-edge")],
        "capabilities": [registry_entry("example:curved-zones", "feature-flag")],
    }
    manifest["capabilities"].append("example:curved-zones")

    region_member = "layers/regions-human/regions-human-r1.json"
    region = json.loads(payloads[region_member])
    original_box = region["data"]["regions"][1]["selector"]
    region["data"]["regions"][1]["selector"] = {
        "type": "example:bezier-zone",
        "coordinate_space": "canvas-normalized",
        "canvas_revision": CANVAS_REVISION,
        "data": {"control_points": [[0.01, 0.2], [0.09, 0.4]]},
        "fallback": original_box,
    }
    region["data"]["relations"].append({
        "id": "relation-custom-1",
        "subject_region_id": "region-margin-1",
        "predicate": "example:comments-on",
        "object_region_id": "region-body-1",
        "confidence": "possible",
        "ext": {},
    })
    payloads[region_member] = json.dumps(region).encode()

    extension_layer = _layer(
        "analysis-custom", "analysis-r1", "example:analysis",
        "Custom analysis", {"opaque_but_inspectable": [1, 2, 3]},
        actor=_actor("software", "tool-custom"),
    )
    extension_member = "layers/analysis-custom/analysis-r1.json"
    payloads[extension_member] = json.dumps(extension_layer).encode()
    manifest["layers"].append({
        "id": "analysis-custom", "revision": "analysis-r1",
        "kind": "example:analysis", "label": "Custom analysis",
        "member": extension_member, "current": True,
        "variant": "example:default", "ext": {},
    })
    manifest["resources"].append({
        "member": extension_member, "media_type": "application/json",
        "role": "layer", "sha256": "0" * 64, "bytes": 0, "ext": {},
    })
    data_member = "assets/custom-analysis.json"
    payloads[data_member] = b'{"portable":true}'
    manifest["resources"].append({
        "member": data_member, "media_type": "application/json",
        "role": "example:analysis-data", "sha256": "0" * 64,
        "bytes": 0, "ext": {},
    })

    buffer = io.BytesIO()
    whled.seal_archive(manifest, payloads, buffer)
    assert whled.validate_archive(buffer.getvalue()) == []
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        saved = json.loads(archive.read(extension_member))
        assert saved["data"] == {"opaque_but_inspectable": [1, 2, 3]}


def test_undeclared_extension_kind_is_rejected():
    manifest, payloads = _fixture()
    layer = _layer(
        "analysis-unknown", "analysis-r1", "example:undeclared",
        "Unknown analysis", {}, actor=_actor("software", "tool-custom"),
    )
    member = "layers/analysis-unknown/analysis-r1.json"
    payloads[member] = json.dumps(layer).encode()
    manifest["layers"].append({
        "id": layer["id"], "revision": layer["revision"],
        "kind": layer["kind"], "label": layer["label"], "member": member,
        "current": True, "variant": "example:default", "ext": {},
    })
    manifest["resources"].append({
        "member": member, "media_type": "application/json", "role": "layer",
        "sha256": "0" * 64, "bytes": 0, "ext": {},
    })
    with pytest.raises(whled.WhledError, match="core or declared layer kind"):
        whled.seal_archive(manifest, payloads, io.BytesIO())
