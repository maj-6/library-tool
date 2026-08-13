"""LIB4 package, remote-resource, partial-layer, release, and safety contracts."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "living_edition"))
import lib4  # noqa: E402


NOW = "2026-08-12T00:00:00Z"
CANVAS_REVISION = "canvas-r1"


def _actor(kind: str = "software", actor_id: str = "lib4-test") -> dict:
    return {
        "type": kind,
        "id": actor_id,
        "label": "LIB4 test" if kind != "human" else "Ada Editor",
        "version": "1",
        "ext": {},
    }


def _lifecycle(state: str, completeness: float) -> dict:
    return {
        "state": state,
        "completeness": completeness,
        "updated_at": NOW,
        "message": None,
        "retryable": state in {"planned", "partial", "failed"},
        "ext": {},
    }


def _catalog() -> dict:
    example = ROOT / "examples" / "lib4" / "minimal" / "manifest.json"
    return json.loads(example.read_text(encoding="utf-8"))["catalog"]


def _resource(
    resource_id: str,
    role: str,
    media_type: str,
    locations: list[dict],
    *,
    integrity: dict | None = None,
) -> dict:
    return {
        "id": resource_id,
        "role": role,
        "media_type": media_type,
        "byte_length": None,
        "integrity": integrity or {
            "state": "unverified",
            "algorithm": None,
            "digest": None,
            "reason": "Source service did not publish a digest.",
        },
        "locations": locations,
        "availability": {
            "state": "available",
            "checked_at": NOW,
            "cache": "allow",
            "offline": "use-alternate" if any(item["type"] != "embedded" for item in locations) else "embedded",
            "ext": {},
        },
        "retrieval_policy": {
            "mode": "on-demand" if any(item["type"] != "embedded" for item in locations) else "embedded-only",
            "authentication": "none",
            "max_bytes": None,
            "ext": {},
        },
        "rights_policy_id": "rights-cc0",
        "access_policy_id": "access-public",
        "provenance": {
            "actor": _actor("import", "fixture-import"),
            "at": NOW,
            "method": "synthetic-fixture",
            "sources": ["urn:test:source"],
            "ext": {},
        },
        "ext": {},
    }


def _partial_layer() -> dict:
    lifecycle = _lifecycle("partial", 0.5)
    actor = _actor("model", "ocr-model")
    return {
        "$schema": lib4.LAYER_SCHEMA_ID,
        "id": "transcription-machine",
        "revision": "ocr-r1",
        "kind": "transcription",
        "label": "Initial OCR",
        "language": "la",
        "lifecycle": lifecycle,
        "editorial_status": "machine-draft",
        "coverage": {
            "scope": "selection",
            "target_uris": ["lib4://package/herbal-test/canvas/canvas-1"],
            "completed_items": 1,
            "total_items": 2,
            "omissions": [{
                "target_uri": "lib4://package/herbal-test/canvas/canvas-2",
                "reason": "Service timeout",
                "ext": {},
            }],
            "ext": {},
        },
        "provenance": {
            "actor": actor,
            "generated_at": NOW,
            "method": "external-ocr",
            "run_id": "ocr-run-1",
            "parameters": {"language_hint": "la"},
            "sources": [{
                "target_uri": "lib4://package/herbal-test/canvas/canvas-1",
                "resource_id": "canvas-image-1",
                "layer_id": None,
                "revision": None,
                "digest": None,
                "ext": {},
            }],
            "ext": {},
        },
        "dependencies": [],
        "history": [{
            "id": "event-ocr-created",
            "action": "created",
            "actor": actor,
            "at": NOW,
            "message": "Initial partial OCR pass",
            "base_revision": None,
            "ext": {},
        }],
        "reviews": [],
        "errors": [{
            "code": "service-timeout",
            "message": "Second canvas timed out.",
            "target_uri": "lib4://package/herbal-test/canvas/canvas-2",
            "retryable": True,
            "at": NOW,
            "ext": {},
        }],
        "data": {
            "passages": [{
                "id": "passage-1",
                "canvas_id": "canvas-1",
                "canvas_revision": CANVAS_REVISION,
                "selector": {
                    "type": "box",
                    "coordinate_space": "canvas-normalized",
                    "canvas_revision": CANVAS_REVISION,
                    "x": 0.1,
                    "y": 0.1,
                    "width": 0.6,
                    "height": 0.2,
                },
                "text": "herba bona",
                "ext": {},
            }]
        },
        "ext": {},
    }


def _fixture() -> tuple[dict, dict[str, bytes]]:
    image = b"not actually an image"
    layer = _partial_layer()
    layer_payload = json.dumps(layer, ensure_ascii=False).encode("utf-8")
    resources = [
        _resource(
            "source-pdf",
            "source",
            "application/pdf",
            [{"type": "s3", "uri": "s3://public-books/herbal/source.pdf", "priority": 0, "ext": {}}],
        ),
        _resource(
            "canvas-image-1",
            "canvas-image",
            "image/jpeg",
            [
                {"type": "embedded", "member": "assets/pages/canvas-1.jpg", "priority": 0, "ext": {}},
                {
                    "type": "iiif",
                    "uri": "https://iiif.example.org/herbal/canvas-1/full/max/0/default.jpg",
                    "service": "https://iiif.example.org/herbal/canvas-1",
                    "priority": 1,
                    "ext": {},
                },
            ],
        ),
        _resource(
            "layer-transcription-ocr-r1",
            "layer",
            "application/json",
            [{
                "type": "embedded",
                "member": "layers/transcription-machine/ocr-r1.json",
                "priority": 0,
                "ext": {},
            }],
        ),
    ]
    manifest = {
        "$schema": lib4.MANIFEST_SCHEMA_ID,
        "format": lib4.FORMAT,
        "profile": lib4.PROFILE,
        "profile_version": lib4.PROFILE_VERSION,
        "package_id": "herbal-test",
        "package_revision": "package-r1",
        "created_at": NOW,
        "generator": _actor(),
        "catalog": _catalog(),
        "source_material": [{
            "id": "source-herbal-pdf",
            "material_type": "manuscript",
            "label": "Source PDF",
            "media_type": "application/pdf",
            "identifiers": [{"scheme": "urn", "value": "urn:test:herbal", "uri": "urn:test:herbal", "ext": {}}],
            "resource_ids": ["source-pdf"],
            "rights_policy_id": "rights-cc0",
            "ext": {},
        }],
        "structures": [{
            "id": "folio-1r",
            "revision": "structure-r1",
            "type": "folio",
            "label": "f. 1r",
            "parent_id": None,
            "order": 0,
            "target_uris": ["lib4://package/herbal-test/canvas/canvas-1"],
            "metadata": {"side": "recto"},
            "ext": {},
        }],
        "canvases": [{
            "id": "canvas-1",
            "revision": CANVAS_REVISION,
            "sequence": 1,
            "label": "f. 1r",
            "structure_ids": ["folio-1r"],
            "image_resource_id": "canvas-image-1",
            "dimensions": {"width": 1000, "height": 1500},
            "duration_ms": None,
            "ext": {},
        }],
        "resources": resources,
        "layers": [{
            "id": layer["id"],
            "revision": layer["revision"],
            "kind": layer["kind"],
            "label": layer["label"],
            "variant": "initial-ocr",
            "language": layer["language"],
            "current": True,
            "lifecycle": layer["lifecycle"],
            "editorial_status": layer["editorial_status"],
            "content_resource_id": "layer-transcription-ocr-r1",
            "supersedes": None,
            "ext": {},
        }],
        "releases": [{
            "id": "release-draft-1",
            "revision": "release-r1",
            "state": "draft",
            "created_at": NOW,
            "created_by": _actor("human", "editor-ada"),
            "previous_release_id": None,
            "layer_pins": [{"layer_id": layer["id"], "revision": layer["revision"]}],
            "resource_ids": ["canvas-image-1"],
            "catalog_revision": "release-1",
            "citation": "A herbal, draft Living Edition r1.",
            "policy": {"approved_layers_only": False, "include_machine_drafts": True, "ext": {}},
            "ext": {},
        }],
        "registries": {
            "layer_kinds": [],
            "resource_roles": [],
            "structure_types": [],
            "material_types": [],
            "selector_types": [],
            "relation_predicates": [],
            "capabilities": [],
        },
        "capabilities": ["external-assets", "alternate-locations", "partial-processing", "release-pins"],
        "ext": {},
    }
    return manifest, {
        "assets/pages/canvas-1.jpg": image,
        "layers/transcription-machine/ocr-r1.json": layer_payload,
    }


def _errors(issues: list[lib4.Issue]) -> list[lib4.Issue]:
    return [issue for issue in issues if issue.level == "error"]


def test_partial_machine_layer_and_remote_source_validate() -> None:
    manifest, payloads = _fixture()
    # Seal derives embedded digests. Validate the authoring manifest without
    # resource bytes first; placeholder integrity is allowed only until sealing.
    assert not _errors(lib4.validate_manifest(manifest))
    assert lib4.validate_layer(_partial_layer(), manifest, manifest["layers"][0]) == []
    assert payloads


def test_deterministic_seal_read_and_remote_only_resolution() -> None:
    manifest, payloads = _fixture()
    first = io.BytesIO()
    second = io.BytesIO()
    finalized = lib4.seal_archive(manifest, payloads, first)
    lib4.seal_archive(manifest, payloads, second)
    assert first.getvalue() == second.getvalue()
    assert not _errors(lib4.validate_archive(first.getvalue()))
    document = lib4.read_archive(first.getvalue())
    assert document.resource_bytes("canvas-image-1") == payloads["assets/pages/canvas-1.jpg"]
    assert document.resource_bytes("source-pdf") is None
    assert finalized["resources"][0]["id"] == "canvas-image-1"
    with zipfile.ZipFile(io.BytesIO(first.getvalue())) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        instructions = archive.read("INSTRUCTIONS.md").decode("utf-8")
        assert "planned" in instructions and "Retrieval chunks" in instructions
        assert "schemas/lib4-manifest.schema.json" in names


def test_signed_or_credential_bearing_remote_location_is_rejected() -> None:
    manifest, _ = _fixture()
    manifest["resources"][0]["locations"][0] = {
        "type": "http",
        "uri": "https://example.org/source.pdf?X-Amz-Signature=secret",
        "priority": 0,
        "ext": {},
    }
    issues = lib4.validate_manifest(manifest)
    assert any("query strings" in issue.message for issue in issues)


def test_embedded_resource_requires_verified_integrity_when_validating_bytes() -> None:
    manifest, payloads = _fixture()
    members = {
        "manifest.json": b"{}",
        "INSTRUCTIONS.md": b"",
        "checksums.sha256": b"",
        **payloads,
    }
    issues = lib4.validate_manifest(manifest, members)
    assert any("embedded resources require" in issue.message for issue in issues)


def test_published_release_cannot_pin_partial_machine_draft() -> None:
    manifest, _ = _fixture()
    release = manifest["releases"][0]
    release["state"] = "published"
    release["policy"] = {"approved_layers_only": True, "include_machine_drafts": False, "ext": {}}
    issues = lib4.validate_manifest(manifest)
    messages = [issue.message for issue in issues]
    assert "published releases may pin only complete layers" in messages
    assert "published releases may pin only approved or frozen layers" in messages


def test_human_approval_is_exact_and_requires_complete_layer() -> None:
    manifest, _ = _fixture()
    layer = _partial_layer()
    layer["editorial_status"] = "approved"
    layer["reviews"] = [{
        "id": "review-1",
        "decision": "approve",
        "reviewer": _actor("human", "editor-ada"),
        "at": NOW,
        "rationale": "Checked",
        "applies_to_revision": layer["revision"],
        "ext": {},
    }]
    issues = lib4.validate_layer(layer, manifest)
    assert any("must be complete" in issue.message for issue in issues)


def test_resource_policy_ids_must_resolve_to_inline_catalog() -> None:
    manifest, _ = _fixture()
    manifest["resources"][0]["rights_policy_id"] = "rights-missing"
    manifest["resources"][0]["access_policy_id"] = "access-missing"
    messages = [issue.message for issue in lib4.validate_manifest(manifest)]
    assert "must reference an inline catalog rights policy" in messages
    assert "must reference an inline catalog access policy" in messages


def test_archive_rejects_traversal_and_undeclared_members() -> None:
    manifest, payloads = _fixture()
    sealed = io.BytesIO()
    lib4.seal_archive(manifest, payloads, sealed)
    with zipfile.ZipFile(io.BytesIO(sealed.getvalue())) as source:
        members = {name: source.read(name) for name in source.namelist()}
    damaged = io.BytesIO()
    with zipfile.ZipFile(damaged, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)
        archive.writestr("../escape.txt", b"no")
    assert any("unsafe" in issue.message for issue in lib4.validate_archive(damaged.getvalue()))


def test_sealer_does_not_fetch_remote_assets_or_accept_undeclared_payloads() -> None:
    manifest, payloads = _fixture()
    with pytest.raises(lib4.Lib4Error, match="undeclared"):
        lib4.seal_archive(manifest, {**payloads, "sources/leak.txt": b"secret"}, io.BytesIO())


def test_schema_documents_are_draft_2020_12_json() -> None:
    for filename in ("lib4-manifest.schema.json", "lib4-layer.schema.json"):
        schema = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_format_detection_supports_explicit_legacy_migration_routing() -> None:
    legacy = io.BytesIO()
    with zipfile.ZipFile(legacy, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "whled/0.1"}))
    assert lib4.detect_format(legacy.getvalue()) == "whled/0.1"


def test_reference_cli_build_validate_and_inspect_round_trip(tmp_path: Path) -> None:
    example = ROOT / "examples" / "lib4" / "minimal"
    archive = tmp_path / "minimal.lib4"
    command = [sys.executable, str(ROOT / "tools" / "living_edition" / "build_lib4.py")]
    built = subprocess.run(
        [*command, "build", str(example / "manifest.json"), str(archive), "--resource-root", str(example)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    validated = subprocess.run(
        [*command, "validate", str(archive), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr or validated.stdout
    assert json.loads(validated.stdout) == []
    inspected = subprocess.run(
        [*command, "inspect", str(archive)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert inspected.returncode == 0, inspected.stderr or inspected.stdout
    summary = json.loads(inspected.stdout)
    assert summary["format"] == "lib/4"
    assert summary["canvases"] == 1
    assert len(summary["layers"]) == 3
