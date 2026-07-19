"""Framework-free module/capability discovery contracts."""

from __future__ import annotations

import json

import pytest

from librarytool.engine.capabilities import (
    CapabilityRef,
    CapabilityRegistry,
    DuplicateManifestError,
    ManifestValidationError,
    ModuleManifest,
    WorkbenchManifest,
)
from librarytool.engine.runtime import LibraryEngine


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
        "catalog", "replica"}
    capabilities = {
        (row["id"], row["version"]) for row in document["capabilities"]}
    assert ("replica.regions", 1) in capabilities
    assert ("replica.interchange", 2) in capabilities


def test_library_engine_exposes_the_same_framework_neutral_discovery():
    registry = CapabilityRegistry(modules=(
        _module("core.items", provides=(ITEMS,)),
    ))
    engine = LibraryEngine(capabilities=registry)
    assert engine.replica is None
    assert engine.discovery_document() == registry.discovery_document()
