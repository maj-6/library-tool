"""Framework-free module/capability discovery contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from librarytool.engine.capabilities import (
    CapabilityRef,
    CapabilityResolution,
    CapabilityRegistry,
    DuplicateManifestError,
    ManifestValidationError,
    ModuleManifest,
    WorkbenchManifest,
)
from librarytool.engine.errors import ConflictError
from librarytool.engine.item_commands import (
    DeleteItemCommand as CatalogueDeleteItemCommand,
)
from librarytool.engine.item_lifecycle import (
    DeleteItemCommand as LifecycleDeleteItemCommand,
    RestoreItemCommand,
)
from librarytool.engine.runtime import (
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_LIFECYCLE_SERVICE,
    ITEM_QUERY_SERVICE,
    JOB_SERVICE,
    LIB_OPEN_SERVICE,
    RASTER_ARTIFACT_QUERY_SERVICE,
    REPLICA_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    SPATIAL_ANNOTATION_QUERY_SERVICE,
    TEXT_LAYER_SERVICE,
    TRANSLATION_PROVENANCE_SERVICE,
    TRANSLATION_SERVICE,
    LibraryEngine,
)


ITEMS = CapabilityRef("items.read", 1)
REGIONS = CapabilityRef("replica.regions.edit", 2)
LAYOUT = CapabilityRef("replica.layout.propose", 1)
TRANSLATE = CapabilityRef("translation.layer.generate", 1)


def _module(module_id, *, provides=(), requires=(), enhances=()):
    return ModuleManifest(
        id=module_id,
        version="1.0.0",
        provides=provides,
        requires=requires,
        enhances=enhances,
    )


def _row(document, collection, row_id):
    return next(row for row in document[collection] if row["id"] == row_id)


@pytest.mark.parametrize("bad_id", ["", "Upper.Case", "two spaces", ".bad"])
def test_manifest_ids_are_portable_and_versions_are_semantic(bad_id):
    with pytest.raises(ManifestValidationError, match="module id"):
        ModuleManifest(id=bad_id, version="1.0.0")
    with pytest.raises(ManifestValidationError, match="semantic version"):
        WorkbenchManifest(id="workbench.valid", version="1")
    with pytest.raises(ManifestValidationError, match="positive integer"):
        CapabilityRef("items.read", 0)


def test_capability_versions_are_exact_across_the_json_boundary():
    maximum = 9_007_199_254_740_991
    capability = CapabilityRef("items.read", maximum)
    document = CapabilityRegistry(modules=(
        _module("provider.items", provides=(capability,)),
    )).discovery_document()

    encoded = json.dumps(document)
    assert json.loads(encoded)["capabilities"][0]["version"] == maximum
    with pytest.raises(ManifestValidationError, match="JSON-safe"):
        CapabilityRef("items.read", maximum + 1)


def test_manifest_dependencies_are_immutable_unique_and_unambiguous():
    original = [ITEMS, REGIONS]
    manifest = ModuleManifest(
        id="provider.replica",
        version="1.2.0-alpha.1",
        provides=original,
    )
    original.clear()
    assert manifest.provides == (ITEMS, REGIONS)

    with pytest.raises(ManifestValidationError, match="duplicate capability"):
        ModuleManifest(
            id="provider.duplicate-capability",
            version="1.0.0",
            provides=(ITEMS, ITEMS),
        )
    with pytest.raises(ManifestValidationError, match="required and optional"):
        WorkbenchManifest(
            id="workbench.ambiguous",
            version="1.0.0",
            requires=(ITEMS,),
            enhances=(ITEMS,),
        )


def test_public_resolution_normalizes_and_freezes_its_values():
    resolution = CapabilityResolution(
        ["z.module", "a.module"],
        [REGIONS, ITEMS],
    )
    assert resolution.active_module_ids == ("a.module", "z.module")
    assert resolution.capabilities == (ITEMS, REGIONS)

    with pytest.raises(ManifestValidationError, match="duplicate module"):
        CapabilityResolution(["a.module", "a.module"], [ITEMS])


def test_duplicate_provider_id_is_rejected_but_alternatives_are_supported():
    registry = CapabilityRegistry()
    registry.register_module(_module("provider.layout.local", provides=(LAYOUT,)))
    with pytest.raises(DuplicateManifestError, match="duplicate module id"):
        registry.register_module(
            _module("provider.layout.local", provides=(LAYOUT,))
        )

    # Several distinct implementations may satisfy the same capability. The
    # discovery contract names all of them in deterministic provider order.
    registry.register_module(_module("provider.layout.remote", provides=(LAYOUT,)))
    capability = registry.discovery_document()["capabilities"][0]
    assert capability == {
        "id": "replica.layout.propose",
        "version": 1,
        "providers": ["provider.layout.local", "provider.layout.remote"],
    }


def test_missing_dependency_blocks_module_and_suppresses_its_capabilities():
    registry = CapabilityRegistry(modules=(
        _module("provider.regions", provides=(REGIONS,), requires=(ITEMS,)),
        _module("provider.layout", provides=(LAYOUT,), requires=(REGIONS,)),
    ))

    document = registry.discovery_document()
    regions = _row(document, "modules", "provider.regions")
    layout = _row(document, "modules", "provider.layout")
    assert regions["status"] == "blocked"
    assert regions["available"] is False
    assert regions["missing_required"] == [{"id": "items.read", "version": 1}]
    assert layout["status"] == "blocked"
    assert layout["missing_required"] == [
        {"id": "replica.regions.edit", "version": 2}
    ]
    assert document["capabilities"] == []


def test_optional_dependencies_degrade_but_do_not_disable_module():
    registry = CapabilityRegistry(modules=(
        _module("core.items", provides=(ITEMS,)),
        _module(
            "provider.regions",
            provides=(REGIONS,),
            requires=(ITEMS,),
            enhances=(LAYOUT,),
        ),
    ))

    document = registry.discovery_document()
    regions = _row(document, "modules", "provider.regions")
    assert regions["status"] == "degraded"
    assert regions["available"] is True
    assert regions["missing_required"] == []
    assert regions["missing_optional"] == [
        {"id": "replica.layout.propose", "version": 1}
    ]
    assert any(row["id"] == REGIONS.id for row in document["capabilities"])


def test_workbench_visibility_uses_hard_requirements_only():
    workbench = WorkbenchManifest(
        id="workbench.facsimile",
        version="2.1.0",
        requires=(ITEMS, REGIONS),
        enhances=(LAYOUT, TRANSLATE),
    )

    blocked = CapabilityRegistry(
        modules=(_module("core.items", provides=(ITEMS,)),),
        workbenches=(workbench,),
    ).discovery_document()
    row = _row(blocked, "workbenches", workbench.id)
    assert row["visible"] is False
    assert row["status"] == "blocked"
    assert row["missing_required"] == [REGIONS.as_dict()]

    degraded = CapabilityRegistry(
        modules=(
            _module("core.items", provides=(ITEMS,)),
            _module("core.replica", provides=(REGIONS,), requires=(ITEMS,)),
        ),
        workbenches=(workbench,),
    ).discovery_document()
    row = _row(degraded, "workbenches", workbench.id)
    assert row["visible"] is True
    assert row["status"] == "degraded"
    assert row["missing_optional"] == [LAYOUT.as_dict(), TRANSLATE.as_dict()]

    available = CapabilityRegistry(
        modules=(
            _module("core.items", provides=(ITEMS,)),
            _module("core.replica", provides=(REGIONS,), requires=(ITEMS,)),
            _module("provider.layout", provides=(LAYOUT,), requires=(REGIONS,)),
            _module("provider.translation", provides=(TRANSLATE,), requires=(ITEMS,)),
        ),
        workbenches=(workbench,),
    ).discovery_document()
    row = _row(available, "workbenches", workbench.id)
    assert row["visible"] is True
    assert row["status"] == "available"
    assert row["missing_optional"] == []


def test_discovery_is_json_friendly_and_registration_order_independent():
    modules = (
        _module("z.provider.layout", provides=(LAYOUT,), requires=(REGIONS,)),
        _module("a.core.items", provides=(ITEMS,)),
        _module("m.core.regions", provides=(REGIONS,), requires=(ITEMS,)),
        _module("b.provider.layout", provides=(LAYOUT,), requires=(REGIONS,)),
    )
    workbenches = (
        WorkbenchManifest("workbench.research", "1.0.0", requires=(ITEMS,)),
        WorkbenchManifest(
            "workbench.facsimile", "1.0.0", requires=(REGIONS,),
            enhances=(TRANSLATE,),
        ),
    )

    forward = CapabilityRegistry(modules, workbenches).discovery_document()
    reverse = CapabilityRegistry(
        reversed(modules), reversed(workbenches)
    ).discovery_document()

    assert forward == reverse
    assert [row["id"] for row in forward["modules"]] == sorted(
        row["id"] for row in forward["modules"]
    )
    assert [row["id"] for row in forward["workbenches"]] == sorted(
        row["id"] for row in forward["workbenches"]
    )
    json.dumps(forward, allow_nan=False)


def test_http_discovery_exposes_resolved_installed_workbenches(client):
    response = client.get("/api/v1/capabilities")
    assert response.status_code == 200
    document = response.get_json()
    assert document["ok"] is True
    assert document["schema"] == "librarytool.capabilities/1"
    assert {row["id"] for row in document["workbenches"] if row["visible"]} == {
        "catalog", "corrections", "replica"}
    capabilities = {
        (row["id"], row["version"]) for row in document["capabilities"]}
    assert ("replica.regions", 1) in capabilities
    assert ("replica.interchange", 2) in capabilities
    assert ("replica.interchange.open", 1) in capabilities
    assert ("library.jobs", 1) in capabilities
    assert ("library.raster-artifacts.read", 1) in capabilities
    assert ("library.spatial-annotations.read", 1) in capabilities


def test_import_does_not_claim_or_open_the_engine_workspace(tmp_path):
    root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["WHL_DATA_ROOT"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join((
        str(root / "src"),
        str(root / "tools"),
        str(root / "tools" / "whl_explorer"),
    ))
    script = "\n".join((
        "from concurrent.futures import ThreadPoolExecutor",
        "import importlib",
        "from pathlib import Path",
        "import server",
        "assert server._engine_session is None",
        "lock = (server.lib.OUTPUT_DIR / '.transactions' / "
        "'engine-session.lock')",
        "assert not Path(lock).exists(), lock",
        "client = server.app.test_client()",
        "rejected = client.get('/api/v1/capabilities', "
        "headers={'Host': 'attacker.example'})",
        "assert rejected.status_code == 403",
        "assert server._engine_session is None",
        "assert not Path(lock).exists(), lock",
        "real_open = server._open_engine_session",
        "opens = []",
        "def tracked_open():",
        "    opens.append(1)",
        "    return real_open()",
        "server._open_engine_session = tracked_open",
        "def request_capabilities(_index):",
        "    response = server.app.test_client().get('/api/v1/capabilities')",
        "    return response.status_code",
        "with ThreadPoolExecutor(max_workers=8) as pool:",
        "    statuses = list(pool.map(request_capabilities, range(16)))",
        "assert statuses == [200] * 16, statuses",
        "assert len(opens) == 1, opens",
        "session = server._engine_session",
        "assert session is not None and not session.closed",
        "assert server._library_engine_instance is session.engine",
        "assert server._engine_write_set is session.write_set",
        "assert server._job_manager is session.jobs",
        "assert server._translation_provenance is session.provenance",
        "server._close_engine_session()",
        "assert server._engine_session is None",
        "assert server._library_engine_instance is None",
        "reopened = server._ensure_engine_session()",
        "assert reopened is not session and not reopened.closed",
        "assert len(opens) == 2, opens",
        "server._close_engine_session()",
        "server = importlib.reload(server)",
        "assert server._engine_session is None",
        "third = server._ensure_engine_session()",
        "assert not third.closed",
        "server._close_engine_session()",
    ))

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_production_services_and_capabilities_are_one_sealed_graph(client):
    import server

    engine = server._library_engine()
    assert server._ensure_engine_session() is server._engine_session
    assert server._engine_session.closed is False
    assert engine is server._engine_session.engine
    assert server._engine_write_set is server._engine_session.write_set
    assert server._job_manager is server._engine_session.jobs
    assert (
        server._translation_provenance
        is server._engine_session.provenance
    )
    assert server._jobs is server._engine_session.jobs.records
    assert server._jobs_events is server._engine_session.jobs.cancel_events
    assert server._jobs_lock is server._engine_session.jobs.lock
    assert server._library_engine_instance is engine
    assert engine.capabilities.sealed is True
    raster_artifacts = engine.require_service(
        RASTER_ARTIFACT_QUERY_SERVICE
    )
    assert raster_artifacts is engine.require_service(
        SPATIAL_ANNOTATION_QUERY_SERVICE
    )
    assert server._engine_session.raster_resource_resolver is raster_artifacts
    assert engine.items is not None
    assert engine.item_commands._allow_legacy_delete is False
    assert {
        "library.items.lifecycle.read",
        "library.items.delete",
        "library.items.restore",
    } <= {
        row["id"] for row in engine.discovery_document()["capabilities"]
    }
    assert {policy.policy_id for policy in engine.items.policies} == {
        "catalogue-commands",
        "item-lifecycle",
        "replica",
        "representation-commands",
        "text-layers",
        "translations",
    }
    context = SimpleNamespace(
        title="A book",
        representations=(
            SimpleNamespace(
                available=True,
                media_type="application/pdf",
            ),
        ),
        artifact_readiness=lambda _kinds: "current",
    )
    commands = {
        command
        for policy in engine.items.policies
        for command in policy.contribute(context).available_commands
    }
    assert commands == {
        "item.delete",
        "item.metadata.edit",
        "replica.open",
        "representation.attach",
        "representation.detach",
        "representation.replace",
    }
    for key, service in (
        (ITEM_QUERY_SERVICE, engine.items),
        (ITEM_COMMAND_SERVICE, engine.item_commands),
        (
            ITEM_LIFECYCLE_SERVICE,
            engine.require_service(ITEM_LIFECYCLE_SERVICE),
        ),
        (INTERCHANGE_SERVICE, engine.interchange),
        (LIB_OPEN_SERVICE, engine.require_service(LIB_OPEN_SERVICE)),
        (JOB_SERVICE, engine.jobs),
        (REPLICA_SERVICE, engine.replica),
        (
            REPRESENTATION_COMMAND_SERVICE,
            engine.require_service(REPRESENTATION_COMMAND_SERVICE),
        ),
        (TEXT_LAYER_SERVICE, engine.text_layers),
        (TRANSLATION_SERVICE, engine.translations),
        (
            TRANSLATION_PROVENANCE_SERVICE,
            engine.translation_provenance,
        ),
    ):
        assert service is not None
        assert engine.require_service(key) is service


def test_production_lifecycle_restores_exact_raw_build_with_fresh_revision(
    monkeypatch,
    tmp_path,
):
    import server

    root = tmp_path / "output"
    builds_path = root / "whl_builds.json"
    entries_dir = root / "entries"
    root.mkdir(parents=True)
    external = tmp_path / "private" / "source.pdf"
    external.parent.mkdir()
    external.write_bytes(b"%PDF-private")
    source_stat = {
        "size": 12,
        "mtime_ns": 1,
        "ctime_ns": 2,
        "device": 3,
        "inode": 4,
    }
    raw = {
        "title": "The Exact Herbal",
        "authors": "Ada Curator",
        "rights": "public-domain",
        "status": "draft",
        "created_at": "2026-01-01T00:00:00.000000+00:00",
        # A future token proves restoration advances the prior revision even
        # when the wall clock cannot provide a later timestamp.
        "updated_at": "2099-01-01T00:00:00.000000+00:00",
        "pdf_file": str(external),
        "pdf_sources": [],
        "images": ["capture/cover.jpg"],
        "extra": {"workspace_path": str(tmp_path / "private")},
        "capture_id": "phone-1",
        "relevance": {"score": 0.75},
        "future.extension": {"nested": [1, True, None]},
        "representation_manifest": {
            "version": 1,
            "sources": {
                "primary": {
                    "role": "primary",
                    "media_type": "application/pdf",
                    "label": "Private source",
                    "acquisition": "reference",
                    "content_sha256": "a" * 64,
                    "size": 12,
                    "source_stat": source_stat,
                    "metadata": {"future": {"preserve": True}},
                },
            },
            "detached": ["scan-two"],
        },
    }
    server.lib.save_json(builds_path, {"book-one": raw})
    entry = entries_dir / "book-one"
    entry.mkdir(parents=True)
    (entry / "owned.txt").write_bytes(b"managed bytes")
    monkeypatch.setattr(server, "BUILDS_PATH", builds_path)
    monkeypatch.setattr(server, "ENTRIES_DIR", entries_dir)

    session = server._open_engine_session(root)
    try:
        engine = session.engine
        lifecycle = engine.require_service(ITEM_LIFECYCLE_SERVICE)
        module = next(
            row
            for row in engine.discovery_document()["modules"]
            if row["id"] == "library.item-lifecycle.commands"
        )
        assert module["status"] == "available"
        assert engine.item_commands._allow_legacy_delete is False

        state = lifecycle.inspect("book-one")
        assert state.item.revision == raw["updated_at"]
        with pytest.raises(ConflictError) as legacy:
            engine.item_commands.delete(CatalogueDeleteItemCommand(
                item_id="book-one",
                expected_revision=state.item.revision,
                operation_id="legacy-delete-1",
            ))
        assert legacy.value.code == "item_lifecycle_command_required"

        deleted = lifecycle.delete(LifecycleDeleteItemCommand(
            item_id="book-one",
            expected_item_revision=state.item.revision,
            expected_managed_tree_revision=state.managed_tree.revision,
            operation_id="lifecycle-delete-1",
        ))
        assert server.lib.load_json(builds_path, {}) == {}
        assert not entry.exists()
        assert external.read_bytes() == b"%PDF-private"

        restored = lifecycle.restore(RestoreItemCommand(
            tombstone_id=deleted.receipt.tombstone.tombstone_id,
            expected_tombstone_revision=(
                deleted.receipt.tombstone.revision
            ),
            operation_id="lifecycle-restore-1",
        ))
        restored_raw = server.lib.load_json(builds_path, {})["book-one"]
        expected = deepcopy(raw)
        expected["id"] = "book-one"
        expected["updated_at"] = restored.receipt.restored_item_revision

        assert restored_raw == expected
        assert restored_raw["updated_at"] != raw["updated_at"]
        assert restored_raw["updated_at"] == (
            "2099-01-01T00:00:00.000001+00:00"
        )
        assert restored_raw["representation_manifest"] == (
            raw["representation_manifest"]
        )
        assert (entry / "owned.txt").read_bytes() == b"managed bytes"
        assert external.read_bytes() == b"%PDF-private"
    finally:
        session.close()


def test_library_engine_exposes_the_same_framework_neutral_discovery():
    registry = CapabilityRegistry(modules=(
        _module("core.items", provides=(ITEMS,)),
    ))
    engine = LibraryEngine(capabilities=registry)
    assert engine.replica is None
    assert engine.discovery_document() == registry.discovery_document()
