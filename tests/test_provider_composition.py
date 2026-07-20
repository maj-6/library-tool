"""Optional first-party composition for provider discovery."""

from __future__ import annotations

import pytest

from librarytool.composition.filesystem import (
    FilesystemServiceGraph,
    ProviderDiscoveryBindings,
)
from librarytool.composition.first_party import (
    FIRST_PARTY_MODULE_MANIFESTS,
    first_party_module_contributions,
)
from librarytool.engine.capabilities import CapabilityRef
from librarytool.engine.providers import (
    ProviderDescriptor,
    ProviderHealthSnapshot,
    ProviderHealthState,
    ProviderRegistry,
    ProviderSelection,
    ProviderSelectionPolicy,
    ProviderTraits,
    ProviderValidationError,
)
from librarytool.engine.runtime import (
    PROVIDER_DISCOVERY_SERVICE,
    LibraryEngineBuilder,
)


LAYOUT = CapabilityRef("replica.layout.generate")


def _provider() -> ProviderDescriptor:
    return ProviderDescriptor(
        "provider.test",
        "1.0.0",
        capabilities=(LAYOUT,),
        traits=ProviderTraits(
            execution="local",
            network="offline",
            modes=("batch",),
            input_media=("document",),
            output_media=("layout",),
        ),
    )


def _graph(provider_discovery=None) -> FilesystemServiceGraph:
    return FilesystemServiceGraph(
        items=object(),
        item_commands=object(),
        item_lifecycle=None,
        representation_commands=None,
        interchange=object(),
        lib_open=None,
        jobs=object(),
        replica=object(),
        text_layers=object(),
        translations=object(),
        translation_provenance=object(),
        provider_discovery=provider_discovery,
    )


def test_provider_module_is_withheld_until_service_is_explicitly_injected():
    without = first_party_module_contributions(_graph())
    assert "library.providers" not in {
        value.manifest.id for value in without
    }
    assert all(
        binding.key != PROVIDER_DISCOVERY_SERVICE
        for contribution in without
        for binding in contribution.bindings
    )

    bindings = ProviderDiscoveryBindings(
        ProviderRegistry(),
        ProviderSelectionPolicy(),
    )
    with_provider = first_party_module_contributions(_graph(bindings.service))
    contribution = next(
        value
        for value in with_provider
        if value.manifest.id == "library.providers"
    )
    assert contribution.manifest.provides == (
        CapabilityRef("library.providers.discover"),
    )
    assert contribution.bindings[0].key == PROVIDER_DISCOVERY_SERVICE
    assert contribution.bindings[0].service is bindings.service

    engine = LibraryEngineBuilder((contribution,)).build()
    assert engine.get_service(PROVIDER_DISCOVERY_SERVICE) is bindings.service
    module = engine.discovery_document()["modules"][0]
    assert module["id"] == "library.providers"
    assert module["available"] is True


def test_provider_binding_and_engine_composition_do_not_run_health_probes():
    calls = []

    class CachedProbe:
        def snapshot(self):
            calls.append("snapshot")
            return ProviderHealthSnapshot(True, ProviderHealthState.HEALTHY)

    provider = _provider()
    bindings = ProviderDiscoveryBindings(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, default_provider_id=provider.id
        ),)),
        health_probes={provider.id: CachedProbe()},
    )
    assert calls == []

    contribution = next(
        value
        for value in first_party_module_contributions(_graph(bindings.service))
        if value.manifest.id == "library.providers"
    )
    engine = LibraryEngineBuilder((contribution,)).build()
    assert calls == []
    discovery = engine.require_service(
        PROVIDER_DISCOVERY_SERVICE
    ).discovery_document()
    assert calls == ["snapshot"]
    assert discovery["available_commands"] == [LAYOUT.as_dict()]


def test_first_party_production_manifests_advertise_no_generation_provider():
    generation_capabilities = {
        "replica.layout.generate",
        "ocr.text.generate",
        "translation.layer.generate",
        "image.generate",
        "embedding.generate",
        "answer.generate",
    }
    advertised = {
        capability.id
        for manifest in FIRST_PARTY_MODULE_MANIFESTS
        for capability in manifest.provides
    }
    assert advertised.isdisjoint(generation_capabilities)
    assert "library.providers.discover" in advertised


def test_provider_bindings_reject_invalid_contracts_without_probe_access():
    with pytest.raises(
        ProviderValidationError,
        match="registry must be ProviderRegistry",
    ):
        ProviderDiscoveryBindings(  # type: ignore[arg-type]
            object(),
            ProviderSelectionPolicy(),
        )
