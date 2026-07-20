"""Versioned HTTP resource for optional provider discovery."""

from __future__ import annotations

from flask import Flask

from librarytool.engine.capabilities import (
    CapabilityRef,
    CapabilityRegistry,
    ModuleManifest,
)
from librarytool.engine.providers import (
    ProviderDescriptor,
    ProviderDiscoveryService,
    ProviderHealthSnapshot,
    ProviderHealthState,
    ProviderRegistry,
    ProviderSelection,
    ProviderSelectionPolicy,
    ProviderTraits,
    StaticProviderHealthProbe,
)
from librarytool.engine.runtime import (
    PROVIDER_DISCOVERY_SERVICE,
    LibraryEngine,
    LibraryEngineBuilder,
    ModuleContribution,
    ServiceBinding,
)
from librarytool_http import create_provider_discovery_blueprint


LAYOUT = CapabilityRef("replica.layout.generate")


def _service() -> ProviderDiscoveryService:
    provider = ProviderDescriptor(
        "provider.local",
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
    return ProviderDiscoveryService(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, default_provider_id=provider.id
        ),)),
        health_probes={
            provider.id: StaticProviderHealthProbe(
                ProviderHealthSnapshot(True, ProviderHealthState.HEALTHY)
            )
        },
    )


def _engine(service: ProviderDiscoveryService | None) -> LibraryEngine:
    if service is None:
        return LibraryEngine(CapabilityRegistry().seal())
    capability = CapabilityRef("library.providers.discover")
    return LibraryEngineBuilder((ModuleContribution(
        ModuleManifest("test.providers", "1.0.0", provides=(capability,)),
        bindings=(ServiceBinding(
            PROVIDER_DISCOVERY_SERVICE,
            service,
            (capability,),
        ),),
    ),)).build()


def _app(engine: LibraryEngine) -> Flask:
    app = Flask(__name__)
    app.register_blueprint(create_provider_discovery_blueprint(lambda: engine))
    return app


def test_provider_discovery_is_versioned_read_only_and_conditional():
    client = _app(_engine(_service())).test_client()

    response = client.get("/api/v1/providers")
    assert response.status_code == 200
    assert response.cache_control.no_cache is True
    assert response.headers["ETag"].startswith('"providers-')
    assert response.get_json() == {
        "ok": True,
        "schema": "librarytool.providers/1",
        "providers": [{
            "id": "provider.local",
            "version": "1.0.0",
            "capabilities": [LAYOUT.as_dict()],
            "traits": {
                "execution": "local",
                "network": "offline",
                "modes": ["batch"],
                "input_media": ["document"],
                "output_media": ["layout"],
                "input_languages": [],
                "output_languages": [],
                "limits": {
                    "max_input_bytes": None,
                    "max_output_bytes": None,
                    "max_batch_items": None,
                    "max_context_tokens": None,
                    "max_output_tokens": None,
                },
            },
            "required_secret_status_ids": [],
            "secret_statuses": [],
            "configured": True,
            "health": {"state": "healthy", "reason": None},
            "available": True,
        }],
        "selections": [{
            "capability": LAYOUT.as_dict(),
            "user_provider_id": "",
            "default_provider_id": "provider.local",
            "selected_provider_id": "provider.local",
            "source": "default",
            "command_available": True,
            "reason": None,
        }],
        "available_commands": [LAYOUT.as_dict()],
    }

    cached = client.get(
        "/api/v1/providers",
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert cached.status_code == 304
    assert client.post("/api/v1/providers").status_code == 405


def test_unbound_provider_module_fails_closed_without_advertising_commands():
    engine = _engine(None)
    assert engine.get_service(PROVIDER_DISCOVERY_SERVICE) is None
    assert "library.providers.discover" not in {
        row["id"]
        for row in engine.discovery_document()["capabilities"]
    }

    response = _app(engine).test_client().get("/api/v1/providers")
    assert response.status_code == 503
    assert response.cache_control.no_store is True
    assert response.headers["Pragma"] == "no-cache"
    assert response.get_json() == {
        "ok": False,
        "error": "provider discovery is unavailable",
        "code": "provider_discovery_unavailable",
        "retryable": False,
    }


def test_blueprint_requires_an_injected_engine_accessor():
    try:
        create_provider_discovery_blueprint(None)  # type: ignore[arg-type]
    except TypeError as error:
        assert str(error) == "engine_for_request must be callable"
    else:
        raise AssertionError("a missing engine accessor must fail")
