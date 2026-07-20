"""Framework-neutral provider discovery and selection contracts.

Provider implementations, SDKs, credentials, and UI concepts do not belong in
this module.  A host contributes immutable public descriptors plus cached,
side-effect-free status probes.  Clients can then discover which generation
contracts are installed and whether a command is safe to offer without ever
receiving credential material, filesystem paths, or raw provider failures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import getattr_static
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol

from .capabilities import CapabilityRef


_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SECRET_NAMESPACE_SEGMENT_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,62}$"
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_LANGUAGE_RE = re.compile(r"^(?:\*|[a-z]{2,8}(?:-[a-z0-9]{1,8})*)$")
_MODES = frozenset({"batch", "streaming"})
_EXECUTION_LOCATIONS = frozenset({"local", "remote"})
_NETWORK_ACCESS = frozenset({"offline", "required"})
_MAX_JSON_SAFE_INTEGER = 9_007_199_254_740_991


class ProviderValidationError(ValueError):
    """A public provider declaration or selection policy is invalid."""


class DuplicateProviderError(ProviderValidationError):
    """Two installed descriptors claim the same stable provider identifier."""


def _validate_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ProviderValidationError(
            f"{field_name} must be a lowercase portable identifier"
        )


def _validate_semver(value: str) -> None:
    if not isinstance(value, str) or not _SEMVER_RE.fullmatch(value):
        raise ProviderValidationError(
            "provider version must be semantic, such as '1.2.0'"
        )


def _validate_secret_status_id(value: str) -> None:
    if not isinstance(value, str) or len(value) > 255:
        raise ProviderValidationError(
            "secret status id must be a portable namespaced identifier"
        )
    segments = value.split(":")
    if len(segments) < 2 or any(
        not _SECRET_NAMESPACE_SEGMENT_RE.fullmatch(segment)
        for segment in segments
    ):
        raise ProviderValidationError(
            "secret status id must be a portable namespaced identifier"
        )


def _secret_status_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProviderValidationError(
            "required_secret_status_ids must be an iterable"
        )
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ProviderValidationError(
            "required_secret_status_ids must be an iterable"
        ) from exc
    for value in result:
        _validate_secret_status_id(value)
    if len(set(result)) != len(result):
        raise ProviderValidationError(
            "required_secret_status_ids contains a duplicate"
        )
    return tuple(sorted(result))


def _portable_values(
    values: Iterable[str],
    *,
    field_name: str,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProviderValidationError(f"{field_name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ProviderValidationError(
            f"{field_name} must be an iterable"
        ) from exc
    if not allow_empty and not result:
        raise ProviderValidationError(f"{field_name} must not be empty")
    for value in result:
        _validate_id(value, field_name)
    if len(set(result)) != len(result):
        raise ProviderValidationError(f"{field_name} contains a duplicate")
    return tuple(sorted(result))


def _languages(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProviderValidationError(f"{field_name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ProviderValidationError(
            f"{field_name} must be an iterable"
        ) from exc
    if any(
        not isinstance(value, str) or not _LANGUAGE_RE.fullmatch(value)
        for value in result
    ):
        raise ProviderValidationError(
            f"{field_name} must contain normalized BCP-47 ranges or '*'"
        )
    if len(set(result)) != len(result):
        raise ProviderValidationError(f"{field_name} contains a duplicate")
    if "*" in result and len(result) != 1:
        raise ProviderValidationError(
            f"{field_name} cannot combine '*' with narrower ranges"
        )
    return tuple(sorted(result))


def _capabilities(values: Iterable[CapabilityRef]) -> tuple[CapabilityRef, ...]:
    if isinstance(values, (str, bytes)):
        raise ProviderValidationError(
            "capabilities must contain CapabilityRef values"
        )
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ProviderValidationError(
            "capabilities must contain CapabilityRef values"
        ) from exc
    if not result or any(not isinstance(value, CapabilityRef) for value in result):
        raise ProviderValidationError(
            "capabilities must contain one or more CapabilityRef values"
        )
    if any(value.version > _MAX_JSON_SAFE_INTEGER for value in result):
        raise ProviderValidationError(
            "capability versions must be JSON-safe positive integers"
        )
    if len(set(result)) != len(result):
        raise ProviderValidationError("capabilities contains a duplicate")
    return tuple(sorted(result))


def _positive_limit(value: int | None, field_name: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
        or value > _MAX_JSON_SAFE_INTEGER
    ):
        raise ProviderValidationError(
            f"{field_name} must be a JSON-safe positive integer"
        )


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    """Portable limits that materially affect command planning.

    Null means the integration has not declared a limit.  Provider-specific
    pricing, paths, account data, and opaque SDK configuration stay private.
    """

    max_input_bytes: int | None = None
    max_output_bytes: int | None = None
    max_batch_items: int | None = None
    max_context_tokens: int | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "max_input_bytes",
            "max_output_bytes",
            "max_batch_items",
            "max_context_tokens",
            "max_output_tokens",
        ):
            _positive_limit(getattr(self, field_name), field_name)

    def as_dict(self) -> dict[str, int | None]:
        return {
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_batch_items": self.max_batch_items,
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True, slots=True)
class ProviderTraits:
    """Provider properties a host may safely expose to every client."""

    execution: str
    network: str
    modes: tuple[str, ...]
    input_media: tuple[str, ...]
    output_media: tuple[str, ...]
    input_languages: tuple[str, ...] = field(default_factory=tuple)
    output_languages: tuple[str, ...] = field(default_factory=tuple)
    limits: ProviderLimits = field(default_factory=ProviderLimits)

    def __post_init__(self) -> None:
        if self.execution not in _EXECUTION_LOCATIONS:
            raise ProviderValidationError("execution must be 'local' or 'remote'")
        if self.network not in _NETWORK_ACCESS:
            raise ProviderValidationError("network must be 'offline' or 'required'")
        if self.execution == "remote" and self.network != "required":
            raise ProviderValidationError(
                "a remote provider must declare required network access"
            )
        modes = _portable_values(
            self.modes,
            field_name="modes",
            allow_empty=False,
        )
        if not set(modes) <= _MODES:
            raise ProviderValidationError(
                "modes may contain only 'batch' and 'streaming'"
            )
        object.__setattr__(self, "modes", modes)
        object.__setattr__(
            self,
            "input_media",
            _portable_values(
                self.input_media,
                field_name="input_media",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "output_media",
            _portable_values(
                self.output_media,
                field_name="output_media",
                allow_empty=False,
            ),
        )
        object.__setattr__(
            self,
            "input_languages",
            _languages(self.input_languages, "input_languages"),
        )
        object.__setattr__(
            self,
            "output_languages",
            _languages(self.output_languages, "output_languages"),
        )
        if not isinstance(self.limits, ProviderLimits):
            raise ProviderValidationError("limits must be ProviderLimits")

    def as_dict(self) -> dict[str, object]:
        return {
            "execution": self.execution,
            "network": self.network,
            "modes": list(self.modes),
            "input_media": list(self.input_media),
            "output_media": list(self.output_media),
            "input_languages": list(self.input_languages),
            "output_languages": list(self.output_languages),
            "limits": self.limits.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """One installed provider integration, independent of its SDK/runtime."""

    id: str
    version: str
    capabilities: tuple[CapabilityRef, ...]
    traits: ProviderTraits
    required_secret_status_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_id(self.id, "provider id")
        _validate_semver(self.version)
        object.__setattr__(self, "capabilities", _capabilities(self.capabilities))
        if not isinstance(self.traits, ProviderTraits):
            raise ProviderValidationError("traits must be ProviderTraits")
        object.__setattr__(
            self,
            "required_secret_status_ids",
            _secret_status_ids(self.required_secret_status_ids),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "version": self.version,
            "capabilities": [value.as_dict() for value in self.capabilities],
            "traits": self.traits.as_dict(),
            "required_secret_status_ids": list(self.required_secret_status_ids),
        }


class ProviderHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


_PUBLIC_REASON_MESSAGES = MappingProxyType({
    "disabled": "The provider is disabled.",
    "health-unknown": "Provider health could not be determined.",
    "network-unavailable": "Required network access is unavailable.",
    "no-selection": "No provider has been selected.",
    "not-configured": "Required provider configuration is missing.",
    "probe-failed": "Provider health could not be determined.",
    "provider-degraded": "The provider reports degraded service.",
    "provider-incompatible": "The selected provider is incompatible.",
    "provider-not-installed": "The selected provider is not installed.",
    "provider-unavailable": "The selected provider is unavailable.",
    "remote-unreachable": "The remote provider could not be reached.",
    "runtime-unavailable": "The provider runtime is unavailable.",
    "secret-status-unknown": "Required credential status is unavailable.",
    "secret-unavailable": "A required credential is not configured.",
})


@dataclass(frozen=True, slots=True)
class ProviderStatusReason:
    """A closed, sanitized reason safe for unauthenticated client display."""

    code: str

    def __post_init__(self) -> None:
        if self.code not in _PUBLIC_REASON_MESSAGES:
            raise ProviderValidationError("provider status reason is not public")

    @property
    def message(self) -> str:
        return _PUBLIC_REASON_MESSAGES[self.code]

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ProviderHealthSnapshot:
    """Cached provider health returned by an injected side-effect-free probe."""

    configured: bool
    state: ProviderHealthState
    reason: ProviderStatusReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.configured, bool):
            raise ProviderValidationError("configured must be a boolean")
        if not isinstance(self.state, ProviderHealthState):
            raise ProviderValidationError("state must be ProviderHealthState")
        if self.reason is not None and not isinstance(
            self.reason, ProviderStatusReason
        ):
            raise ProviderValidationError("reason must be ProviderStatusReason")
        if not self.configured and self.state != ProviderHealthState.UNAVAILABLE:
            raise ProviderValidationError(
                "an unconfigured provider must be unavailable"
            )
        if self.state == ProviderHealthState.HEALTHY and self.reason is not None:
            raise ProviderValidationError(
                "a healthy provider cannot carry a failure reason"
            )
        if self.state != ProviderHealthState.HEALTHY and self.reason is None:
            raise ProviderValidationError(
                "a degraded or unavailable provider needs a public reason"
            )


class ProviderHealthProbe(Protocol):
    """Read an already-cached health snapshot without I/O or side effects."""

    def snapshot(self) -> ProviderHealthSnapshot:
        """Return current public health without contacting the provider."""


class SecretStatusProbe(Protocol):
    """Read configuration presence only; never return credential material."""

    def configured(self, secret_status_id: str) -> bool | None:
        """Return true/false, or none when status cannot be determined."""


@dataclass(frozen=True, slots=True)
class StaticProviderHealthProbe:
    """Convenience probe over a precomputed immutable snapshot."""

    value: ProviderHealthSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.value, ProviderHealthSnapshot):
            raise ProviderValidationError("value must be ProviderHealthSnapshot")

    def snapshot(self) -> ProviderHealthSnapshot:
        return self.value


class MappingSecretStatusProbe:
    """Immutable secret-presence snapshot; it intentionally stores no values."""

    __slots__ = ("_statuses",)

    def __init__(
        self,
        statuses: Mapping[str, bool | None] | None = None,
    ) -> None:
        statuses = {} if statuses is None else statuses
        if not isinstance(statuses, Mapping):
            raise ProviderValidationError("secret statuses must be a mapping")
        copied: dict[str, bool | None] = {}
        for secret_id, configured in statuses.items():
            _validate_secret_status_id(secret_id)
            if configured is not None and not isinstance(configured, bool):
                raise ProviderValidationError(
                    "secret status values must be booleans or None"
                )
            copied[secret_id] = configured
        object.__setattr__(self, "_statuses", MappingProxyType(copied))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("MappingSecretStatusProbe is immutable")

    def configured(self, secret_status_id: str) -> bool | None:
        return self._statuses.get(secret_status_id)


@dataclass(frozen=True, slots=True, order=True)
class ProviderSelection:
    """Explicit user/default choice for one exact capability contract."""

    capability: CapabilityRef
    user_provider_id: str = ""
    default_provider_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.capability, CapabilityRef):
            raise ProviderValidationError("selection capability is invalid")
        for value, field_name in (
            (self.user_provider_id, "user provider id"),
            (self.default_provider_id, "default provider id"),
        ):
            if value:
                _validate_id(value, field_name)


class ProviderSelectionPolicy:
    """Immutable, explicit choices; absence never means pick the first provider."""

    __slots__ = ("_selections", "_values")

    def __init__(self, selections: Iterable[ProviderSelection] = ()) -> None:
        if isinstance(selections, (str, bytes)):
            raise ProviderValidationError(
                "selections must contain ProviderSelection values"
            )
        try:
            values = tuple(selections)
        except TypeError as exc:
            raise ProviderValidationError(
                "selections must contain ProviderSelection values"
            ) from exc
        if any(not isinstance(value, ProviderSelection) for value in values):
            raise ProviderValidationError(
                "selections must contain ProviderSelection values"
            )
        ordered = tuple(sorted(values, key=lambda value: value.capability))
        by_capability: dict[CapabilityRef, ProviderSelection] = {}
        for value in ordered:
            if value.capability in by_capability:
                raise ProviderValidationError(
                    "selection policy contains a duplicate capability"
                )
            by_capability[value.capability] = value
        object.__setattr__(self, "_values", ordered)
        object.__setattr__(self, "_selections", MappingProxyType(by_capability))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ProviderSelectionPolicy is immutable")

    @property
    def selections(self) -> tuple[ProviderSelection, ...]:
        return self._values

    def get(self, capability: CapabilityRef) -> ProviderSelection | None:
        return self._selections.get(capability)


class ProviderRegistry:
    """Immutable deterministic registry of installed public descriptors."""

    __slots__ = ("_by_id", "_providers")

    def __init__(self, providers: Iterable[ProviderDescriptor] = ()) -> None:
        if isinstance(providers, (str, bytes)):
            raise ProviderValidationError(
                "providers must contain ProviderDescriptor values"
            )
        try:
            values = tuple(providers)
        except TypeError as exc:
            raise ProviderValidationError(
                "providers must contain ProviderDescriptor values"
            ) from exc
        if any(not isinstance(value, ProviderDescriptor) for value in values):
            raise ProviderValidationError(
                "providers must contain ProviderDescriptor values"
            )
        ordered = tuple(sorted(values, key=lambda value: (value.id, value.version)))
        by_id: dict[str, ProviderDescriptor] = {}
        for provider in ordered:
            if provider.id in by_id:
                raise DuplicateProviderError(
                    f"duplicate provider id: {provider.id}"
                )
            by_id[provider.id] = provider
        object.__setattr__(self, "_providers", ordered)
        object.__setattr__(self, "_by_id", MappingProxyType(by_id))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ProviderRegistry is immutable")

    @property
    def providers(self) -> tuple[ProviderDescriptor, ...]:
        return self._providers

    def get(self, provider_id: str) -> ProviderDescriptor | None:
        return self._by_id.get(provider_id)


def _require_probe_method(probe: object, method_name: str, field_name: str) -> None:
    try:
        member = getattr_static(probe, method_name)
    except AttributeError as exc:
        raise ProviderValidationError(
            f"{field_name} must expose callable {method_name}()"
        ) from exc
    if isinstance(member, (classmethod, staticmethod)):
        member = member.__func__
    if not callable(member):
        raise ProviderValidationError(
            f"{field_name} must expose callable {method_name}()"
        )


class ProviderDiscoveryService:
    """Read-only provider/status/selection discovery.

    Construction validates structure without invoking a probe.  Querying calls
    only the injected cached snapshot ports and converts every exception or
    malformed result to a closed public reason.  It never calls a provider SDK.
    """

    SCHEMA = "librarytool.providers/1"
    __slots__ = ("_health_probes", "_policy", "_registry", "_secret_probe")

    def __init__(
        self,
        registry: ProviderRegistry,
        policy: ProviderSelectionPolicy,
        *,
        health_probes: Mapping[str, ProviderHealthProbe] | None = None,
        secret_status_probe: SecretStatusProbe | None = None,
    ) -> None:
        if not isinstance(registry, ProviderRegistry):
            raise ProviderValidationError("registry must be ProviderRegistry")
        if not isinstance(policy, ProviderSelectionPolicy):
            raise ProviderValidationError(
                "policy must be ProviderSelectionPolicy"
            )
        health_probes = {} if health_probes is None else health_probes
        if not isinstance(health_probes, Mapping):
            raise ProviderValidationError("health_probes must be a mapping")
        copied: dict[str, ProviderHealthProbe] = {}
        for provider_id, probe in health_probes.items():
            _validate_id(provider_id, "health probe provider id")
            if registry.get(provider_id) is None:
                raise ProviderValidationError(
                    f"health probe names an unknown provider: {provider_id}"
                )
            _require_probe_method(probe, "snapshot", "health probe")
            copied[provider_id] = probe
        if secret_status_probe is not None:
            _require_probe_method(
                secret_status_probe,
                "configured",
                "secret status probe",
            )
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_health_probes", MappingProxyType(copied))
        object.__setattr__(self, "_secret_probe", secret_status_probe)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ProviderDiscoveryService is immutable")

    def _health_for(self, provider_id: str) -> ProviderHealthSnapshot:
        probe = self._health_probes.get(provider_id)
        if probe is None:
            return ProviderHealthSnapshot(
                configured=False,
                state=ProviderHealthState.UNAVAILABLE,
                reason=ProviderStatusReason("health-unknown"),
            )
        try:
            value = probe.snapshot()
        except Exception:
            return ProviderHealthSnapshot(
                configured=False,
                state=ProviderHealthState.UNAVAILABLE,
                reason=ProviderStatusReason("probe-failed"),
            )
        if not isinstance(value, ProviderHealthSnapshot):
            return ProviderHealthSnapshot(
                configured=False,
                state=ProviderHealthState.UNAVAILABLE,
                reason=ProviderStatusReason("probe-failed"),
            )
        return value

    def _secret_status(self, secret_id: str) -> bool | None:
        if self._secret_probe is None:
            return None
        try:
            value = self._secret_probe.configured(secret_id)
        except Exception:
            return None
        return value if isinstance(value, bool) or value is None else None

    def _provider_row(
        self,
        provider: ProviderDescriptor,
        secret_status_by_id: Mapping[str, bool | None],
    ) -> dict[str, object]:
        health = self._health_for(provider.id)
        secret_statuses = tuple(
            (secret_id, secret_status_by_id[secret_id])
            for secret_id in provider.required_secret_status_ids
        )
        unknown_secret = any(value is None for _, value in secret_statuses)
        missing_secret = any(value is False for _, value in secret_statuses)

        if unknown_secret:
            configured = False
            state = ProviderHealthState.UNAVAILABLE
            reason = ProviderStatusReason("secret-status-unknown")
        elif missing_secret:
            configured = False
            state = ProviderHealthState.UNAVAILABLE
            reason = ProviderStatusReason("secret-unavailable")
        elif not health.configured:
            configured = False
            state = ProviderHealthState.UNAVAILABLE
            reason = health.reason or ProviderStatusReason("not-configured")
        else:
            configured = True
            state = health.state
            reason = health.reason
        available = configured and state in {
            ProviderHealthState.HEALTHY,
            ProviderHealthState.DEGRADED,
        }
        return {
            **provider.as_dict(),
            "secret_statuses": [
                {"id": secret_id, "configured": value}
                for secret_id, value in secret_statuses
            ],
            "configured": configured,
            "health": {
                "state": state.value,
                "reason": None if reason is None else reason.as_dict(),
            },
            "available": available,
        }

    @staticmethod
    def _selection_reason(
        provider_reason: object,
    ) -> ProviderStatusReason:
        if isinstance(provider_reason, Mapping):
            code = provider_reason.get("code")
            if isinstance(code, str) and code in _PUBLIC_REASON_MESSAGES:
                return ProviderStatusReason(code)
        return ProviderStatusReason("provider-unavailable")

    def discovery_document(self) -> dict[str, object]:
        secret_status_by_id = {
            secret_id: self._secret_status(secret_id)
            for secret_id in sorted({
                secret_id
                for provider in self._registry.providers
                for secret_id in provider.required_secret_status_ids
            })
        }
        provider_rows = [
            self._provider_row(provider, secret_status_by_id)
            for provider in self._registry.providers
        ]
        rows_by_id = {str(row["id"]): row for row in provider_rows}
        capabilities = {
            capability
            for provider in self._registry.providers
            for capability in provider.capabilities
        }
        capabilities.update(
            selection.capability for selection in self._policy.selections
        )
        selection_rows: list[dict[str, object]] = []
        available_commands: list[dict[str, object]] = []
        for capability in sorted(capabilities):
            selection = self._policy.get(capability)
            user_id = "" if selection is None else selection.user_provider_id
            default_id = (
                "" if selection is None else selection.default_provider_id
            )
            selected_id = user_id or default_id
            source = "user" if user_id else "default" if default_id else "none"
            reason: ProviderStatusReason | None = None
            command_available = False
            if not selected_id:
                reason = ProviderStatusReason("no-selection")
            else:
                descriptor = self._registry.get(selected_id)
                provider_row = rows_by_id.get(selected_id)
                if descriptor is None or provider_row is None:
                    reason = ProviderStatusReason("provider-not-installed")
                elif capability not in descriptor.capabilities:
                    reason = ProviderStatusReason("provider-incompatible")
                elif provider_row["available"] is not True:
                    health = provider_row["health"]
                    assert isinstance(health, Mapping)
                    reason = self._selection_reason(health.get("reason"))
                else:
                    command_available = True
                    available_commands.append(capability.as_dict())
            selection_rows.append({
                "capability": capability.as_dict(),
                "user_provider_id": user_id,
                "default_provider_id": default_id,
                "selected_provider_id": selected_id,
                "source": source,
                "command_available": command_available,
                "reason": None if reason is None else reason.as_dict(),
            })
        return {
            "schema": self.SCHEMA,
            "providers": provider_rows,
            "selections": selection_rows,
            "available_commands": available_commands,
        }


__all__ = [
    "DuplicateProviderError",
    "MappingSecretStatusProbe",
    "ProviderDescriptor",
    "ProviderDiscoveryService",
    "ProviderHealthProbe",
    "ProviderHealthSnapshot",
    "ProviderHealthState",
    "ProviderLimits",
    "ProviderRegistry",
    "ProviderSelection",
    "ProviderSelectionPolicy",
    "ProviderStatusReason",
    "ProviderTraits",
    "ProviderValidationError",
    "SecretStatusProbe",
    "StaticProviderHealthProbe",
]
