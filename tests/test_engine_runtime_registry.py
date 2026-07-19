"""Validated, framework-neutral runtime module/service composition."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from librarytool.engine import (
    ITEM_QUERY_SERVICE,
    CapabilityRef,
    CapabilityRegistry,
    DuplicateServiceError,
    LibraryEngine,
    LibraryEngineBuilder,
    ModuleContribution,
    ModuleManifest,
    SealedRegistryError,
    ServiceBinding,
    ServiceKey,
    ServiceNotFoundError,
    ServiceRegistry,
    ServiceRegistryError,
    WorkbenchManifest,
)


ITEMS = CapabilityRef("library.items.read")
TRANSLATIONS = CapabilityRef("translation.layers.read")
GENERATION = CapabilityRef("translation.layers.generate")


def _contribution(
    module_id: str,
    *,
    provides: tuple[CapabilityRef, ...],
    bindings: tuple[ServiceBinding[object], ...],
    requires: tuple[CapabilityRef, ...] = (),
    enhances: tuple[CapabilityRef, ...] = (),
    workbenches: tuple[WorkbenchManifest, ...] = (),
) -> ModuleContribution:
    return ModuleContribution(
        ModuleManifest(
            module_id,
            "1.0.0",
            provides=provides,
            requires=requires,
            enhances=enhances,
        ),
        bindings=bindings,
        workbenches=workbenches,
    )


def _binding(
    key: ServiceKey[object], service: object, *capabilities: CapabilityRef
) -> ServiceBinding[object]:
    return ServiceBinding(key, service, capabilities)


@pytest.mark.parametrize(
    ("service_id", "version"),
    (("", 1), ("Upper.Case", 1), ("bad space", 1), ("valid.id", 0),
     ("valid.id", True)),
)
def test_service_keys_have_portable_versioned_identity(service_id, version):
    with pytest.raises(ServiceRegistryError):
        ServiceKey(service_id, version)


def test_service_registry_is_immutable_and_has_typed_lookup_semantics():
    key = ServiceKey[object]("example.reader", 2)
    service = object()
    registry = ServiceRegistry((_binding(key, service, ITEMS),))

    assert registry.get(key) is service
    assert registry.require(key) is service
    assert registry.keys == (key,)
    assert len(registry) == 1
    with pytest.raises(ServiceNotFoundError, match="example.missing@1"):
        registry.require(ServiceKey("example.missing"))
    with pytest.raises(AttributeError):
        registry.extra = service


def test_bindings_require_declared_exact_capability_coverage():
    key = ServiceKey[object]("example.reader")
    with pytest.raises(ServiceRegistryError, match="one or more"):
        ServiceBinding(key, object(), ())
    with pytest.raises(ServiceRegistryError, match="no service binding"):
        _contribution(
            "example.missing",
            provides=(ITEMS, TRANSLATIONS),
            bindings=(_binding(key, object(), ITEMS),),
        )
    with pytest.raises(ServiceRegistryError, match="undeclared"):
        _contribution(
            "example.undeclared",
            provides=(ITEMS,),
            bindings=(_binding(key, object(), ITEMS, TRANSLATIONS),),
        )


def test_builder_populates_legacy_fields_from_well_known_keys():
    item_queries = object()
    contribution = _contribution(
        "library.core",
        provides=(ITEMS,),
        bindings=(_binding(ITEM_QUERY_SERVICE, item_queries, ITEMS),),
    )

    engine = LibraryEngineBuilder((contribution,)).build()

    assert engine.items is item_queries
    assert engine.get_service(ITEM_QUERY_SERVICE) is item_queries
    assert engine.require_service(ITEM_QUERY_SERVICE) is item_queries
    assert engine.services.require(ITEM_QUERY_SERVICE) is engine.items
    assert engine.capabilities.sealed is True
    with pytest.raises(SealedRegistryError, match="sealed"):
        engine.capabilities.register_module(
            ModuleManifest("late.module", "1.0.0")
        )


def test_direct_library_engine_construction_remains_compatible():
    item_queries = object()
    capabilities = CapabilityRegistry()
    engine = LibraryEngine(capabilities=capabilities, items=item_queries)

    assert engine.items is item_queries
    assert len(engine.services) == 0
    assert capabilities.sealed is False
    assert LibraryEngine(capabilities=capabilities) == LibraryEngine(
        capabilities=capabilities
    )


def test_inactive_modules_do_not_publish_their_services():
    replica_capability = CapabilityRef("replica.regions")
    replica_key = ServiceKey[object]("replica.regions")
    contribution = _contribution(
        "replica.core",
        provides=(replica_capability,),
        requires=(ITEMS,),
        bindings=(_binding(replica_key, object(), replica_capability),),
        workbenches=(
            WorkbenchManifest(
                "replica", "1.0.0", requires=(replica_capability,)
            ),
        ),
    )

    engine = LibraryEngineBuilder((contribution,)).build()
    document = engine.discovery_document()

    assert engine.services.get(replica_key) is None
    assert document["capabilities"] == []
    assert document["modules"][0]["status"] == "blocked"
    assert document["workbenches"][0]["visible"] is False
    assert document["workbenches"][0]["owner_available"] is False


def test_blocked_module_cannot_expose_a_workbench_via_another_provider():
    base = CapabilityRef("base.read")
    missing = CapabilityRef("missing.dependency")
    base_module = _contribution(
        "base.core",
        provides=(base,),
        bindings=(
            _binding(ServiceKey("base.reader"), object(), base),
        ),
    )
    blocked_module = _contribution(
        "blocked.module",
        provides=(TRANSLATIONS,),
        requires=(missing,),
        bindings=(
            _binding(
                ServiceKey("blocked.translation"),
                object(),
                TRANSLATIONS,
            ),
        ),
        workbenches=(
            WorkbenchManifest(
                "blocked.editor", "1.0.0", requires=(base,)
            ),
        ),
    )

    document = LibraryEngineBuilder(
        (blocked_module, base_module)
    ).build().discovery_document()
    workbench = document["workbenches"][0]

    assert workbench["status"] == "blocked"
    assert workbench["visible"] is False
    assert workbench["owner_module"] == "blocked.module"
    assert workbench["owner_available"] is False


def test_missing_enhancement_degrades_without_withholding_service():
    translation_key = ServiceKey[object]("translation.reader")
    service = object()
    contribution = _contribution(
        "translation.core",
        provides=(TRANSLATIONS,),
        enhances=(GENERATION,),
        bindings=(_binding(translation_key, service, TRANSLATIONS),),
    )

    engine = LibraryEngineBuilder((contribution,)).build()
    row = engine.discovery_document()["modules"][0]

    assert engine.services.require(translation_key) is service
    assert row["status"] == "degraded"
    assert row["missing_optional"] == [GENERATION.as_dict()]


def test_duplicate_keys_fail_but_alternative_capability_providers_coexist():
    local_key = ServiceKey[object]("translation.provider.local")
    remote_key = ServiceKey[object]("translation.provider.remote")
    local = _contribution(
        "provider.local",
        provides=(GENERATION,),
        bindings=(_binding(local_key, object(), GENERATION),),
    )
    duplicate = _contribution(
        "provider.duplicate",
        provides=(GENERATION,),
        bindings=(_binding(local_key, object(), GENERATION),),
    )
    with pytest.raises(DuplicateServiceError, match="provider.local"):
        LibraryEngineBuilder((local, duplicate))

    remote = _contribution(
        "provider.remote",
        provides=(GENERATION,),
        bindings=(_binding(remote_key, object(), GENERATION),),
    )
    engine = LibraryEngineBuilder((remote, local)).build()
    capability = engine.discovery_document()["capabilities"][0]
    assert capability["providers"] == ["provider.local", "provider.remote"]
    assert engine.services.keys == (local_key, remote_key)


def test_resolution_and_build_results_are_immutable_and_order_independent():
    item_service = object()
    translation_service = object()
    item_module = _contribution(
        "library.core",
        provides=(ITEMS,),
        bindings=(_binding(ITEM_QUERY_SERVICE, item_service, ITEMS),),
    )
    translation_key = ServiceKey[object]("translation.reader")
    translation_module = _contribution(
        "translation.core",
        provides=(TRANSLATIONS,),
        requires=(ITEMS,),
        bindings=(
            _binding(translation_key, translation_service, TRANSLATIONS),
        ),
    )

    forward = LibraryEngineBuilder((item_module, translation_module)).build()
    reverse = LibraryEngineBuilder((translation_module, item_module)).build()

    assert forward.discovery_document() == reverse.discovery_document()
    assert forward.services.keys == reverse.services.keys
    resolution = forward.capabilities.resolve()
    assert resolution.active_module_ids == ("library.core", "translation.core")
    assert resolution.capabilities == (ITEMS, TRANSLATIONS)
    with pytest.raises(FrozenInstanceError):
        resolution.active_module_ids = ()


def test_legacy_field_cannot_disagree_with_generic_registry():
    registry_item = object()
    direct_item = object()
    registry = ServiceRegistry(
        (_binding(ITEM_QUERY_SERVICE, registry_item, ITEMS),)
    )

    with pytest.raises(ServiceRegistryError, match="legacy field items"):
        LibraryEngine(
            capabilities=CapabilityRegistry(),
            items=direct_item,
            services=registry,
        )
