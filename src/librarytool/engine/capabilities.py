"""Framework-neutral module and workbench capability discovery.

The registry describes what the engine can do; it does not import providers,
workbenches, Flask, or any other UI/runtime framework.  A composition root can
therefore build these manifests before it constructs optional implementations,
and every client receives the same deterministic discovery document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ManifestValidationError(ValueError):
    """A module/workbench manifest is malformed or internally ambiguous."""


class DuplicateManifestError(ManifestValidationError):
    """Two registered manifests claim the same stable identifier."""


class SealedRegistryError(ManifestValidationError):
    """A resolved capability registry cannot be changed."""


def _validate_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ManifestValidationError(
            f"{field_name} must be a lowercase portable identifier"
        )


def _validate_semver(value: str, field_name: str = "version") -> None:
    if not isinstance(value, str) or not _SEMVER_RE.fullmatch(value):
        raise ManifestValidationError(
            f"{field_name} must be a semantic version such as '1.2.0'"
        )


@dataclass(frozen=True, slots=True, order=True)
class CapabilityRef:
    """One major version of an engine capability contract.

    Capability versions are deliberately exact major contract versions.  A
    provider that supports two majors advertises both refs, rather than making
    the resolver guess whether a newer contract is backwards compatible.
    """

    id: str
    version: int = 1

    def __post_init__(self) -> None:
        _validate_id(self.id, "capability id")
        if (not isinstance(self.version, int) or isinstance(self.version, bool)
                or self.version < 1):
            raise ManifestValidationError(
                "capability version must be a positive integer"
            )

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "version": self.version}


def _capability_tuple(
    values: Iterable[CapabilityRef], field_name: str
) -> tuple[CapabilityRef, ...]:
    if isinstance(values, (str, bytes)):
        raise ManifestValidationError(
            f"{field_name} must contain CapabilityRef values"
        )
    try:
        result = tuple(values)
    except TypeError as exc:
        raise ManifestValidationError(
            f"{field_name} must be an iterable of CapabilityRef values"
        ) from exc
    if any(not isinstance(value, CapabilityRef) for value in result):
        raise ManifestValidationError(
            f"{field_name} must contain only CapabilityRef values"
        )
    if len(set(result)) != len(result):
        raise ManifestValidationError(
            f"{field_name} contains a duplicate capability"
        )
    return tuple(sorted(result))


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    """Immutable declaration of one engine module or provider.

    ``requires`` gates the module. ``enhances`` never gates its core behavior;
    absent enhancements make the module degraded while its provided
    capabilities remain usable.
    """

    id: str
    version: str
    provides: tuple[CapabilityRef, ...] = field(default_factory=tuple)
    requires: tuple[CapabilityRef, ...] = field(default_factory=tuple)
    enhances: tuple[CapabilityRef, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_id(self.id, "module id")
        _validate_semver(self.version)
        provides = _capability_tuple(self.provides, "provides")
        requires = _capability_tuple(self.requires, "requires")
        enhances = _capability_tuple(self.enhances, "enhances")
        overlap = set(requires) & set(enhances)
        if overlap:
            names = ", ".join(_capability_token(value) for value in sorted(overlap))
            raise ManifestValidationError(
                f"capabilities cannot be both required and optional: {names}"
            )
        object.__setattr__(self, "provides", provides)
        object.__setattr__(self, "requires", requires)
        object.__setattr__(self, "enhances", enhances)


@dataclass(frozen=True, slots=True)
class WorkbenchManifest:
    """Immutable engine-facing contract for one focused client workbench."""

    id: str
    version: str
    requires: tuple[CapabilityRef, ...] = field(default_factory=tuple)
    enhances: tuple[CapabilityRef, ...] = field(default_factory=tuple)
    owner_module: str = ""

    def __post_init__(self) -> None:
        _validate_id(self.id, "workbench id")
        _validate_semver(self.version)
        requires = _capability_tuple(self.requires, "requires")
        enhances = _capability_tuple(self.enhances, "enhances")
        overlap = set(requires) & set(enhances)
        if overlap:
            names = ", ".join(_capability_token(value) for value in sorted(overlap))
            raise ManifestValidationError(
                f"capabilities cannot be both required and optional: {names}"
            )
        object.__setattr__(self, "requires", requires)
        object.__setattr__(self, "enhances", enhances)
        if self.owner_module:
            _validate_id(self.owner_module, "workbench owner module id")


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """Immutable result of resolving one installed module graph."""

    active_module_ids: tuple[str, ...]
    capabilities: tuple[CapabilityRef, ...]

    def __post_init__(self) -> None:
        try:
            active = tuple(self.active_module_ids)
        except TypeError as exc:
            raise ManifestValidationError(
                "active_module_ids must be an iterable"
            ) from exc
        for module_id in active:
            _validate_id(module_id, "active module id")
        if len(set(active)) != len(active):
            raise ManifestValidationError(
                "active_module_ids contains a duplicate module"
            )
        capabilities = _capability_tuple(
            self.capabilities, "resolved capabilities"
        )
        object.__setattr__(self, "active_module_ids", tuple(sorted(active)))
        object.__setattr__(self, "capabilities", capabilities)

    def is_active(self, module_id: str) -> bool:
        return module_id in self.active_module_ids

    def provides(self, capability: CapabilityRef) -> bool:
        return capability in self.capabilities


def _capability_token(value: CapabilityRef) -> str:
    return f"{value.id}@{value.version}"


def _refs_as_dicts(values: Iterable[CapabilityRef]) -> list[dict[str, object]]:
    return [value.as_dict() for value in sorted(values)]


class CapabilityRegistry:
    """Resolve installed manifests into deterministic client discovery data."""

    SCHEMA = "librarytool.capabilities/1"

    def __init__(
        self,
        modules: Iterable[ModuleManifest] = (),
        workbenches: Iterable[WorkbenchManifest] = (),
    ) -> None:
        self._modules: dict[str, ModuleManifest] = {}
        self._workbenches: dict[str, WorkbenchManifest] = {}
        self._sealed = False
        for manifest in modules:
            self.register_module(manifest)
        for manifest in workbenches:
            self.register_workbench(manifest)

    def register_module(self, manifest: ModuleManifest) -> None:
        self._require_mutable()
        if not isinstance(manifest, ModuleManifest):
            raise ManifestValidationError("module must be a ModuleManifest")
        if manifest.id in self._modules:
            raise DuplicateManifestError(f"duplicate module id: {manifest.id}")
        self._modules[manifest.id] = manifest

    def register_workbench(self, manifest: WorkbenchManifest) -> None:
        self._require_mutable()
        if not isinstance(manifest, WorkbenchManifest):
            raise ManifestValidationError(
                "workbench must be a WorkbenchManifest"
            )
        if manifest.id in self._workbenches:
            raise DuplicateManifestError(
                f"duplicate workbench id: {manifest.id}"
            )
        self._workbenches[manifest.id] = manifest

    @property
    def sealed(self) -> bool:
        return self._sealed

    def seal(self) -> CapabilityRegistry:
        """Prevent later registrations and return this registry.

        Mutable registration remains available for compatibility while a
        composition root is being assembled. Runtime engines should expose a
        sealed registry so their service graph and discovery document cannot
        drift apart after construction.
        """

        self._sealed = True
        return self

    def sealed_copy(self) -> CapabilityRegistry:
        """Return an independent immutable registry with the same manifests."""

        return CapabilityRegistry(
            modules=self._modules.values(),
            workbenches=self._workbenches.values(),
        ).seal()

    def resolve(self) -> CapabilityResolution:
        """Resolve installed modules into a stable immutable result."""

        active: set[str] = set()
        capabilities: set[CapabilityRef] = set()
        pending = set(self._modules)

        # Resolve in batches. A module becomes active only from capabilities
        # supplied by an earlier complete batch, so registration/dict order can
        # never affect the result or discovery output.
        while pending:
            ready = {
                module_id
                for module_id in pending
                if set(self._modules[module_id].requires) <= capabilities
            }
            if not ready:
                break
            active.update(ready)
            pending.difference_update(ready)
            for module_id in ready:
                capabilities.update(self._modules[module_id].provides)
        return CapabilityResolution(
            active_module_ids=tuple(sorted(active)),
            capabilities=tuple(sorted(capabilities)),
        )

    def _require_mutable(self) -> None:
        if self._sealed:
            raise SealedRegistryError("capability registry is sealed")

    def discovery_document(self) -> dict[str, object]:
        """Return ordinary JSON-shaped data in a stable, canonical order."""
        resolution = self.resolve()
        active = set(resolution.active_module_ids)
        capabilities = set(resolution.capabilities)

        providers: dict[CapabilityRef, list[str]] = {}
        for module_id in sorted(active):
            for capability in self._modules[module_id].provides:
                providers.setdefault(capability, []).append(module_id)

        capability_rows = [
            {
                **capability.as_dict(),
                "providers": sorted(providers[capability]),
            }
            for capability in sorted(providers)
        ]
        module_rows = [
            self._module_row(self._modules[module_id], active, capabilities)
            for module_id in sorted(self._modules)
        ]
        workbench_rows = [
            self._workbench_row(
                self._workbenches[workbench_id], capabilities, active
            )
            for workbench_id in sorted(self._workbenches)
        ]
        return {
            "schema": self.SCHEMA,
            "capabilities": capability_rows,
            "modules": module_rows,
            "workbenches": workbench_rows,
        }

    def _resolve_modules(self) -> tuple[set[str], set[CapabilityRef]]:
        """Compatibility wrapper for callers of the former private resolver."""

        resolution = self.resolve()
        return set(resolution.active_module_ids), set(resolution.capabilities)

    @staticmethod
    def _module_row(
        manifest: ModuleManifest,
        active: set[str],
        capabilities: set[CapabilityRef],
    ) -> dict[str, object]:
        missing_required = tuple(
            requirement
            for requirement in manifest.requires
            if requirement not in capabilities
        )
        missing_optional = tuple(
            enhancement
            for enhancement in manifest.enhances
            if enhancement not in capabilities
        )
        if manifest.id not in active:
            status = "blocked"
        elif missing_optional:
            status = "degraded"
        else:
            status = "available"
        return {
            "id": manifest.id,
            "version": manifest.version,
            "status": status,
            "available": manifest.id in active,
            "provides": _refs_as_dicts(manifest.provides),
            "requires": _refs_as_dicts(manifest.requires),
            "enhances": _refs_as_dicts(manifest.enhances),
            "missing_required": _refs_as_dicts(missing_required),
            "missing_optional": _refs_as_dicts(missing_optional),
        }

    @staticmethod
    def _workbench_row(
        manifest: WorkbenchManifest,
        capabilities: set[CapabilityRef],
        active_modules: set[str],
    ) -> dict[str, object]:
        missing_required = tuple(
            requirement
            for requirement in manifest.requires
            if requirement not in capabilities
        )
        missing_optional = tuple(
            enhancement
            for enhancement in manifest.enhances
            if enhancement not in capabilities
        )
        owner_available = (
            not manifest.owner_module
            or manifest.owner_module in active_modules
        )
        if not owner_available or missing_required:
            status = "blocked"
        elif missing_optional:
            status = "degraded"
        else:
            status = "available"
        return {
            "id": manifest.id,
            "version": manifest.version,
            "status": status,
            "visible": owner_available and not missing_required,
            "owner_module": manifest.owner_module,
            "owner_available": owner_available,
            "requires": _refs_as_dicts(manifest.requires),
            "enhances": _refs_as_dicts(manifest.enhances),
            "missing_required": _refs_as_dicts(missing_required),
            "missing_optional": _refs_as_dicts(missing_optional),
        }


__all__ = [
    "CapabilityRef",
    "CapabilityResolution",
    "CapabilityRegistry",
    "DuplicateManifestError",
    "ManifestValidationError",
    "ModuleManifest",
    "SealedRegistryError",
    "WorkbenchManifest",
]
