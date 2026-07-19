"""Composition object shared by transports and headless clients."""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import CapabilityRegistry
from .interchange import LibInterchangeService
from .item_commands import ItemCommandService
from .items import ItemQueryService
from .jobs import JobManager
from .replica import ReplicaApplicationService
from .text_layers import TextLayerService
from .translations import TranslationProvenanceService, TranslationService


@dataclass(frozen=True, slots=True)
class LibraryEngine:
    """One local engine assembled from installed application services.

    The object is intentionally small: construction and adapter selection stay
    in the composition root, while Flask, CLI, Qt, Godot, and test transports
    can all receive the same service graph. Optional modules remain ``None``;
    clients discover whether to expose their workbench through ``capabilities``.
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

    def discovery_document(self) -> dict[str, object]:
        return self.capabilities.discovery_document()


__all__ = ["LibraryEngine"]
