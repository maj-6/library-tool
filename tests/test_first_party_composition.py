"""First-party product composition stays deterministic and host-neutral."""

from __future__ import annotations

from itertools import product

import pytest

from librarytool.composition import (
    FIRST_PARTY_MODULE_MANIFESTS,
    FIRST_PARTY_WORKBENCH_MANIFESTS,
    FilesystemServiceGraph,
    first_party_module_contributions,
)
from librarytool.engine.capabilities import CapabilityRef
from librarytool.engine.items import ItemQueryService
from librarytool.engine.runtime import (
    CANVAS_PREPARATION_SERVICE,
    CANVAS_QUERY_SERVICE,
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_LIFECYCLE_SERVICE,
    JOB_SERVICE,
    LIB_OPEN_SERVICE,
    REPLICA_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    TEXT_LAYER_AGGREGATE_SERVICE,
    TEXT_LAYER_SERVICE,
    TRANSLATION_PROVENANCE_SERVICE,
    TRANSLATION_SERVICE,
    LibraryEngineBuilder,
)


class _EmptyItemRepository:
    def list_records(self):
        return ()

    def get_record(self, _item_id):
        return None

    def list_representation_records(self, _item_id, _item_record=None):
        return ()

    def list_artifact_records(self, _item_id, _item_record=None):
        return ()


def _graph(
    *,
    representation: bool = True,
    lifecycle: bool = True,
    lib_open: bool = True,
    canvases: bool = True,
    text_layer_aggregate: bool = True,
) -> FilesystemServiceGraph:
    return FilesystemServiceGraph(
        items=ItemQueryService(_EmptyItemRepository()),
        item_commands=object(),
        item_lifecycle=object() if lifecycle else None,
        representation_commands=object() if representation else None,
        interchange=object(),
        lib_open=object() if lib_open else None,
        jobs=object(),
        replica=object(),
        text_layers=object(),
        translations=object(),
        translation_provenance=object(),
        canvas_query=object() if canvases else None,
        canvas_preparation=object() if canvases else None,
        text_layer_aggregate=(object() if text_layer_aggregate else None),
    )


def _capabilities(values) -> set[tuple[str, int]]:
    return {(value.id, value.version) for value in values}


def _capability_ids(document) -> set[str]:
    return {row["id"] for row in document["capabilities"]}


def test_first_party_manifests_preserve_the_production_product_contract():
    modules = {
        manifest.id: manifest for manifest in FIRST_PARTY_MODULE_MANIFESTS
    }
    assert {
        module_id: manifest.version for module_id, manifest in modules.items()
    } == {
        "library.core": "1.1.0",
        "jobs.core": "1.0.0",
        "library.catalogue.commands": "1.0.0",
        "library.representation.commands": "1.0.0",
        "library.item-lifecycle.commands": "1.0.0",
        "library.canvases": "1.0.0",
        "library.text-layers": "1.0.0",
        "replica.core": "1.0.0",
        "translation.core": "2.0.0",
        "replica.lib": "2.0.0",
        "replica.lib-open": "1.0.0",
    }
    assert _capabilities(modules["library.core"].provides) == {
        ("library.items", 1),
        ("library.items.read", 1),
        ("library.representations", 1),
        ("library.artifacts", 1),
    }
    assert _capabilities(
        modules["library.catalogue.commands"].provides
    ) == {
        ("library.items.create", 1),
        ("library.items.update", 1),
    }
    assert _capabilities(
        modules["library.representation.commands"].provides
    ) == {
        ("library.representations.attach", 1),
        ("library.representations.replace", 1),
        ("library.representations.detach", 1),
    }
    lifecycle = modules["library.item-lifecycle.commands"]
    assert _capabilities(lifecycle.provides) == {
        ("library.items.lifecycle.read", 1),
        ("library.items.delete", 1),
        ("library.items.restore", 1),
    }
    assert _capabilities(lifecycle.requires) == {
        ("library.items.read", 1),
        ("library.jobs", 1),
    }
    canvases = modules["library.canvases"]
    assert _capabilities(canvases.provides) == {
        ("library.canvases.read", 1),
        ("library.canvases.prepare", 1),
    }
    assert _capabilities(canvases.requires) == {
        ("library.items.read", 1),
        ("library.representations", 1),
    }
    native_text_layers = modules["library.text-layers"]
    assert _capabilities(native_text_layers.provides) == {
        ("library.text-layers.read", 1),
        ("library.text-layers.edit", 1),
    }
    assert _capabilities(native_text_layers.requires) == {
        ("library.items.read", 1),
        ("library.representations", 1),
    }
    assert _capabilities(modules["replica.core"].provides) == {
        ("replica.regions", 1),
        ("replica.proposals", 1),
        ("replica.text-layers", 1),
        ("replica.layout-families", 1),
    }
    assert _capabilities(modules["translation.core"].provides) == {
        ("translation.provenance", 1),
        ("translation.layers.read", 1),
        ("translation.layers.status", 1),
        ("translation.layers.edit", 1),
    }
    assert _capabilities(modules["replica.lib"].provides) == {
        ("replica.interchange", 2),
    }
    assert _capabilities(modules["replica.lib-open"].requires) == {
        ("replica.interchange", 2),
        ("library.items.create", 1),
    }

    workbenches = {
        manifest.id: manifest for manifest in FIRST_PARTY_WORKBENCH_MANIFESTS
    }
    assert set(workbenches) == {"catalog", "replica"}
    assert _capabilities(workbenches["catalog"].requires) == {
        ("library.items", 1),
    }
    assert _capabilities(workbenches["catalog"].enhances) == {
        ("library.items.create", 1),
        ("library.items.update", 1),
        ("library.items.lifecycle.read", 1),
        ("library.items.delete", 1),
        ("library.items.restore", 1),
        ("library.representations.attach", 1),
        ("library.representations.replace", 1),
        ("library.representations.detach", 1),
    }
    assert _capabilities(workbenches["replica"].requires) == {
        ("replica.regions", 1),
        ("replica.text-layers", 1),
    }
    assert _capabilities(workbenches["replica"].enhances) == {
        ("replica.interchange", 2),
        ("replica.interchange.open", 1),
        ("replica.layout-families", 1),
        ("translation.provenance", 1),
        ("translation.layers.read", 1),
        ("translation.layers.status", 1),
        ("translation.layers.edit", 1),
        ("library.jobs", 1),
        ("library.canvases.read", 1),
        ("library.canvases.prepare", 1),
    }


def test_full_first_party_graph_binds_every_service_and_policy():
    graph = _graph()
    contributions = first_party_module_contributions(graph)
    assert tuple(value.manifest.id for value in contributions) == (
        "library.core",
        "jobs.core",
        "library.catalogue.commands",
        "library.representation.commands",
        "library.item-lifecycle.commands",
        "library.canvases",
        "library.text-layers",
        "replica.core",
        "translation.core",
        "replica.lib",
        "replica.lib-open",
    )
    binding_keys = {
        contribution.manifest.id: tuple(
            binding.key.token for binding in contribution.bindings
        )
        for contribution in contributions
    }
    assert binding_keys == {
        "library.core": ("library.items.query@1",),
        "jobs.core": ("library.jobs@1",),
        "library.catalogue.commands": ("library.items.commands@1",),
        "library.representation.commands": (
            "library.representations.commands@1",
        ),
        "library.item-lifecycle.commands": ("library.items.lifecycle@1",),
        "library.canvases": (
            "library.canvases.prepare@1",
            "library.canvases.query@1",
        ),
        "library.text-layers": ("library.text-layers.aggregate@1",),
        "replica.core": ("replica.application@1", "replica.text-layers@1"),
        "translation.core": (
            "translation.application@1",
            "translation.provenance@1",
        ),
        "replica.lib": ("replica.interchange@1",),
        "replica.lib-open": ("replica.interchange.open@1",),
    }
    policy_bindings = {
        contribution.manifest.id: {
            binding.policy.policy_id: _capabilities(binding.requires)
            for binding in contribution.item_policies
        }
        for contribution in contributions
        if contribution.item_policies
    }
    assert policy_bindings == {
        "library.catalogue.commands": {
            "catalogue-commands": {("library.items.update", 1)},
        },
        "library.representation.commands": {
            "representation-commands": {
                ("library.representations.attach", 1)
            },
        },
        "library.item-lifecycle.commands": {
            "item-lifecycle": {("library.items.delete", 1)},
        },
        "replica.core": {
            "replica": {("replica.regions", 1)},
            "text-layers": {("replica.text-layers", 1)},
        },
        "translation.core": {
            "translations": {("translation.layers.status", 1)},
        },
    }

    engine = LibraryEngineBuilder(contributions).build()
    document = engine.discovery_document()
    assert all(row["status"] == "available" for row in document["modules"])
    assert all(
        row["status"] == "available" for row in document["workbenches"]
    )
    assert engine.require_service(ITEM_COMMAND_SERVICE) is graph.item_commands
    assert engine.require_service(CANVAS_QUERY_SERVICE) is graph.canvas_query
    assert engine.require_service(
        CANVAS_PREPARATION_SERVICE
    ) is graph.canvas_preparation
    assert engine.require_service(ITEM_LIFECYCLE_SERVICE) is graph.item_lifecycle
    assert engine.require_service(JOB_SERVICE) is graph.jobs
    assert engine.require_service(
        REPRESENTATION_COMMAND_SERVICE
    ) is graph.representation_commands
    assert engine.require_service(INTERCHANGE_SERVICE) is graph.interchange
    assert engine.require_service(LIB_OPEN_SERVICE) is graph.lib_open
    assert engine.require_service(REPLICA_SERVICE) is graph.replica
    assert engine.require_service(TEXT_LAYER_SERVICE) is graph.text_layers
    assert engine.require_service(
        TEXT_LAYER_AGGREGATE_SERVICE
    ) is graph.text_layer_aggregate
    assert engine.text_layers is graph.text_layers
    assert engine.require_service(TRANSLATION_SERVICE) is graph.translations
    assert engine.require_service(
        TRANSLATION_PROVENANCE_SERVICE
    ) is graph.translation_provenance
    assert {policy.policy_id for policy in engine.items.policies} == {
        "catalogue-commands",
        "item-lifecycle",
        "replica",
        "representation-commands",
        "text-layers",
        "translations",
    }


@pytest.mark.parametrize(
    (
        "representation",
        "lifecycle",
        "lib_open",
        "canvases",
        "text_layer_aggregate",
    ),
    tuple(product((False, True), repeat=5)),
)
def test_optional_modules_are_independent_deterministic_and_withheld(
    representation,
    lifecycle,
    lib_open,
    canvases,
    text_layer_aggregate,
):
    graph = _graph(
        representation=representation,
        lifecycle=lifecycle,
        lib_open=lib_open,
        canvases=canvases,
        text_layer_aggregate=text_layer_aggregate,
    )
    contributions = first_party_module_contributions(graph)
    engine = LibraryEngineBuilder(contributions).build()
    document = engine.discovery_document()
    module_ids = {row["id"] for row in document["modules"]}
    capability_ids = _capability_ids(document)
    policies = {policy.policy_id for policy in engine.items.policies}

    assert ("library.representation.commands" in module_ids) is representation
    assert (
        "library.item-lifecycle.commands" in module_ids
    ) is lifecycle
    assert ("replica.lib-open" in module_ids) is lib_open
    assert ("library.canvases" in module_ids) is canvases
    assert ("library.text-layers" in module_ids) is text_layer_aggregate
    assert (
        engine.get_service(REPRESENTATION_COMMAND_SERVICE) is not None
    ) is representation
    assert (
        engine.get_service(ITEM_LIFECYCLE_SERVICE) is not None
    ) is lifecycle
    assert (engine.get_service(LIB_OPEN_SERVICE) is not None) is lib_open
    assert (engine.get_service(CANVAS_QUERY_SERVICE) is not None) is canvases
    assert (engine.get_service(CANVAS_PREPARATION_SERVICE) is not None) is canvases
    assert (
        engine.get_service(TEXT_LAYER_AGGREGATE_SERVICE) is not None
    ) is text_layer_aggregate
    assert ("representation-commands" in policies) is representation
    assert ("item-lifecycle" in policies) is lifecycle

    representation_capabilities = {
        "library.representations.attach",
        "library.representations.replace",
        "library.representations.detach",
    }
    lifecycle_capabilities = {
        "library.items.lifecycle.read",
        "library.items.delete",
        "library.items.restore",
    }
    assert bool(representation_capabilities & capability_ids) is representation
    assert bool(lifecycle_capabilities & capability_ids) is lifecycle
    assert (
        "replica.interchange.open" in capability_ids
    ) is lib_open
    assert (
        bool({"library.canvases.read", "library.canvases.prepare"} & capability_ids)
        is canvases
    )
    assert (
        bool(
            {"library.text-layers.read", "library.text-layers.edit"}
            & capability_ids
        )
        is text_layer_aggregate
    )

    workbenches = {row["id"]: row for row in document["workbenches"]}
    assert workbenches["catalog"]["visible"] is True
    assert workbenches["catalog"]["status"] == (
        "available" if representation and lifecycle else "degraded"
    )
    assert workbenches["replica"]["visible"] is True
    assert workbenches["replica"]["status"] == (
        "available" if lib_open and canvases else "degraded"
    )
    assert document == LibraryEngineBuilder(
        first_party_module_contributions(graph)
    ).build().discovery_document()


def test_absent_lifecycle_is_not_discoverable_or_bound():
    graph = _graph(lifecycle=False)
    contributions = first_party_module_contributions(graph)
    engine = LibraryEngineBuilder(contributions).build()
    document = engine.discovery_document()

    assert "library.item-lifecycle.commands" not in {
        row["id"] for row in document["modules"]
    }
    assert {
        "library.items.lifecycle.read",
        "library.items.delete",
        "library.items.restore",
    }.isdisjoint(_capability_ids(document))
    assert engine.get_service(ITEM_LIFECYCLE_SERVICE) is None
    assert "item-lifecycle" not in {
        policy.policy_id for policy in engine.items.policies
    }
    catalog = next(
        row for row in document["workbenches"] if row["id"] == "catalog"
    )
    assert {
        (row["id"], row["version"])
        for row in catalog["missing_optional"]
    } >= {
        ("library.items.lifecycle.read", 1),
        ("library.items.delete", 1),
        ("library.items.restore", 1),
    }
    assert CapabilityRef("library.items.delete") not in {
        capability
        for contribution in contributions
        for capability in contribution.manifest.provides
    }


def test_canvas_services_and_capabilities_are_withheld_as_one_vertical():
    graph = _graph(canvases=False)
    contributions = first_party_module_contributions(graph)
    engine = LibraryEngineBuilder(contributions).build()
    document = engine.discovery_document()

    assert "library.canvases" not in {row["id"] for row in document["modules"]}
    assert {
        "library.canvases.read",
        "library.canvases.prepare",
    }.isdisjoint(_capability_ids(document))
    assert engine.get_service(CANVAS_QUERY_SERVICE) is None
    assert engine.get_service(CANVAS_PREPARATION_SERVICE) is None
    replica = next(row for row in document["workbenches"] if row["id"] == "replica")
    assert {(row["id"], row["version"]) for row in replica["missing_optional"]} >= {
        ("library.canvases.read", 1),
        ("library.canvases.prepare", 1),
    }


def test_native_text_layer_service_and_capabilities_are_withheld_together():
    graph = _graph(text_layer_aggregate=False)
    contributions = first_party_module_contributions(graph)
    engine = LibraryEngineBuilder(contributions).build()
    document = engine.discovery_document()

    assert "library.text-layers" not in {
        row["id"] for row in document["modules"]
    }
    assert {
        "library.text-layers.read",
        "library.text-layers.edit",
    }.isdisjoint(_capability_ids(document))
    assert engine.get_service(TEXT_LAYER_AGGREGATE_SERVICE) is None
    assert engine.text_layers is graph.text_layers


def test_service_graph_rejects_half_installed_canvas_vertical():
    values = {
        "items": ItemQueryService(_EmptyItemRepository()),
        "item_commands": object(),
        "item_lifecycle": None,
        "representation_commands": None,
        "interchange": object(),
        "lib_open": None,
        "jobs": object(),
        "replica": object(),
        "text_layers": object(),
        "translations": object(),
        "translation_provenance": object(),
    }

    with pytest.raises(ValueError, match="must be installed together"):
        FilesystemServiceGraph(
            **values,
            canvas_query=object(),
            canvas_preparation=None,
        )
    with pytest.raises(ValueError, match="must be installed together"):
        FilesystemServiceGraph(
            **values,
            canvas_query=None,
            canvas_preparation=object(),
        )
