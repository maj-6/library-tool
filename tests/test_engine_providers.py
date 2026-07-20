"""Immutable provider discovery, status, and selection contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from librarytool.engine.capabilities import CapabilityRef
from librarytool.engine.providers import (
    DuplicateProviderError,
    MappingSecretStatusProbe,
    ProviderDescriptor,
    ProviderDiscoveryService,
    ProviderHealthSnapshot,
    ProviderHealthState,
    ProviderLimits,
    ProviderRegistry,
    ProviderSelection,
    ProviderSelectionPolicy,
    ProviderStatusReason,
    ProviderTraits,
    ProviderValidationError,
    StaticProviderHealthProbe,
)


LAYOUT = CapabilityRef("replica.layout.generate")
OCR = CapabilityRef("ocr.text.generate")
TRANSLATE = CapabilityRef("translation.layer.generate")
IMAGE = CapabilityRef("image.generate")
EMBED = CapabilityRef("embedding.generate")
ANSWER = CapabilityRef("answer.generate")
SECRET_ID = "provider:mistral:api-key"
PRIVATE = r"C:\Users\private\credentials.json sk-private-value"


def _traits(*, remote: bool = False) -> ProviderTraits:
    return ProviderTraits(
        execution="remote" if remote else "local",
        network="required" if remote else "offline",
        modes=("streaming", "batch"),
        input_media=("text", "document"),
        output_media=("text", "layout"),
        input_languages=("zh-hant", "en"),
        output_languages=("*",),
        limits=ProviderLimits(
            max_input_bytes=8_000_000,
            max_batch_items=64,
            max_context_tokens=128_000,
            max_output_tokens=8_192,
        ),
    )


def _provider(
    provider_id: str,
    *,
    capabilities: tuple[CapabilityRef, ...] = (LAYOUT,),
    remote: bool = False,
    secrets: tuple[str, ...] = (),
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id,
        "2.1.0",
        capabilities=capabilities,
        traits=_traits(remote=remote),
        required_secret_status_ids=secrets,
    )


def _health(
    state: ProviderHealthState = ProviderHealthState.HEALTHY,
    *,
    configured: bool = True,
    reason: str | None = None,
) -> StaticProviderHealthProbe:
    return StaticProviderHealthProbe(ProviderHealthSnapshot(
        configured=configured,
        state=state,
        reason=None if reason is None else ProviderStatusReason(reason),
    ))


def test_descriptor_models_generation_contracts_without_provider_imports():
    provider = _provider(
        "provider.portable",
        capabilities=(LAYOUT, OCR, TRANSLATE, IMAGE, EMBED, ANSWER),
        remote=True,
        secrets=(SECRET_ID,),
    )

    assert provider.capabilities == tuple(sorted(
        (LAYOUT, OCR, TRANSLATE, IMAGE, EMBED, ANSWER)
    ))
    assert provider.traits.modes == ("batch", "streaming")
    assert provider.traits.input_media == ("document", "text")
    assert provider.traits.input_languages == ("en", "zh-hant")
    document = provider.as_dict()
    assert document["required_secret_status_ids"] == [SECRET_ID]
    assert document["traits"]["execution"] == "remote"
    assert document["traits"]["network"] == "required"
    assert document["traits"]["limits"] == {
        "max_input_bytes": 8_000_000,
        "max_output_bytes": None,
        "max_batch_items": 64,
        "max_context_tokens": 128_000,
        "max_output_tokens": 8_192,
    }
    assert "secret_value" not in repr(document).casefold()
    assert "api_key" not in repr(document).casefold()
    assert PRIVATE not in repr(document)

    with pytest.raises(FrozenInstanceError):
        provider.version = "9.0.0"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("factory", "message"),
    (
        (lambda: _provider("Bad Provider"), "provider id"),
        (lambda: ProviderDescriptor(
            "provider.bad-version", "latest", (LAYOUT,), _traits()
        ), "semantic"),
        (lambda: ProviderDescriptor(
            "provider.empty", "1.0.0", (), _traits()
        ), "one or more"),
        (lambda: ProviderTraits(
            execution="remote", network="offline", modes=("batch",),
            input_media=("text",), output_media=("text",),
        ), "remote provider"),
        (lambda: ProviderTraits(
            execution="local", network="offline", modes=("interactive",),
            input_media=("text",), output_media=("text",),
        ), "modes may contain"),
        (lambda: ProviderTraits(
            execution="local", network="offline", modes=("batch",),
            input_media=("text",), output_media=("text",),
            input_languages=("EN_us",),
        ), "BCP-47"),
        (lambda: ProviderTraits(
            execution="local", network="offline", modes=("batch",),
            input_media=("text",), output_media=("text",),
            input_languages=("*", "en"),
        ), "cannot combine"),
        (lambda: ProviderLimits(max_context_tokens=0), "positive integer"),
        (lambda: ProviderLimits(
            max_context_tokens=9_007_199_254_740_992
        ), "JSON-safe"),
        (lambda: _provider(
            "provider.unsafe-version",
            capabilities=(CapabilityRef(
                "replica.layout.generate", 9_007_199_254_740_992
            ),),
        ), "JSON-safe"),
        (lambda: _provider(
            "provider.bad-secret", secrets=("provider.not-namespaced",)
        ), "namespaced"),
        (lambda: ProviderStatusReason("raw-exception"), "not public"),
    ),
)
def test_invalid_public_provider_contracts_are_rejected(factory, message):
    with pytest.raises(ProviderValidationError, match=message):
        factory()


def test_registry_and_selection_policy_are_immutable_and_deterministic():
    zulu = _provider("provider.zulu")
    alpha = _provider("provider.alpha")
    registry = ProviderRegistry((zulu, alpha))
    policy = ProviderSelectionPolicy((
        ProviderSelection(TRANSLATE, default_provider_id="provider.zulu"),
        ProviderSelection(LAYOUT, user_provider_id="provider.alpha"),
    ))

    assert [value.id for value in registry.providers] == [
        "provider.alpha", "provider.zulu",
    ]
    assert [value.capability for value in policy.selections] == [
        LAYOUT, TRANSLATE,
    ]
    assert registry.get("provider.alpha") is alpha
    with pytest.raises(AttributeError, match="immutable"):
        registry._providers = ()  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="immutable"):
        policy._values = ()  # type: ignore[attr-defined]
    with pytest.raises(DuplicateProviderError, match="provider.alpha"):
        ProviderRegistry((alpha, _provider("provider.alpha", remote=True)))
    with pytest.raises(ProviderValidationError, match="duplicate capability"):
        ProviderSelectionPolicy((
            ProviderSelection(LAYOUT, default_provider_id="provider.alpha"),
            ProviderSelection(LAYOUT, user_provider_id="provider.zulu"),
        ))


def test_user_selection_never_silently_falls_back_to_default():
    preferred = _provider("provider.preferred")
    fallback = _provider("provider.default")
    registry = ProviderRegistry((fallback, preferred))
    service = ProviderDiscoveryService(
        registry,
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT,
            user_provider_id=preferred.id,
            default_provider_id=fallback.id,
        ),)),
        health_probes={
            preferred.id: _health(
                ProviderHealthState.UNAVAILABLE,
                reason="runtime-unavailable",
            ),
            fallback.id: _health(),
        },
    )

    document = service.discovery_document()
    selection = document["selections"][0]
    assert selection == {
        "capability": LAYOUT.as_dict(),
        "user_provider_id": preferred.id,
        "default_provider_id": fallback.id,
        "selected_provider_id": preferred.id,
        "source": "user",
        "command_available": False,
        "reason": ProviderStatusReason("runtime-unavailable").as_dict(),
    }
    assert document["available_commands"] == []

    default_service = ProviderDiscoveryService(
        registry,
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT,
            default_provider_id=fallback.id,
        ),)),
        health_probes={preferred.id: _health(), fallback.id: _health()},
    )
    chosen = default_service.discovery_document()["selections"][0]
    assert chosen["source"] == "default"
    assert chosen["selected_provider_id"] == fallback.id
    assert chosen["command_available"] is True
    assert default_service.discovery_document()["available_commands"] == [
        LAYOUT.as_dict()
    ]


def test_no_selection_and_missing_provider_fail_closed():
    installed = _provider("provider.installed")
    no_choice = ProviderDiscoveryService(
        ProviderRegistry((installed,)),
        ProviderSelectionPolicy(),
        health_probes={installed.id: _health()},
    ).discovery_document()
    assert no_choice["selections"][0]["command_available"] is False
    assert no_choice["selections"][0]["reason"]["code"] == "no-selection"

    missing = ProviderDiscoveryService(
        ProviderRegistry((installed,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, user_provider_id="provider.not-installed"
        ),)),
        health_probes={installed.id: _health()},
    ).discovery_document()
    assert missing["selections"][0]["reason"]["code"] == (
        "provider-not-installed"
    )
    assert missing["available_commands"] == []


@pytest.mark.parametrize(
    ("secret_status", "reason"),
    ((False, "secret-unavailable"), (None, "secret-status-unknown")),
)
def test_required_secret_presence_is_status_only_and_fails_closed(
    secret_status,
    reason,
):
    provider = _provider(
        "provider.remote",
        remote=True,
        secrets=(SECRET_ID,),
    )
    service = ProviderDiscoveryService(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, default_provider_id=provider.id
        ),)),
        health_probes={provider.id: _health()},
        secret_status_probe=MappingSecretStatusProbe({
            SECRET_ID: secret_status,
        }),
    )

    document = service.discovery_document()
    row = document["providers"][0]
    assert row["required_secret_status_ids"] == [SECRET_ID]
    assert row["secret_statuses"] == [{
        "id": SECRET_ID,
        "configured": secret_status,
    }]
    assert row["configured"] is False
    assert row["available"] is False
    assert row["health"]["reason"]["code"] == reason
    assert document["available_commands"] == []
    assert PRIVATE not in repr(document)


def test_degraded_provider_remains_explicitly_usable():
    provider = _provider("provider.degraded")
    service = ProviderDiscoveryService(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, default_provider_id=provider.id
        ),)),
        health_probes={provider.id: _health(
            ProviderHealthState.DEGRADED,
            reason="provider-degraded",
        )},
    )
    document = service.discovery_document()
    assert document["providers"][0]["health"] == {
        "state": "degraded",
        "reason": ProviderStatusReason("provider-degraded").as_dict(),
    }
    assert document["providers"][0]["available"] is True
    assert document["selections"][0]["command_available"] is True


class _ExplodingProbe:
    def snapshot(self):
        raise OSError(PRIVATE)


class _MalformedProbe:
    def snapshot(self):
        return {"configured": True, "state": "healthy", "path": PRIVATE}


class _ExplodingSecretProbe:
    def configured(self, _secret_status_id):
        raise RuntimeError(PRIVATE)


@pytest.mark.parametrize("probe", (_ExplodingProbe(), _MalformedProbe()))
def test_probe_failures_are_sanitized_and_never_open_commands(probe):
    provider = _provider("provider.failure")
    service = ProviderDiscoveryService(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, default_provider_id=provider.id
        ),)),
        health_probes={provider.id: probe},
    )
    document = service.discovery_document()
    assert document["providers"][0]["health"]["reason"]["code"] == (
        "probe-failed"
    )
    assert document["available_commands"] == []
    assert PRIVATE not in repr(document)


def test_secret_probe_failures_are_sanitized():
    provider = _provider("provider.secret-failure", secrets=(SECRET_ID,))
    service = ProviderDiscoveryService(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy((ProviderSelection(
            LAYOUT, default_provider_id=provider.id
        ),)),
        health_probes={provider.id: _health()},
        secret_status_probe=_ExplodingSecretProbe(),
    )
    document = service.discovery_document()
    assert document["providers"][0]["secret_statuses"] == [{
        "id": SECRET_ID, "configured": None,
    }]
    assert document["providers"][0]["health"]["reason"]["code"] == (
        "secret-status-unknown"
    )
    assert PRIVATE not in repr(document)


def test_shared_secret_status_is_read_once_per_discovery_snapshot():
    calls = []

    class SecretProbe:
        def configured(self, secret_id):
            calls.append(secret_id)
            return True

    providers = (
        _provider("provider.alpha", secrets=(SECRET_ID,)),
        _provider("provider.beta", secrets=(SECRET_ID,)),
    )
    service = ProviderDiscoveryService(
        ProviderRegistry(providers),
        ProviderSelectionPolicy(),
        health_probes={provider.id: _health() for provider in providers},
        secret_status_probe=SecretProbe(),
    )

    service.discovery_document()

    assert calls == [SECRET_ID]


def test_health_snapshot_contract_rejects_ambiguous_states():
    with pytest.raises(ProviderValidationError, match="must be unavailable"):
        ProviderHealthSnapshot(False, ProviderHealthState.HEALTHY)
    with pytest.raises(ProviderValidationError, match="needs a public reason"):
        ProviderHealthSnapshot(True, ProviderHealthState.DEGRADED)
    with pytest.raises(ProviderValidationError, match="cannot carry"):
        ProviderHealthSnapshot(
            True,
            ProviderHealthState.HEALTHY,
            ProviderStatusReason("provider-degraded"),
        )


def test_constructor_validates_probe_structure_without_invoking_it():
    calls = []

    class Probe:
        def snapshot(self):
            calls.append("health")
            return ProviderHealthSnapshot(True, ProviderHealthState.HEALTHY)

    provider = _provider("provider.lazy")
    service = ProviderDiscoveryService(
        ProviderRegistry((provider,)),
        ProviderSelectionPolicy(),
        health_probes={provider.id: Probe()},
    )
    assert calls == []
    service.discovery_document()
    assert calls == ["health"]

    with pytest.raises(ProviderValidationError, match="callable snapshot"):
        ProviderDiscoveryService(
            ProviderRegistry((provider,)),
            ProviderSelectionPolicy(),
            health_probes={provider.id: object()},
        )
    with pytest.raises(ProviderValidationError, match="unknown provider"):
        ProviderDiscoveryService(
            ProviderRegistry((provider,)),
            ProviderSelectionPolicy(),
            health_probes={"provider.unknown": Probe()},
        )
