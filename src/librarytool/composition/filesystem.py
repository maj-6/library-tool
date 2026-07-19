"""Assemble the bundled engine from injected filesystem compatibility seams.

This module selects concrete filesystem adapters, but it owns no process
lifecycle.  Importing it performs no I/O, and :func:`compose_filesystem_engine`
does not recover a workspace, create a singleton, or start background work.
The host must settle recovery and construct shared resources before composing
an engine.  That distinction lets Flask, a CLI, or another host expose the
same service graph without introducing a second locking or recovery domain.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ContextManager

from ._filesystem_paths import (
    resolve_workspace_path,
    workspace_paths_overlap,
)

from ..adapters.filesystem import (
    FilesystemInterchangeRepository,
    FilesystemItemCommandRepository,
    FilesystemItemQueryRepository,
    FilesystemReplicaRepository,
    FilesystemTranslationRepository,
    RecoverableWriteSet,
)
from ..engine.errors import RepositoryError
from ..engine.interchange import LibImportPlannerPort, LibInterchangeService
from ..engine.item_commands import (
    ItemCommandService,
    ItemDraft,
    ItemRecordSnapshot,
)
from ..engine.items import ItemQueryService
from ..engine.jobs import JobManager
from ..engine.ports import (
    ItemRepositoryPort,
    ReplicaPolicyPort,
    TextLayerRepositoryPort,
)
from ..engine.replica import ReplicaApplicationService
from ..engine.runtime import (
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_QUERY_SERVICE,
    JOB_SERVICE,
    REPLICA_SERVICE,
    TEXT_LAYER_SERVICE,
    TRANSLATION_PROVENANCE_SERVICE,
    TRANSLATION_SERVICE,
    LibraryEngine,
    LibraryEngineBuilder,
    ModuleContribution,
    ServiceKey,
    ServiceRegistryError,
)
from ..engine.text_layers import TextLayerService
from ..engine.translation_contracts import TranslationSourceSnapshot
from ..engine.translations import (
    TranslationProvenanceService,
    TranslationService,
)


ItemSnapshotLoader = Callable[
    [], Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]
]
ItemRecordDecoder = Callable[
    [str, Mapping[str, Any]], ItemRecordSnapshot
]
ItemRecordEncoder = Callable[
    [str, ItemDraft, Mapping[str, Any] | None], Mapping[str, Any]
]
ItemIdAllocator = Callable[[frozenset[str]], str]
CatalogueLockFactory = Callable[[], ContextManager[Any]]
ItemLockFactory = Callable[[str], ContextManager[Any]]
ReadJson = Callable[[Path], Any]
WriteJson = Callable[[Path, Mapping[str, Any]], None]
SourceIdsLoader = Callable[[str], tuple[str, ...] | None]
TranslationItemExists = Callable[[str], bool]
TranslationSourceLoader = Callable[
    [str, str], TranslationSourceSnapshot | None
]
TranslationSourceReference = Callable[[TranslationSourceSnapshot], str]
_ENTRY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{value}" for value in range(1, 10)}
    | {f"lpt{value}" for value in range(1, 10)}
)


class _EntryDirectoryResolver:
    """Resolve only exact, portable direct children of the entries root."""

    def __init__(self, root: Path, entries: Path) -> None:
        self._root = root
        self._entries = entries

    def __call__(self, item_id: str) -> Path:
        self.validate_item_id(item_id)
        entries = resolve_workspace_path(
            self._root,
            self._entries,
            artifact="entries",
            directory=True,
        )
        candidate = resolve_workspace_path(
            self._root,
            entries / item_id,
            artifact="item_entry",
            directory=True,
        )
        try:
            resolved_entries = entries.resolve(strict=False)
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            raise RepositoryError(
                "the item entry directory cannot be resolved",
                code="unsafe_filesystem_engine_path",
                details={"artifact": "item_entry", "item_id": item_id},
            ) from exc
        if resolved.parent != resolved_entries or resolved.name != item_id:
            raise RepositoryError(
                "the item entry directory is not an exact direct child",
                code="unsafe_filesystem_engine_path",
                details={"artifact": "item_entry", "item_id": item_id},
            )
        return candidate

    @staticmethod
    def validate_item_id(item_id: str) -> None:
        """Enforce the shared portable identity contract for this graph."""

        if (
            not isinstance(item_id, str)
            or not _ENTRY_ID_RE.fullmatch(item_id)
            or item_id.endswith(".")
            or item_id.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
        ):
            raise RepositoryError(
                "the item cannot name an entry directory",
                code="unsafe_filesystem_entry_identity",
                details={"item_id": str(item_id or "")[:128]},
            )

    def layout_path(self, item_id: str) -> Path:
        entry = self(item_id)
        ocr = resolve_workspace_path(
            self._root,
            entry / "ocr",
            artifact="item_ocr",
            directory=True,
        )
        return resolve_workspace_path(
            self._root,
            ocr / "layout.json",
            artifact="replica_layout",
            directory=False,
        )


@dataclass(frozen=True, slots=True)
class FilesystemEnginePaths:
    """Storage locations selected by the host.

    Relative paths are rooted under ``resources.write_set.root``. Composition
    rejects external, reserved, redirecting, or overlapping locations, and one
    shared resolver validates every item entry path at point of use.
    """

    catalogue: Path
    entries: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "catalogue", Path(self.catalogue))
        object.__setattr__(self, "entries", Path(self.entries))


@dataclass(frozen=True, slots=True)
class CatalogueBindings:
    """Legacy catalogue projection, identity, codec, and locking seams."""

    load_snapshot: ItemSnapshotLoader
    descriptors: ItemRepositoryPort
    decode_record: ItemRecordDecoder
    encode_record: ItemRecordEncoder
    allocate_item_id: ItemIdAllocator
    lock_context_for: CatalogueLockFactory


@dataclass(frozen=True, slots=True)
class ReplicaBindings:
    """Replica policies and compatibility persistence callbacks."""

    policies: ReplicaPolicyPort
    text_repository: TextLayerRepositoryPort
    read_json: ReadJson
    write_json: WriteJson
    lock_context_for: ItemLockFactory


@dataclass(frozen=True, slots=True)
class InterchangeBindings:
    """Portable archive planner and transitional format callbacks."""

    planner: LibImportPlannerPort
    source_ids_for: SourceIdsLoader
    clean_region_id: Callable[[Any], str]
    normalize_language: Callable[[str], str]
    sanitize_document_name: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class TranslationBindings:
    """Authoritative item/source lookup callbacks for translation storage."""

    item_exists_for: TranslationItemExists
    source_snapshot_for: TranslationSourceLoader
    source_reference_for: TranslationSourceReference


@dataclass(frozen=True, slots=True)
class FilesystemEngineResources:
    """Shared, already-initialized process resources.

    The host owns recovery; composition verifies that no blocking recovery
    journal remains before exposing services. ``jobs`` and every lock callback
    must be the same objects used by compatibility writers in the host.
    """

    write_set: RecoverableWriteSet
    jobs: JobManager
    provenance: TranslationProvenanceService
    workspace_lock_context_for: ItemLockFactory


@dataclass(frozen=True, slots=True)
class FilesystemServiceGraph:
    """Concrete services awaiting installed-module capability declarations."""

    items: ItemQueryService
    item_commands: ItemCommandService
    interchange: LibInterchangeService
    jobs: JobManager
    replica: ReplicaApplicationService
    text_layers: TextLayerService
    translations: TranslationService
    translation_provenance: TranslationProvenanceService

    def keyed_services(self) -> tuple[tuple[ServiceKey[Any], Any], ...]:
        return (
            (ITEM_QUERY_SERVICE, self.items),
            (ITEM_COMMAND_SERVICE, self.item_commands),
            (INTERCHANGE_SERVICE, self.interchange),
            (JOB_SERVICE, self.jobs),
            (REPLICA_SERVICE, self.replica),
            (TEXT_LAYER_SERVICE, self.text_layers),
            (TRANSLATION_SERVICE, self.translations),
            (
                TRANSLATION_PROVENANCE_SERVICE,
                self.translation_provenance,
            ),
        )


ContributionFactory = Callable[
    [FilesystemServiceGraph], Iterable[ModuleContribution]
]


def compose_filesystem_engine(
    *,
    paths: FilesystemEnginePaths,
    resources: FilesystemEngineResources,
    catalogue: CatalogueBindings,
    replica: ReplicaBindings,
    interchange: InterchangeBindings,
    translation: TranslationBindings,
    contribution_factory: ContributionFactory,
) -> LibraryEngine:
    """Return one complete filesystem-backed service graph.

    Composition deliberately has no hidden defaults for legacy policies,
    codecs, JSON I/O, jobs, locks, or installed modules. The contribution
    factory binds the concrete service graph to capability manifests; the
    validated builder then seals discovery and withholds blocked services.
    """

    if not callable(contribution_factory):
        raise TypeError("contribution_factory must be callable")
    # Recovery remains host-owned, but composition refuses to expose any
    # service graph while an unfinished workspace transaction exists.
    with resources.write_set.workspace_lease():
        pass

    catalogue_path = resolve_workspace_path(
        resources.write_set.root,
        paths.catalogue,
        artifact="catalogue",
        directory=False,
    )
    entries_path = resolve_workspace_path(
        resources.write_set.root,
        paths.entries,
        artifact="entries",
        directory=True,
    )
    if workspace_paths_overlap(catalogue_path, entries_path):
        raise RepositoryError(
            "the catalogue and entries locations cannot overlap",
            code="unsafe_filesystem_engine_path",
            details={"artifact": "catalogue"},
        )
    entry_directory_for = _EntryDirectoryResolver(
        resources.write_set.root,
        entries_path,
    )

    items = ItemQueryService(
        FilesystemItemQueryRepository(
            catalogue.load_snapshot,
            validate_item_id=entry_directory_for.validate_item_id,
        ),
    )
    replica_repository = FilesystemReplicaRepository(
        entry_directory_for.layout_path,
        read_json=replica.read_json,
        write_json=replica.write_json,
        lock_context_for=replica.lock_context_for,
        workspace_context_for=lambda _item_id: (
            resources.write_set.workspace_lease()
        ),
    )
    text_layers = TextLayerService(
        replica.text_repository,
        replica.policies,
    )
    replica_service = ReplicaApplicationService(
        catalogue.descriptors,
        replica_repository,
        replica.policies,
        text_layers,
    )
    interchange_repository = FilesystemInterchangeRepository(
        resources.write_set,
        entry_directory_for=entry_directory_for,
        source_ids_for=interchange.source_ids_for,
        clean_region_id=interchange.clean_region_id,
        normalize_language=interchange.normalize_language,
        sanitize_document_name=interchange.sanitize_document_name,
        lock_context_for=resources.workspace_lock_context_for,
        recover=False,
    )
    translation_repository = FilesystemTranslationRepository(
        resources.write_set,
        entry_directory_for=entry_directory_for,
        item_exists_for=translation.item_exists_for,
        source_snapshot_for=translation.source_snapshot_for,
        source_reference_for=translation.source_reference_for,
        lock_context_for=resources.workspace_lock_context_for,
        recover=False,
    )
    item_command_repository = FilesystemItemCommandRepository(
        resources.write_set,
        catalogue_path=catalogue_path,
        decode_record=catalogue.decode_record,
        encode_record=catalogue.encode_record,
        allocate_item_id=catalogue.allocate_item_id,
        validate_item_id=entry_directory_for.validate_item_id,
        lock_context_for=catalogue.lock_context_for,
        recover=False,
    )

    graph = FilesystemServiceGraph(
        items=items,
        item_commands=ItemCommandService(item_command_repository),
        interchange=LibInterchangeService(
            interchange.planner,
            interchange_repository,
        ),
        jobs=resources.jobs,
        replica=replica_service,
        text_layers=text_layers,
        translations=TranslationService(
            catalogue.descriptors,
            translation_repository,
        ),
        translation_provenance=resources.provenance,
    )
    try:
        contributions = tuple(contribution_factory(graph))
    except TypeError:
        raise
    except Exception as exc:
        raise ServiceRegistryError(
            "the filesystem module contribution factory failed"
        ) from exc
    declared = {
        (binding.key, id(binding.service))
        for contribution in contributions
        for binding in contribution.bindings
    }
    undeclared = [
        key.token
        for key, service in graph.keyed_services()
        if (key, id(service)) not in declared
    ]
    if undeclared:
        raise ServiceRegistryError(
            "filesystem services are not bound by installed modules: "
            + ", ".join(undeclared)
        )
    engine = LibraryEngineBuilder(contributions).build()
    missing = [
        key.token
        for key, service in graph.keyed_services()
        if (
            engine.get_service(key) is None
            if key == ITEM_QUERY_SERVICE
            else engine.get_service(key) is not service
        )
    ]
    if missing:
        raise ServiceRegistryError(
            "filesystem services are not bound by active modules: "
            + ", ".join(missing)
        )
    return engine


__all__ = [
    "CatalogueBindings",
    "FilesystemEnginePaths",
    "FilesystemEngineResources",
    "FilesystemServiceGraph",
    "InterchangeBindings",
    "ReplicaBindings",
    "TranslationBindings",
    "compose_filesystem_engine",
]
