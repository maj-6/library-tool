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
from dataclasses import dataclass, field
from inspect import getattr_static
from pathlib import Path
from typing import Any, ContextManager

from ._filesystem_paths import (
    resolve_workspace_path,
    workspace_paths_overlap,
)

from ..adapters.filesystem import (
    AttachedPdfAssetLookup,
    FilesystemAttachedPdfInspector,
    FilesystemCanvasInspection,
    FilesystemCanvasPreparationRepository,
    FilesystemCanvasQueryRepository,
    FilesystemInterchangeRepository,
    FilesystemItemCommandRepository,
    FilesystemItemLifecycleRepository,
    FilesystemItemLifecycleReservationRepository,
    FilesystemItemQueryRepository,
    FilesystemOpenLibRepository,
    FilesystemReplicaRepository,
    FilesystemRepresentationCommandRepository,
    FilesystemTextLayerAggregateRepository,
    FilesystemTranslationRepository,
    RecoverableWriteSet,
)
from ..engine.canvas_commands import (
    CanvasPreparationItemSnapshot,
    CanvasPreparationRepresentationSnapshot,
    CanvasPreparationService,
)
from ..engine.canvases import CanvasQueryService
from ..engine.errors import RepositoryError
from ..engine.interchange import (
    LibImportPlannerPort,
    LibInterchangeService,
    OpenLibDraftFactory,
    OpenLibService,
)
from ..engine.item_commands import (
    ItemCommandPolicyPort,
    ItemCommandService,
    ItemDraft,
    ItemRecordSnapshot,
)
from ..engine.item_lifecycle import ItemLifecycleService
from ..engine.representation_commands import (
    RepresentationAggregateSnapshot,
    RepresentationAttachmentDraft,
    RepresentationCommandService,
)
from ..engine.items import ItemQueryService
from ..engine.jobs import JobManager
from ..engine.ports import (
    ItemRepositoryPort,
    ReplicaPolicyPort,
    TextLayerRepositoryPort,
)
from ..engine.providers import (
    ProviderDiscoveryService,
    ProviderHealthProbe,
    ProviderRegistry,
    ProviderSelectionPolicy,
    SecretStatusProbe,
)
from ..engine.replica import ReplicaApplicationService
from ..engine.secret_store import (
    SecretStoreRepositoryPort,
    SecretStoreService,
)
from ..engine.runtime import (
    CANVAS_PREPARATION_SERVICE,
    CANVAS_QUERY_SERVICE,
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_LIFECYCLE_SERVICE,
    ITEM_QUERY_SERVICE,
    JOB_SERVICE,
    LIB_OPEN_SERVICE,
    PROVIDER_DISCOVERY_SERVICE,
    REPLICA_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    SECRET_STORE_SERVICE,
    TEXT_LAYER_AGGREGATE_SERVICE,
    TEXT_LAYER_SERVICE,
    TRANSLATION_PROVENANCE_SERVICE,
    TRANSLATION_SERVICE,
    LibraryEngine,
    LibraryEngineBuilder,
    ModuleContribution,
    ServiceKey,
    ServiceRegistryError,
)
from ..engine.text_layer_aggregate import (
    TextLayerAggregateService,
    TextLayerSourceSnapshot,
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
AdvanceRestoredItemRecord = Callable[
    [str, Mapping[str, Any]], Mapping[str, Any]
]
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
RepresentationAggregateDecoder = Callable[
    [str, Mapping[str, Any]], RepresentationAggregateSnapshot
]
RepresentationPutRecord = Callable[
    [str, Mapping[str, Any], RepresentationAttachmentDraft], Mapping[str, Any]
]
RepresentationDetachRecord = Callable[
    [str, Mapping[str, Any], str], Mapping[str, Any]
]
CanvasItemSnapshotLoader = Callable[[str], CanvasPreparationItemSnapshot | None]
CanvasRepresentationSnapshotLoader = Callable[
    [str, str], CanvasPreparationRepresentationSnapshot | None
]
CanvasMediaInspector = Callable[
    [CanvasPreparationRepresentationSnapshot, Path],
    FilesystemCanvasInspection,
]
CanvasIdAllocator = Callable[[frozenset[str]], str]
TextLayerItemMembership = Callable[[str], bool]
TextLayerSourceSnapshotLoader = Callable[
    [str, str], TextLayerSourceSnapshot | None
]
TextLayerIdFactory = Callable[[], str]
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
class RepresentationBindings:
    """Transitional catalogue codecs for representation mutations."""

    decode_aggregate: RepresentationAggregateDecoder
    put_record: RepresentationPutRecord
    detach_record: RepresentationDetachRecord


@dataclass(frozen=True, slots=True)
class ItemLifecycleBindings:
    """Host codec needed to recreate an exact deleted catalogue record."""

    advance_restored_record: AdvanceRestoredItemRecord


@dataclass(frozen=True, slots=True)
class CatalogueBindings:
    """Legacy catalogue projection, identity, codec, and locking seams."""

    load_snapshot: ItemSnapshotLoader
    descriptors: ItemRepositoryPort
    decode_record: ItemRecordDecoder
    encode_record: ItemRecordEncoder
    allocate_item_id: ItemIdAllocator
    lock_context_for: CatalogueLockFactory
    representations: RepresentationBindings | None = None
    lifecycle: ItemLifecycleBindings | None = None
    item_command_policy: ItemCommandPolicyPort | None = None


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
    # Opening an archive as a *new* item is a composite of catalogue-create
    # and Replica interchange.  Hosts that install only existing-item
    # interchange omit this policy and the service/capability disappears.
    open_item_draft_for: OpenLibDraftFactory | None = None


@dataclass(frozen=True, slots=True)
class TranslationBindings:
    """Authoritative item/source lookup callbacks for translation storage."""

    item_exists_for: TranslationItemExists
    source_snapshot_for: TranslationSourceLoader
    source_reference_for: TranslationSourceReference


@dataclass(frozen=True, slots=True)
class CanvasBindings:
    """Complete local authority and inspection seams for canvas services.

    The bundle is intentionally indivisible: query and preparation share the
    exact same live snapshots, entry resolver, and broad host lock.  Media
    inspection is local/provider-free and canvas identity allocation must
    honor every active and retired identifier supplied by the repository.
    """

    item_snapshot_for: CanvasItemSnapshotLoader
    representation_snapshot_for: CanvasRepresentationSnapshotLoader
    inspect_media: CanvasMediaInspector
    allocate_canvas_id: CanvasIdAllocator
    lock_context_for: CatalogueLockFactory

    def __post_init__(self) -> None:
        for callback, name in (
            (self.item_snapshot_for, "item_snapshot_for"),
            (self.representation_snapshot_for, "representation_snapshot_for"),
            (self.inspect_media, "inspect_media"),
            (self.allocate_canvas_id, "allocate_canvas_id"),
            (self.lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")

    @classmethod
    def for_attached_pdfs(
        cls,
        *,
        item_snapshot_for: CanvasItemSnapshotLoader,
        representation_snapshot_for: CanvasRepresentationSnapshotLoader,
        asset_snapshot_for: AttachedPdfAssetLookup,
        allocate_canvas_id: CanvasIdAllocator,
        lock_context_for: CatalogueLockFactory,
    ) -> "CanvasBindings":
        """Bind the exact tracked-PDF inspector without a host path seam.

        The attachment lookup is called under the same broad lock as the live
        representation lookup.  It must project the same authority and return
        the digest-pinned asset for the requested representation revision.
        """

        return cls(
            item_snapshot_for=item_snapshot_for,
            representation_snapshot_for=representation_snapshot_for,
            inspect_media=FilesystemAttachedPdfInspector(asset_snapshot_for),
            allocate_canvas_id=allocate_canvas_id,
            lock_context_for=lock_context_for,
        )


@dataclass(frozen=True, slots=True)
class TextLayerAggregateBindings:
    """Complete authority and persistence seams for native text layers.

    This bundle is separate from :class:`ReplicaBindings`: installing the
    revisioned aggregate must neither replace nor silently alias the legacy
    ``replica.text-layers`` compatibility service.  Entry resolution and the
    broad mutation lock deliberately come from the common filesystem config
    and resources, so an optional module cannot introduce a second path or
    locking domain.
    """

    item_exists_for: TextLayerItemMembership
    source_snapshot_for: TextLayerSourceSnapshotLoader
    layer_id_factory: TextLayerIdFactory

    def __post_init__(self) -> None:
        for callback, name in (
            (self.item_exists_for, "item_exists_for"),
            (self.source_snapshot_for, "source_snapshot_for"),
            (self.layer_id_factory, "layer_id_factory"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")


@dataclass(frozen=True, slots=True)
class SecretStoreBindings:
    """Complete public secret-store persistence supplied by a host.

    The repository is already constructed and remains owned by the caller.
    Composition uses only its public status/mutation port; credential leases
    and adapter health ports are intentionally not part of this bundle.
    Static inspection avoids invoking repository methods or descriptors while
    validating the required structural contract.
    """

    repository: SecretStoreRepositoryPort

    def __post_init__(self) -> None:
        repository = self.repository
        if repository is None or isinstance(repository, type):
            raise TypeError(
                "repository must be a constructed SecretStoreRepositoryPort"
            )
        missing = []
        for name in ("status", "unit_of_work"):
            try:
                member = getattr_static(repository, name)
            except AttributeError:
                missing.append(name)
                continue
            if isinstance(member, (classmethod, staticmethod)):
                member = member.__func__
            if not callable(member):
                missing.append(name)
        if missing:
            raise TypeError(
                "repository must expose callable methods: "
                + ", ".join(missing)
            )


@dataclass(frozen=True, slots=True)
class ProviderDiscoveryBindings:
    """Provider descriptors, explicit selections, and cached status ports.

    The service copies the mapping and validates probe structure without
    invoking any probe. Provider SDKs and live health checks remain owned by a
    host or background monitor; engine composition and discovery only read
    sanitized snapshots. Its base executable set is empty; the sealed engine
    builder derives the bound service from exact active module capabilities.
    """

    registry: ProviderRegistry
    policy: ProviderSelectionPolicy
    health_probes: Mapping[str, ProviderHealthProbe] = field(
        default_factory=dict
    )
    secret_status_probe: SecretStatusProbe | None = None
    service: ProviderDiscoveryService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "service",
            ProviderDiscoveryService(
                self.registry,
                self.policy,
                health_probes=self.health_probes,
                secret_status_probe=self.secret_status_probe,
            ),
        )


class _CanvasAuthority:
    """Validate and sanitize one exact host canvas authority projection."""

    def __init__(self, bindings: CanvasBindings) -> None:
        self._bindings = bindings

    def item_snapshot_for(
        self,
        item_id: str,
    ) -> CanvasPreparationItemSnapshot | None:
        try:
            value = self._bindings.item_snapshot_for(item_id)
        except Exception as exc:
            raise RepositoryError(
                "the canvas item authority is unavailable",
                code="canvas_preparation_authority_unavailable",
                details={
                    "item_id": item_id,
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc
        if value is None:
            return None
        if (
            not isinstance(value, CanvasPreparationItemSnapshot)
            or value.item_id != item_id
        ):
            raise RepositoryError(
                "the canvas item authority returned an invalid snapshot",
                code="invalid_canvas_preparation_authority_snapshot",
                details={"item_id": item_id},
            )
        return value

    def representation_snapshot_for(
        self,
        item_id: str,
        representation_id: str,
    ) -> CanvasPreparationRepresentationSnapshot | None:
        try:
            value = self._bindings.representation_snapshot_for(
                item_id,
                representation_id,
            )
        except Exception as exc:
            raise RepositoryError(
                "the canvas representation authority is unavailable",
                code="canvas_preparation_authority_unavailable",
                details={
                    "item_id": item_id,
                    "representation_id": representation_id,
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc
        if value is None:
            return None
        if (
            not isinstance(value, CanvasPreparationRepresentationSnapshot)
            or value.item_id != item_id
            or value.representation_id != representation_id
        ):
            raise RepositoryError(
                "the canvas representation authority returned an invalid snapshot",
                code="invalid_canvas_preparation_authority_snapshot",
                details={
                    "item_id": item_id,
                    "representation_id": representation_id,
                },
            )
        return value

    def item_exists(self, item_id: str) -> bool:
        return self.item_snapshot_for(item_id) is not None

    def representation_revision_for(
        self,
        item_id: str,
        representation_id: str,
    ) -> str | None:
        value = self.representation_snapshot_for(item_id, representation_id)
        return None if value is None else value.revision


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
    item_lifecycle: ItemLifecycleService | None
    representation_commands: RepresentationCommandService | None
    interchange: LibInterchangeService
    lib_open: OpenLibService | None
    jobs: JobManager
    replica: ReplicaApplicationService
    text_layers: TextLayerService
    translations: TranslationService
    translation_provenance: TranslationProvenanceService
    canvas_query: CanvasQueryService | None = None
    canvas_preparation: CanvasPreparationService | None = None
    text_layer_aggregate: TextLayerAggregateService | None = None
    secret_store: SecretStoreService | None = None
    provider_discovery: ProviderDiscoveryService | None = None

    def __post_init__(self) -> None:
        if (self.canvas_query is None) != (self.canvas_preparation is None):
            raise ValueError(
                "canvas query and preparation services must be installed together"
            )
        if self.secret_store is not None and not isinstance(
            self.secret_store,
            SecretStoreService,
        ):
            raise TypeError("secret_store must be a SecretStoreService or None")
        if self.provider_discovery is not None and not isinstance(
            self.provider_discovery,
            ProviderDiscoveryService,
        ):
            raise TypeError(
                "provider_discovery must be a ProviderDiscoveryService or None"
            )

    def keyed_services(self) -> tuple[tuple[ServiceKey[Any], Any], ...]:
        services = (
            (ITEM_QUERY_SERVICE, self.items),
            (CANVAS_QUERY_SERVICE, self.canvas_query),
            (CANVAS_PREPARATION_SERVICE, self.canvas_preparation),
            (ITEM_COMMAND_SERVICE, self.item_commands),
            (ITEM_LIFECYCLE_SERVICE, self.item_lifecycle),
            (REPRESENTATION_COMMAND_SERVICE, self.representation_commands),
            (INTERCHANGE_SERVICE, self.interchange),
            (LIB_OPEN_SERVICE, self.lib_open),
            (JOB_SERVICE, self.jobs),
            (REPLICA_SERVICE, self.replica),
            (TEXT_LAYER_SERVICE, self.text_layers),
            (TEXT_LAYER_AGGREGATE_SERVICE, self.text_layer_aggregate),
            (SECRET_STORE_SERVICE, self.secret_store),
            (PROVIDER_DISCOVERY_SERVICE, self.provider_discovery),
            (TRANSLATION_SERVICE, self.translations),
            (
                TRANSLATION_PROVENANCE_SERVICE,
                self.translation_provenance,
            ),
        )
        return tuple(
            (key, service)
            for key, service in services
            if service is not None
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
    canvases: CanvasBindings | None = None,
    text_layer_aggregate: TextLayerAggregateBindings | None = None,
    secrets: SecretStoreBindings | None = None,
    providers: ProviderDiscoveryBindings | None = None,
) -> LibraryEngine:
    """Return one complete filesystem-backed service graph.

    Composition deliberately has no hidden defaults for legacy policies,
    codecs, JSON I/O, jobs, locks, or installed modules. The contribution
    factory binds the concrete service graph to capability manifests; the
    validated builder then seals discovery and withholds blocked services.
    """

    if not callable(contribution_factory):
        raise TypeError("contribution_factory must be callable")
    if canvases is not None and not isinstance(canvases, CanvasBindings):
        raise TypeError("canvases must be a CanvasBindings bundle or None")
    if text_layer_aggregate is not None and not isinstance(
        text_layer_aggregate,
        TextLayerAggregateBindings,
    ):
        raise TypeError(
            "text_layer_aggregate must be a TextLayerAggregateBindings "
            "bundle or None"
        )
    if secrets is not None and not isinstance(secrets, SecretStoreBindings):
        raise TypeError("secrets must be a SecretStoreBindings bundle or None")
    if providers is not None and not isinstance(
        providers,
        ProviderDiscoveryBindings,
    ):
        raise TypeError(
            "providers must be a ProviderDiscoveryBindings bundle or None"
        )
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

    canvas_query = None
    canvas_preparation = None
    if canvases is not None:
        canvas_authority = _CanvasAuthority(canvases)
        canvas_query = CanvasQueryService(
            FilesystemCanvasQueryRepository(
                resources.write_set,
                item_exists=canvas_authority.item_exists,
                representation_revision_for=(
                    canvas_authority.representation_revision_for
                ),
                entry_directory_for=entry_directory_for,
                lock_context_for=canvases.lock_context_for,
            )
        )
        canvas_preparation = CanvasPreparationService(
            FilesystemCanvasPreparationRepository(
                resources.write_set,
                item_snapshot_for=canvas_authority.item_snapshot_for,
                representation_snapshot_for=(
                    canvas_authority.representation_snapshot_for
                ),
                entry_directory_for=entry_directory_for,
                inspect_media=canvases.inspect_media,
                allocate_canvas_id=canvases.allocate_canvas_id,
                lock_context_for=canvases.lock_context_for,
                recover=False,
            )
        )

    native_text_layers = None
    if text_layer_aggregate is not None:
        native_text_layers = TextLayerAggregateService(
            FilesystemTextLayerAggregateRepository(
                resources.write_set,
                item_exists_for=text_layer_aggregate.item_exists_for,
                entry_directory_for=entry_directory_for,
                source_snapshot_for=(
                    text_layer_aggregate.source_snapshot_for
                ),
                layer_id_factory=text_layer_aggregate.layer_id_factory,
                lock_context_for=lambda: (
                    resources.workspace_lock_context_for("")
                ),
                # Startup recovery is owned by the process host.  Repeating
                # it here would introduce a second, narrower lock domain.
                recover=False,
            )
        )

    secret_store = (
        None
        if secrets is None
        else SecretStoreService(secrets.repository)
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
        item_exists_for=lambda item_id: (
            catalogue.descriptors.get(item_id) is not None
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
    # Lifecycle commands are optional, but their namespaced persistent state
    # remains authoritative if the module is later disabled or uninstalled.
    # The narrow reader is empty on a workspace that has never used lifecycle
    # commands and requires no lifecycle service dependency or host lock.
    identity_reservations = FilesystemItemLifecycleReservationRepository(
        resources.write_set
    )
    load_identity_reservations = identity_reservations.load
    item_command_repository = FilesystemItemCommandRepository(
        resources.write_set,
        catalogue_path=catalogue_path,
        decode_record=catalogue.decode_record,
        encode_record=catalogue.encode_record,
        allocate_item_id=catalogue.allocate_item_id,
        validate_item_id=entry_directory_for.validate_item_id,
        load_identity_reservations=load_identity_reservations,
        lock_context_for=catalogue.lock_context_for,
        recover=False,
    )
    item_lifecycle = None
    if catalogue.lifecycle is not None:
        lifecycle_repository = FilesystemItemLifecycleRepository(
            resources.write_set,
            item_repository=item_command_repository,
            entry_directory_for=entry_directory_for,
            advance_restored_record=(
                catalogue.lifecycle.advance_restored_record
            ),
            lock_context_for=lambda: (
                resources.workspace_lock_context_for("")
            ),
            deletion_guard_for=resources.jobs.item_deletion_guard,
        )
        item_lifecycle = ItemLifecycleService(lifecycle_repository)
    representation_commands = None
    if catalogue.representations is not None:
        representation_repository = FilesystemRepresentationCommandRepository(
            resources.write_set,
            item_repository=item_command_repository,
            decode_aggregate=catalogue.representations.decode_aggregate,
            put_record=catalogue.representations.put_record,
            detach_record=catalogue.representations.detach_record,
        )
        representation_commands = RepresentationCommandService(
            representation_repository
        )
    lib_open = None
    if interchange.open_item_draft_for is not None:
        lib_open_repository = FilesystemOpenLibRepository(
            resources.write_set,
            catalogue_path=catalogue_path,
            entry_directory_for=entry_directory_for,
            decode_record=catalogue.decode_record,
            encode_record=catalogue.encode_record,
            allocate_item_id=catalogue.allocate_item_id,
            clean_region_id=interchange.clean_region_id,
            normalize_language=interchange.normalize_language,
            validate_item_id=entry_directory_for.validate_item_id,
            load_identity_reservations=load_identity_reservations,
            sanitize_document_name=interchange.sanitize_document_name,
            lock_context_for=lambda: resources.workspace_lock_context_for(""),
            recover=False,
        )
        lib_open = OpenLibService(
            interchange.planner,
            lib_open_repository,
            interchange.open_item_draft_for,
        )

    graph = FilesystemServiceGraph(
        items=items,
        item_commands=ItemCommandService(
            item_command_repository,
            policy=catalogue.item_command_policy,
            allow_legacy_delete=item_lifecycle is None,
        ),
        item_lifecycle=item_lifecycle,
        representation_commands=representation_commands,
        interchange=LibInterchangeService(
            interchange.planner,
            interchange_repository,
        ),
        lib_open=lib_open,
        jobs=resources.jobs,
        replica=replica_service,
        text_layers=text_layers,
        translations=TranslationService(
            catalogue.descriptors,
            translation_repository,
        ),
        translation_provenance=resources.provenance,
        canvas_query=canvas_query,
        canvas_preparation=canvas_preparation,
        text_layer_aggregate=native_text_layers,
        secret_store=secret_store,
        provider_discovery=(None if providers is None else providers.service),
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
            if key in {
                ITEM_QUERY_SERVICE,
                PROVIDER_DISCOVERY_SERVICE,
            }
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
    "CanvasBindings",
    "CatalogueBindings",
    "FilesystemEnginePaths",
    "FilesystemEngineResources",
    "FilesystemServiceGraph",
    "InterchangeBindings",
    "ItemLifecycleBindings",
    "ReplicaBindings",
    "RepresentationBindings",
    "ProviderDiscoveryBindings",
    "SecretStoreBindings",
    "TranslationBindings",
    "TextLayerAggregateBindings",
    "compose_filesystem_engine",
]
