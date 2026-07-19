"""Framework-neutral engine composition and service discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from functools import total_ordering
from types import MappingProxyType
from typing import Generic, Iterable, Mapping, TypeVar, cast

from .capabilities import (
    CapabilityRef,
    CapabilityRegistry,
    DuplicateManifestError,
    ModuleManifest,
    WorkbenchManifest,
)
from .interchange import LibInterchangeService, OpenLibService
from .item_commands import ItemCommandService
from .representation_commands import RepresentationCommandService
from .items import ItemQueryService, WorkbenchPolicyPort
from .jobs import JobManager
from .replica import ReplicaApplicationService
from .text_layers import TextLayerService
from .translations import TranslationProvenanceService, TranslationService


_SERVICE_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
S = TypeVar("S")


class ServiceRegistryError(ValueError):
    """A service declaration or registry is invalid."""


class DuplicateServiceError(ServiceRegistryError):
    """Two modules claim the same concrete service contract."""


class ServiceNotFoundError(LookupError):
    """A requested service contract is not installed and active."""


@total_ordering
class ServiceKey(Generic[S]):
    """Stable major version of one concrete application-service contract."""

    __slots__ = ("_id", "_version")

    def __init__(self, service_id: str, version: int = 1) -> None:
        if (
            not isinstance(service_id, str)
            or not _SERVICE_ID_RE.fullmatch(service_id)
        ):
            raise ServiceRegistryError(
                "service id must be a lowercase portable identifier"
            )
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise ServiceRegistryError(
                "service version must be a positive integer"
            )
        object.__setattr__(self, "_id", service_id)
        object.__setattr__(self, "_version", version)

    @property
    def id(self) -> str:
        return self._id

    @property
    def version(self) -> int:
        return self._version

    @property
    def token(self) -> str:
        return f"{self.id}@{self.version}"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ServiceKey is immutable")

    def __hash__(self) -> int:
        return hash((self.id, self.version))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ServiceKey):
            return NotImplemented
        return (self.id, self.version) == (other.id, other.version)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ServiceKey):
            return NotImplemented
        return (self.id, self.version) < (other.id, other.version)

    def __repr__(self) -> str:
        return f"ServiceKey(id={self.id!r}, version={self.version!r})"


def _capability_tuple(
    values: Iterable[CapabilityRef], *, field_name: str
) -> tuple[CapabilityRef, ...]:
    if isinstance(values, (str, bytes)):
        raise ServiceRegistryError(
            f"{field_name} must contain CapabilityRef values"
        )
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ServiceRegistryError(
            f"{field_name} must be an iterable of CapabilityRef values"
        ) from exc
    if not result or any(not isinstance(value, CapabilityRef) for value in result):
        raise ServiceRegistryError(
            f"{field_name} must contain one or more CapabilityRef values"
        )
    if len(set(result)) != len(result):
        raise ServiceRegistryError(f"{field_name} contains a duplicate capability")
    return tuple(sorted(result))


class ServiceBinding(Generic[S]):
    """Bind one implementation to its service key and advertised features."""

    __slots__ = ("_capabilities", "_key", "_service")

    def __init__(
        self,
        key: ServiceKey[S],
        service: S,
        capabilities: Iterable[CapabilityRef],
    ) -> None:
        if not isinstance(key, ServiceKey):
            raise ServiceRegistryError("binding key must be a ServiceKey")
        if service is None:
            raise ServiceRegistryError("a service binding cannot bind None")
        declared = _capability_tuple(
            capabilities,
            field_name=f"capabilities for {key.token}",
        )
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_capabilities", declared)

    @property
    def key(self) -> ServiceKey[S]:
        return self._key

    @property
    def service(self) -> S:
        return self._service

    @property
    def capabilities(self) -> tuple[CapabilityRef, ...]:
        return self._capabilities

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ServiceBinding is immutable")

    def __repr__(self) -> str:
        return (
            f"ServiceBinding(key={self.key!r}, service={self.service!r}, "
            f"capabilities={self.capabilities!r})"
        )


class ServiceRegistry:
    """Immutable lookup table for services in one resolved engine graph."""

    __slots__ = ("_bindings", "_services")

    _bindings: tuple[ServiceBinding[object], ...]
    _services: Mapping[ServiceKey[object], object]

    def __init__(self, bindings: Iterable[ServiceBinding[object]] = ()) -> None:
        if isinstance(bindings, (str, bytes)):
            raise ServiceRegistryError("bindings must contain ServiceBinding values")
        try:
            values = tuple(bindings)
        except TypeError as exc:
            raise ServiceRegistryError(
                "bindings must be an iterable of ServiceBinding values"
            ) from exc
        if any(not isinstance(value, ServiceBinding) for value in values):
            raise ServiceRegistryError(
                "bindings must contain only ServiceBinding values"
            )
        ordered = tuple(sorted(values, key=lambda value: value.key))
        services: dict[ServiceKey[object], object] = {}
        for binding in ordered:
            if binding.key in services:
                raise DuplicateServiceError(
                    f"duplicate service key: {binding.key.token}"
                )
            services[binding.key] = binding.service
        object.__setattr__(self, "_bindings", ordered)
        object.__setattr__(self, "_services", MappingProxyType(services))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("ServiceRegistry is immutable")

    @property
    def bindings(self) -> tuple[ServiceBinding[object], ...]:
        return self._bindings

    @property
    def keys(self) -> tuple[ServiceKey[object], ...]:
        return tuple(binding.key for binding in self._bindings)

    def get(self, key: ServiceKey[S]) -> S | None:
        if not isinstance(key, ServiceKey):
            raise TypeError("service key must be a ServiceKey")
        return cast(S | None, self._services.get(key))

    def require(self, key: ServiceKey[S]) -> S:
        service = self.get(key)
        if service is None:
            raise ServiceNotFoundError(
                f"service is not installed and active: {key.token}"
            )
        return service

    def __contains__(self, key: object) -> bool:
        return key in self._services

    def __len__(self) -> int:
        return len(self._services)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ServiceRegistry):
            return NotImplemented
        return tuple(
            (binding.key, binding.service, binding.capabilities)
            for binding in self.bindings
        ) == tuple(
            (binding.key, binding.service, binding.capabilities)
            for binding in other.bindings
        )


def _materialize_bindings(
    values: Iterable[ServiceBinding[object]],
) -> tuple[ServiceBinding[object], ...]:
    if isinstance(values, (str, bytes)):
        raise ServiceRegistryError("bindings must contain ServiceBinding values")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ServiceRegistryError(
            "bindings must be an iterable of ServiceBinding values"
        ) from exc
    if any(not isinstance(value, ServiceBinding) for value in result):
        raise ServiceRegistryError(
            "bindings must contain only ServiceBinding values"
        )
    return tuple(sorted(result, key=lambda value: value.key))


def _materialize_workbenches(
    values: Iterable[WorkbenchManifest],
) -> tuple[WorkbenchManifest, ...]:
    if isinstance(values, (str, bytes)):
        raise ServiceRegistryError(
            "workbenches must contain WorkbenchManifest values"
        )
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ServiceRegistryError(
            "workbenches must be an iterable of WorkbenchManifest values"
        ) from exc
    if any(not isinstance(value, WorkbenchManifest) for value in result):
        raise ServiceRegistryError(
            "workbenches must contain only WorkbenchManifest values"
        )
    return tuple(sorted(result, key=lambda value: value.id))


class WorkbenchPolicyBinding:
    """Gate one item workbench policy on declared module capabilities."""

    __slots__ = ("_policy", "_requires")

    def __init__(
        self,
        policy: WorkbenchPolicyPort,
        requires: Iterable[CapabilityRef],
    ) -> None:
        policy_id = str(getattr(policy, "policy_id", "") or "").strip()
        if (
            not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", policy_id)
            or not callable(getattr(policy, "contribute", None))
        ):
            raise ServiceRegistryError(
                "a workbench policy binding needs a valid policy"
            )
        requirements = _capability_tuple(
            requires,
            field_name=f"requirements for workbench policy {policy_id}",
        )
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_requires", requirements)

    @property
    def policy(self) -> WorkbenchPolicyPort:
        return self._policy

    @property
    def requires(self) -> tuple[CapabilityRef, ...]:
        return self._requires

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("WorkbenchPolicyBinding is immutable")


def _materialize_policy_bindings(
    values: Iterable[WorkbenchPolicyBinding],
) -> tuple[WorkbenchPolicyBinding, ...]:
    if isinstance(values, (str, bytes)):
        raise ServiceRegistryError(
            "item policies must contain WorkbenchPolicyBinding values"
        )
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ServiceRegistryError(
            "item policies must be an iterable of WorkbenchPolicyBinding values"
        ) from exc
    if any(not isinstance(value, WorkbenchPolicyBinding) for value in result):
        raise ServiceRegistryError(
            "item policies must contain only WorkbenchPolicyBinding values"
        )
    return tuple(
        sorted(result, key=lambda value: str(value.policy.policy_id))
    )


def _capability_tokens(values: Iterable[CapabilityRef]) -> str:
    return ", ".join(f"{value.id}@{value.version}" for value in sorted(values))


@dataclass(frozen=True, slots=True)
class ModuleContribution:
    """One installed module's manifest, services, and client workbenches.

    Every provided capability must be linked to at least one concrete binding.
    Marker-like capabilities link to the service that answers their discovery
    or status query. This exact coverage prevents a manifest from advertising
    a feature for which the assembled engine has no implementation.
    """

    manifest: ModuleManifest
    bindings: tuple[ServiceBinding[object], ...] = field(default_factory=tuple)
    workbenches: tuple[WorkbenchManifest, ...] = field(default_factory=tuple)
    item_policies: tuple[WorkbenchPolicyBinding, ...] = field(
        default_factory=tuple
    )

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ModuleManifest):
            raise ServiceRegistryError("manifest must be a ModuleManifest")
        bindings = _materialize_bindings(self.bindings)
        workbenches = _materialize_workbenches(self.workbenches)
        item_policies = _materialize_policy_bindings(self.item_policies)
        keys = [binding.key for binding in bindings]
        if len(set(keys)) != len(keys):
            raise DuplicateServiceError(
                f"module {self.manifest.id} contains a duplicate service key"
            )
        workbench_ids = [workbench.id for workbench in workbenches]
        if len(set(workbench_ids)) != len(workbench_ids):
            raise DuplicateManifestError(
                f"module {self.manifest.id} contains a duplicate workbench id"
            )
        policy_ids = [str(value.policy.policy_id) for value in item_policies]
        if len(set(policy_ids)) != len(policy_ids):
            raise ServiceRegistryError(
                f"module {self.manifest.id} contains a duplicate item policy"
            )

        declared_dependencies = set(self.manifest.provides)
        declared_dependencies.update(self.manifest.requires)
        declared_dependencies.update(self.manifest.enhances)
        for policy_binding in item_policies:
            undeclared_requirements = (
                set(policy_binding.requires) - declared_dependencies
            )
            if undeclared_requirements:
                raise ServiceRegistryError(
                    f"module {self.manifest.id} has an item policy with "
                    "undeclared capability requirements: "
                    f"{_capability_tokens(undeclared_requirements)}"
                )

        declared = set(self.manifest.provides)
        implemented = {
            capability
            for binding in bindings
            for capability in binding.capabilities
        }
        undeclared = implemented - declared
        missing = declared - implemented
        if undeclared:
            raise ServiceRegistryError(
                f"module {self.manifest.id} binds undeclared capabilities: "
                f"{_capability_tokens(undeclared)}"
            )
        if missing:
            raise ServiceRegistryError(
                f"module {self.manifest.id} has no service binding for: "
                f"{_capability_tokens(missing)}"
            )
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "workbenches", workbenches)
        object.__setattr__(self, "item_policies", item_policies)


ITEM_QUERY_SERVICE: ServiceKey[ItemQueryService] = ServiceKey(
    "library.items.query"
)
ITEM_COMMAND_SERVICE: ServiceKey[ItemCommandService] = ServiceKey(
    "library.items.commands"
)
REPRESENTATION_COMMAND_SERVICE: ServiceKey[RepresentationCommandService] = (
    ServiceKey("library.representations.commands")
)
INTERCHANGE_SERVICE: ServiceKey[LibInterchangeService] = ServiceKey(
    "replica.interchange"
)
LIB_OPEN_SERVICE: ServiceKey[OpenLibService] = ServiceKey(
    "replica.interchange.open"
)
JOB_SERVICE: ServiceKey[JobManager] = ServiceKey("library.jobs")
REPLICA_SERVICE: ServiceKey[ReplicaApplicationService] = ServiceKey(
    "replica.application"
)
TEXT_LAYER_SERVICE: ServiceKey[TextLayerService] = ServiceKey(
    "replica.text-layers"
)
TRANSLATION_SERVICE: ServiceKey[TranslationService] = ServiceKey(
    "translation.application"
)
TRANSLATION_PROVENANCE_SERVICE: ServiceKey[TranslationProvenanceService] = (
    ServiceKey("translation.provenance")
)


_LEGACY_SERVICE_FIELDS = (
    ("items", ITEM_QUERY_SERVICE),
    ("item_commands", ITEM_COMMAND_SERVICE),
    ("interchange", INTERCHANGE_SERVICE),
    ("jobs", JOB_SERVICE),
    ("replica", REPLICA_SERVICE),
    ("text_layers", TEXT_LAYER_SERVICE),
    ("translations", TRANSLATION_SERVICE),
    ("translation_provenance", TRANSLATION_PROVENANCE_SERVICE),
)


@dataclass(frozen=True, slots=True)
class LibraryEngine:
    """One local engine assembled from installed application services.

    Existing typed fields remain a compatibility facade. New independently
    installed modules are accessed through ``services`` without requiring this
    core class to grow another optional field for every module.
    """

    capabilities: CapabilityRegistry
    items: ItemQueryService | None = None
    item_commands: ItemCommandService | None = None
    interchange: LibInterchangeService | None = None
    jobs: JobManager | None = None
    replica: ReplicaApplicationService | None = None
    text_layers: TextLayerService | None = None
    translations: TranslationService | None = None
    translation_provenance: TranslationProvenanceService | None = None
    services: ServiceRegistry = field(default_factory=ServiceRegistry)

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, CapabilityRegistry):
            raise TypeError("capabilities must be a CapabilityRegistry")
        if not isinstance(self.services, ServiceRegistry):
            raise TypeError("services must be a ServiceRegistry")
        for field_name, key in _LEGACY_SERVICE_FIELDS:
            bound = self.services.get(key)
            current = getattr(self, field_name)
            if bound is None:
                continue
            if current is not None and current is not bound:
                raise ServiceRegistryError(
                    f"legacy field {field_name} conflicts with {key.token}"
                )
            if current is None:
                object.__setattr__(self, field_name, bound)

    def get_service(self, key: ServiceKey[S]) -> S | None:
        return self.services.get(key)

    def require_service(self, key: ServiceKey[S]) -> S:
        return self.services.require(key)

    def discovery_document(self) -> dict[str, object]:
        return self.capabilities.discovery_document()


class LibraryEngineBuilder:
    """Validate installed module contributions and build one immutable engine."""

    def __init__(self, contributions: Iterable[ModuleContribution] = ()) -> None:
        self._contributions: dict[str, ModuleContribution] = {}
        self._service_owners: dict[ServiceKey[object], str] = {}
        if isinstance(contributions, (str, bytes)):
            raise ServiceRegistryError(
                "contributions must contain ModuleContribution values"
            )
        try:
            values = tuple(contributions)
        except TypeError as exc:
            raise ServiceRegistryError(
                "contributions must be an iterable of ModuleContribution values"
            ) from exc
        for contribution in values:
            self.install(contribution)

    def install(self, contribution: ModuleContribution) -> LibraryEngineBuilder:
        if not isinstance(contribution, ModuleContribution):
            raise ServiceRegistryError(
                "contribution must be a ModuleContribution"
            )
        module_id = contribution.manifest.id
        if module_id in self._contributions:
            raise DuplicateManifestError(f"duplicate module id: {module_id}")
        for binding in contribution.bindings:
            owner = self._service_owners.get(binding.key)
            if owner is not None:
                raise DuplicateServiceError(
                    f"duplicate service key {binding.key.token}: "
                    f"{owner} and {module_id}"
                )
        self._contributions[module_id] = contribution
        for binding in contribution.bindings:
            self._service_owners[binding.key] = module_id
        return self

    def build(self) -> LibraryEngine:
        contributions = tuple(
            self._contributions[module_id]
            for module_id in sorted(self._contributions)
        )
        capabilities = CapabilityRegistry(
            modules=(value.manifest for value in contributions),
            workbenches=(
                replace(
                    workbench,
                    owner_module=value.manifest.id,
                )
                for value in contributions
                for workbench in value.workbenches
            ),
        ).seal()
        resolution = capabilities.resolve()
        active = set(resolution.active_module_ids)
        active_capabilities = set(resolution.capabilities)
        item_policies = tuple(
            policy_binding.policy
            for contribution in contributions
            if contribution.manifest.id in active
            for policy_binding in contribution.item_policies
            if set(policy_binding.requires) <= active_capabilities
        )
        active_bindings = [
            binding
            for contribution in contributions
            if contribution.manifest.id in active
            for binding in contribution.bindings
        ]
        item_binding = next(
            (
                binding
                for binding in active_bindings
                if binding.key == ITEM_QUERY_SERVICE
            ),
            None,
        )
        if item_policies and item_binding is None:
            raise ServiceRegistryError(
                "active item policies require the item query service"
            )
        if item_binding is not None and item_policies:
            if not isinstance(item_binding.service, ItemQueryService):
                raise ServiceRegistryError(
                    "item policies require an ItemQueryService binding"
                )
            configured_items = item_binding.service.with_policies(item_policies)
            active_bindings = [
                ServiceBinding(
                    binding.key,
                    configured_items,
                    binding.capabilities,
                )
                if binding is item_binding
                else binding
                for binding in active_bindings
            ]
        services = ServiceRegistry(active_bindings)
        return LibraryEngine(capabilities=capabilities, services=services)


__all__ = [
    "DuplicateServiceError",
    "INTERCHANGE_SERVICE",
    "LIB_OPEN_SERVICE",
    "ITEM_COMMAND_SERVICE",
    "ITEM_QUERY_SERVICE",
    "JOB_SERVICE",
    "LibraryEngine",
    "LibraryEngineBuilder",
    "ModuleContribution",
    "REPLICA_SERVICE",
    "REPRESENTATION_COMMAND_SERVICE",
    "ServiceBinding",
    "ServiceKey",
    "ServiceNotFoundError",
    "ServiceRegistry",
    "ServiceRegistryError",
    "TEXT_LAYER_SERVICE",
    "TRANSLATION_PROVENANCE_SERVICE",
    "TRANSLATION_SERVICE",
    "WorkbenchPolicyBinding",
]
