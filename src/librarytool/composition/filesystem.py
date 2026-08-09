"""Assemble the bundled engine from injected filesystem compatibility seams.

This module selects concrete filesystem adapters, but it owns no process
lifecycle.  Importing it performs no I/O, and :func:`compose_filesystem_engine`
does not recover a workspace, create a singleton, or start background work.
The host must settle recovery and construct shared resources before composing
an engine.  That distinction lets Flask, a CLI, or another host expose the
same service graph without introducing a second locking or recovery domain.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from inspect import getattr_static
from pathlib import Path
from typing import Any, ContextManager

from ._filesystem_paths import (
    resolve_workspace_path,
    workspace_paths_overlap,
)

from ..adapters.filesystem import (
    AttachedPdfAssetLookup,
    CanonicalTextLayerHumanAssertionReader,
    FilesystemAttachedPdfInspector,
    FilesystemCanvasInspection,
    FilesystemCanvasPreparationRepository,
    FilesystemCanvasQueryRepository,
    FilesystemCaptureAssetLifecycleStore,
    FilesystemCaptureDocumentArtifactRepository,
    FilesystemCaptureOriginalBackupStore,
    FilesystemCorrectionOcrProposalRepository,
    FilesystemCorrectionRepository,
    FilesystemCorrectionSourceSnapshotReader,
    FilesystemCorrectionTransformStore,
    FilesystemCorrectionsArtifactRepository,
    FilesystemInterchangeRepository,
    FilesystemItemCommandRepository,
    FilesystemItemLifecycleRepository,
    FilesystemItemLifecycleReservationRepository,
    FilesystemItemQueryRepository,
    PROCESSING_PRESET_RELATIVE,
    FilesystemOpenLibRepository,
    FilesystemProcessingPresetStore,
    FilesystemReplicaRepository,
    FilesystemRepresentationCommandRepository,
    FilesystemTextLayerAggregateRepository,
    FilesystemTranslationRepository,
    RecoverableWriteSet,
    WriteSetError,
)
from ..engine.canvas_commands import (
    CanvasPreparationItemSnapshot,
    CanvasPreparationRepresentationSnapshot,
    CanvasPreparationService,
)
from ..engine.correction_projection import (
    CorrectionAggregateProjector,
    CorrectionProjectionService,
    reconcile_correction_aggregates,
)
from ..engine.correction_ocr import (
    CORRECTION_OCR_MAX_SOURCE_BYTES,
    CORRECTION_REOCR_OPERATION_PREFIX,
    CorrectionOcrFollowupService,
    CorrectionOcrProposalCatalogService,
    CorrectionOcrProposalQueryService,
    CorrectionOcrProviderPort,
    CorrectionReocrService,
)
from ..engine.corrections import CorrectionService
from ..engine.correction_transforms import (
    CorrectionTransformService,
    CorrectionTransformWorker,
)
from ..engine.document_artifacts import (
    DocumentArtifactCatalogService,
    DocumentResourcePageService,
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
from ..engine.processing_presets import ProcessingPresetService
from ..engine.secret_store import (
    SecretStoreRepositoryPort,
    SecretStoreService,
)
from ..engine.runtime import (
    CANVAS_PREPARATION_SERVICE,
    CANVAS_QUERY_SERVICE,
    CORRECTION_CAPTION_SERVICE,
    CORRECTION_METADATA_SERVICE,
    CORRECTION_REVIEW_SERVICE,
    CORRECTION_SERVICE,
    CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE,
    CORRECTION_OCR_PROPOSAL_QUERY_SERVICE,
    CORRECTION_REOCR_SERVICE,
    CORRECTION_TRANSFORM_SERVICE,
    DOCUMENT_ARTIFACT_CATALOG_SERVICE,
    DOCUMENT_RESOURCE_PAGE_SERVICE,
    INTERCHANGE_SERVICE,
    ITEM_COMMAND_SERVICE,
    ITEM_LIFECYCLE_SERVICE,
    ITEM_QUERY_SERVICE,
    JOB_SERVICE,
    LIB_OPEN_SERVICE,
    PROVIDER_DISCOVERY_SERVICE,
    RASTER_ARTIFACT_QUERY_SERVICE,
    REPLICA_SERVICE,
    REPRESENTATION_COMMAND_SERVICE,
    PROCESSING_PRESET_SERVICE,
    SECRET_STORE_SERVICE,
    SPATIAL_ANNOTATION_QUERY_SERVICE,
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
from ..engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterResourceRef,
    ResourceState,
)
from ..engine.spatial_annotations import (
    SpatialAnnotationKey,
    SpatialAnnotationProjectorPort,
    SpatialAnnotationView,
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
CorrectionsItemMembership = Callable[[str], bool]
CorrectionsCaptureIdentity = Callable[[str], str | None]
CorrectionsCaptureDirectory = Callable[[str], Path]
CorrectionsEntryDirectory = Callable[[str], Path]
CorrectionsRepresentationRevision = Callable[[str, str], str | None]
CorrectionsTextLayerItemIdentity = Callable[[str], str | None]
CorrectionsItemUpdatedAtPublication = Callable[[str], tuple[Path, bytes, str]]
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
class CorrectionsBindings:
    """Read authority for capture and Mistral Corrections projections.

    Capture files may live outside the engine write-set (the desktop stores
    them beside ``output``). ``capture_authority_root`` explicitly confines
    that borrowed read authority without moving the write-set or its recovery
    journals. ``entry_directory_for`` may map a canonical Corrections identity
    to an active compatibility entry; the artifact adapter still confines the
    result to the engine workspace. ``text_layer_item_id_for`` may likewise
    map that identity to the active native text-layer owner, or return ``None``
    when a capture-only item has no native text store. Omitting either mapping
    uses the common entry resolver and the canonical identity, respectively.
    """

    item_exists_for: CorrectionsItemMembership
    capture_id_for: CorrectionsCaptureIdentity
    capture_directory_for: CorrectionsCaptureDirectory
    capture_authority_root: Path
    representation_revision_for: CorrectionsRepresentationRevision
    lock_context_for: CatalogueLockFactory
    entry_directory_for: CorrectionsEntryDirectory | None = None
    job_start_context_for: ItemLockFactory | None = None
    ocr_provider: CorrectionOcrProviderPort | None = None
    text_layer_item_id_for: CorrectionsTextLayerItemIdentity | None = None
    transaction_root: Path | None = None
    original_backup_root: Path | None = None
    item_updated_at_publication_for: (
        CorrectionsItemUpdatedAtPublication | None
    ) = None

    def __post_init__(self) -> None:
        capture_authority_root = Path(self.capture_authority_root)
        if not capture_authority_root.is_absolute():
            raise ValueError("capture_authority_root must be absolute")
        object.__setattr__(
            self,
            "capture_authority_root",
            capture_authority_root,
        )
        if (self.transaction_root is None) != (self.original_backup_root is None):
            raise ValueError(
                "transaction_root and original_backup_root must be configured together"
            )
        if self.transaction_root is not None:
            transaction_root = Path(self.transaction_root)
            backup_root = Path(self.original_backup_root)  # type: ignore[arg-type]
            if not transaction_root.is_absolute() or not backup_root.is_absolute():
                raise ValueError("correction backup roots must be absolute")
            for child, name in (
                (capture_authority_root, "capture_authority_root"),
                (backup_root, "original_backup_root"),
            ):
                try:
                    child.relative_to(transaction_root)
                except ValueError as exc:
                    raise ValueError(
                        f"{name} must be below transaction_root"
                    ) from exc
            object.__setattr__(self, "transaction_root", transaction_root)
            object.__setattr__(self, "original_backup_root", backup_root)
        for callback, name in (
            (self.item_exists_for, "item_exists_for"),
            (self.capture_id_for, "capture_id_for"),
            (self.capture_directory_for, "capture_directory_for"),
            (
                self.representation_revision_for,
                "representation_revision_for",
            ),
            (self.lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if (
            self.entry_directory_for is not None
            and not callable(self.entry_directory_for)
        ):
            raise TypeError("entry_directory_for must be callable or None")
        if (
            self.text_layer_item_id_for is not None
            and not callable(self.text_layer_item_id_for)
        ):
            raise TypeError(
                "text_layer_item_id_for must be callable or None"
            )
        if (
            self.item_updated_at_publication_for is not None
            and not callable(self.item_updated_at_publication_for)
        ):
            raise TypeError(
                "item_updated_at_publication_for must be callable or None"
            )
        if (
            self.job_start_context_for is not None
            and not callable(self.job_start_context_for)
        ):
            raise TypeError("job_start_context_for must be callable or None")
        if (
            self.ocr_provider is not None
            and not isinstance(self.ocr_provider, CorrectionOcrProviderPort)
        ):
            raise TypeError(
                "ocr_provider must implement CorrectionOcrProviderPort or be None"
            )


@dataclass(frozen=True, slots=True)
class TextLayerAggregateBindings:
    """Complete authority and persistence seams for native text layers.

    This bundle is separate from :class:`ReplicaBindings`: installing the
    revisioned aggregate must neither replace nor silently alias the legacy
    ``replica.text-layers`` compatibility service. Entry resolution comes from
    the common filesystem config, and composition selects one shared broad
    mutation lock. When Corrections also consumes this aggregate, both use its
    reentrant catalogue-lock adapter so the cross-aggregate snapshot cannot
    introduce a second nested lock domain.
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


class _ReentrantContextFactory:
    """Make one injected lock domain safely nestable on the owning thread."""

    def __init__(self, factory: Callable[[], ContextManager[Any]]) -> None:
        self._factory = factory
        self._state = threading.local()

    @contextmanager
    def __call__(self):
        depth = int(getattr(self._state, "depth", 0))
        if depth:
            self._state.depth = depth + 1
            try:
                yield
            finally:
                self._state.depth = depth
            return
        with self._factory():
            self._state.depth = 1
            try:
                yield
            finally:
                self._state.depth = 0


class _CorrectionProjectionUnion:
    """Combine live source evidence with immutable correction outputs."""

    def __init__(
        self,
        base: RasterArtifactProjectorPort,
        transforms: FilesystemCorrectionTransformStore,
        *,
        write_set: RecoverableWriteSet,
        lock_context_for: CatalogueLockFactory,
        capture_asset_lifecycle: FilesystemCaptureAssetLifecycleStore | None = None,
        original_backups: FilesystemCaptureOriginalBackupStore | None = None,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not callable(lock_context_for):
            raise TypeError("lock_context_for must be callable")
        self._base = base
        self._transforms = transforms
        self._write_set = write_set
        self._lock_context_for = lock_context_for
        self._capture_asset_lifecycle = capture_asset_lifecycle
        self._original_backups = original_backups

    @contextmanager
    def _read_context(self):
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    yield
        except WriteSetError as exc:
            raise RepositoryError(
                "the correction raster workspace is unavailable",
                code=exc.code,
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc

    def list_raster_artifacts(
        self,
        item_id: str,
    ) -> tuple[RasterArtifactView, ...]:
        with self._read_context():
            # The base lookup is first so missing-item semantics remain owned
            # by the catalogue/capture authority rather than the private
            # output store.
            base = tuple(self._base.list_raster_artifacts(item_id))
            projection = self._transforms.project_item(item_id)
            heads = self._active_display_heads(
                item_id,
                projection,
                base_values=base,
            )
            deleted = self._capture_deleted_artifact_ids(item_id)
            # A display-head output is represented by its stable capture slot,
            # never as a second raster card while active. A lifecycle-deleted
            # root must not resurrect it either; other inactive heads remain
            # visible as immutable stale/replaced-source history.
            hidden = {
                head.artifact.key.artifact_id.casefold()
                for head in heads.values()
            }
            hidden.update(
                head.artifact.key.artifact_id.casefold()
                for head in projection.display_heads
                if head.root_key.artifact_id.casefold() in deleted
            )
            values = (
                *(
                    self._display_alias(value, heads.get(value.key.artifact_id.casefold()))
                    for value in base
                ),
                *(
                    value
                    for value in projection.raster_artifacts
                    if value.key.artifact_id.casefold() not in hidden
                ),
            )
        identities = [value.key.artifact_id.casefold() for value in values]
        if len(identities) != len(set(identities)):
            raise RepositoryError(
                "a correction transform output reuses a raster identity",
                code="invalid_correction_transform_storage",
                details={"item_id": item_id},
            )
        return tuple(sorted(values, key=lambda value: value.key.artifact_id))

    def list_capture_import_marks(
        self,
        item_ids: Sequence[str],
    ) -> tuple[Mapping[str, Any], ...]:
        """Delegate capture counts and import stamps to the authority adapter."""

        marks = getattr(self._base, "list_capture_import_marks", None)
        if not callable(marks):
            return ()
        with self._read_context():
            values = marks(item_ids)
        if (
            isinstance(values, (str, bytes))
            or not isinstance(values, Sequence)
            or any(not isinstance(value, Mapping) for value in values)
        ):
            raise RepositoryError(
                "the correction capture index returned invalid marks",
                code="invalid_corrections_index_projection",
            )
        return tuple(values)

    def list_capture_index_hints(
        self,
        item_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        """Delegate navigation-only capture hints to the authority adapter."""

        hints = getattr(self._base, "list_capture_index_hints", None)
        if not callable(hints):
            return ()
        with self._read_context():
            snapshotter = getattr(
                self._base,
                "capture_index_hint_snapshot",
                None,
            )
            if callable(snapshotter):
                snapshot = snapshotter(item_id)
                if not isinstance(snapshot, Mapping):
                    raise RepositoryError(
                        "the correction capture index returned an invalid snapshot",
                        code="invalid_corrections_index_projection",
                        details={"item_id": item_id},
                    )
            else:
                snapshot = {
                    "hints": hints(item_id),
                    "authorities": {},
                }
            head_hints = self._transforms.project_display_head_hints(item_id)
            return self._project_capture_index_hint_snapshot(
                item_id,
                snapshot,
                head_hints,
            )

    def list_capture_index_hints_many(
        self,
        item_ids: Sequence[str],
    ) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
        """Batch capture hints under one lease instead of one per item."""

        items = tuple(item_ids)
        snapshots_many = getattr(
            self._base,
            "capture_index_hint_snapshots_many",
            None,
        )
        snapshotter = getattr(
            self._base,
            "capture_index_hint_snapshot",
            None,
        )
        many = getattr(self._base, "list_capture_index_hints_many", None)
        single = getattr(self._base, "list_capture_index_hints", None)
        with self._read_context():
            if callable(snapshots_many):
                snapshots = snapshots_many(items)
            elif callable(snapshotter):
                snapshots = {
                    item_id: snapshotter(item_id) for item_id in items
                }
            elif callable(many):
                rows_by_item = many(items)
                if not isinstance(rows_by_item, Mapping):
                    raise RepositoryError(
                        "the correction capture index returned invalid hints",
                        code="invalid_corrections_index_projection",
                    )
                snapshots = {
                    item_id: {"hints": rows, "authorities": {}}
                    for item_id, rows in rows_by_item.items()
                }
            elif callable(single):
                snapshots = {
                    item_id: {
                        "hints": single(item_id),
                        "authorities": {},
                    }
                    for item_id in items
                }
            else:
                return {str(item_id): () for item_id in items}
            if not isinstance(snapshots, Mapping) or any(
                not isinstance(item_id, str)
                or not isinstance(snapshot, Mapping)
                for item_id, snapshot in snapshots.items()
            ):
                raise RepositoryError(
                    "the correction capture index returned invalid hints",
                    code="invalid_corrections_index_projection",
                )
            project_many = getattr(
                self._transforms,
                "project_display_head_hints_many",
                None,
            )
            if callable(project_many):
                heads_by_item = project_many(tuple(snapshots))
            else:
                heads_by_item = {
                    item_id: self._transforms.project_display_head_hints(
                        item_id
                    )
                    for item_id in snapshots
                }
            if not isinstance(heads_by_item, Mapping):
                raise RepositoryError(
                    "the correction capture index returned invalid hints",
                    code="invalid_corrections_index_projection",
                )
            return {
                item_id: self._project_capture_index_hint_snapshot(
                    item_id,
                    snapshot,
                    heads_by_item.get(item_id, ()),
                )
                for item_id, snapshot in snapshots.items()
            }

    def _project_capture_index_hint_snapshot(
        self,
        item_id: str,
        snapshot: Mapping[str, Any],
        head_hints: Sequence[Any],
    ) -> tuple[Mapping[str, Any], ...]:
        values = snapshot.get("hints")
        authorities = snapshot.get("authorities")
        if (
            isinstance(values, (str, bytes))
            or not isinstance(values, Sequence)
            or any(not isinstance(value, Mapping) for value in values)
            or not isinstance(authorities, Mapping)
            or any(
                not isinstance(identity, str)
                or not isinstance(authority, Mapping)
                for identity, authority in authorities.items()
            )
        ):
            raise RepositoryError(
                "the correction capture index returned invalid hints",
                code="invalid_corrections_index_projection",
                details={"item_id": item_id},
            )
        heads = self._active_display_head_hints(
            item_id,
            head_hints,
            values=values,
            authorities=authorities,
        )
        return tuple(
            {
                **value,
                "revision": self._display_head_revision(
                    "index",
                    authority_revision,
                    self._display_head_revision(
                        "correction-display-hint",
                        heads[identity].operation_id,
                        heads[identity].publication_sha256,
                        heads[identity].output_artifact_revision,
                        heads[identity].output_content_sha256,
                    ),
                ),
                "resource_state": ResourceState.AVAILABLE.value,
            }
            if (
                isinstance(
                    (artifact_id := value.get("artifact_id")),
                    str,
                )
                and (identity := artifact_id.casefold()) in heads
                and isinstance(
                    (authority_revision := value.get("revision")),
                    str,
                )
            )
            else value
            for value in values
        )

    def get_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be a RasterArtifactKey")
        with self._read_context():
            base = self._base.get_raster_artifact(key)
            projection = self._transforms.project_item(key.item_id)
            heads = self._active_display_heads(
                key.item_id,
                projection,
                base_values=(() if base is None else (base,)),
            )
            transformed = next(
                (
                    value
                    for value in projection.raster_artifacts
                    if value.key == key
                ),
                None,
            )
        if base is not None:
            head = heads.get(key.artifact_id.casefold())
            if head is not None:
                return self._display_alias(base, head)
        if base is not None and transformed is not None:
            raise RepositoryError(
                "a correction transform output reuses a raster identity",
                code="invalid_correction_transform_storage",
                details={"item_id": key.item_id},
            )
        return base if base is not None else transformed

    def get_capture_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        """Return a stable capture slot, including its current display head."""

        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be a RasterArtifactKey")
        getter = getattr(self._base, "get_capture_raster_artifact", None)
        if not callable(getter):
            return None
        with self._read_context():
            base = getter(key)
            if base is None:
                return None
            projection = self._transforms.project_item(key.item_id)
            heads = self._active_display_heads(
                key.item_id,
                projection,
                base_values=(base,),
            )
            return self._display_alias(
                base,
                heads.get(key.artifact_id.casefold()),
            )

    def resolve_capture_preview(
        self,
        item_id: str,
        artifact_id: str,
    ) -> Any:
        key = RasterArtifactKey(item_id, artifact_id)
        with self._read_context():
            base = self._base.get_raster_artifact(key)
            projection = self._transforms.project_item(item_id)
            heads = self._active_display_heads(
                item_id,
                projection,
                base_values=(() if base is None else (base,)),
            )
            head = heads.get(artifact_id.casefold())
            if base is not None and head is not None:
                return self._transforms.resolve_raster_resource(
                    item_id,
                    head.artifact.resource,
                )
            preview = getattr(self._base, "resolve_capture_preview", None)
            if callable(preview):
                resolved = preview(item_id, artifact_id)
                if resolved is not None:
                    return resolved
            # Non-capture and transformed artifacts preserve the ordinary
            # authoritative keyed projection/resource grant as a fallback.
            artifact = self.get_raster_artifact(key)
            if artifact is None or artifact.resource is None:
                return None
            return self.resolve_raster_resource(item_id, artifact.resource)

    def resolve_raster_resource(
        self,
        item_id: str,
        resource: RasterResourceRef,
    ) -> Any:
        if not isinstance(resource, RasterResourceRef):
            raise TypeError("resource must be a RasterResourceRef")
        with self._read_context():
            if resource.resource_id.startswith("correction-raster:"):
                # Recheck item membership under the same authority lock as
                # the immutable-object snapshot.
                membership = getattr(self._base, "assert_item_exists", None)
                if callable(membership):
                    membership(item_id)
                else:
                    self._base.list_raster_artifacts(item_id)
                return self._transforms.resolve_raster_resource(
                    item_id,
                    resource,
                )
            resolver = getattr(self._base, "resolve_raster_resource", None)
            if not callable(resolver):
                return None
            return resolver(item_id, resource)

    def resolve_original_backup(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
    ) -> Any:
        if self._original_backups is None:
            return None
        return self._original_backups.resolve_original_backup(
            item_id,
            artifact_id,
            expected_revision,
        )

    def restore_original_backup(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
        operation_id: str,
    ) -> Mapping[str, Any]:
        if self._original_backups is None:
            raise RepositoryError(
                "the original backup store is unavailable",
                code="capture_original_backup_unavailable",
                retryable=True,
            )
        return self._original_backups.restore_original_backup(
            item_id,
            artifact_id,
            expected_revision,
            operation_id,
        )

    def delete_capture_asset(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
        operation_id: str,
    ) -> Mapping[str, Any]:
        if self._capture_asset_lifecycle is None:
            raise RepositoryError(
                "the capture asset lifecycle store is unavailable",
                code="capture_asset_lifecycle_unavailable",
                retryable=True,
            )
        return self._capture_asset_lifecycle.delete_capture_asset(
            item_id,
            artifact_id,
            expected_revision,
            operation_id,
        )

    def restore_capture_asset(
        self,
        item_id: str,
        artifact_id: str,
        inverse: Mapping[str, Any],
        operation_id: str,
    ) -> Mapping[str, Any]:
        if self._capture_asset_lifecycle is None:
            raise RepositoryError(
                "the capture asset lifecycle store is unavailable",
                code="capture_asset_lifecycle_unavailable",
                retryable=True,
            )
        return self._capture_asset_lifecycle.restore_capture_asset(
            item_id,
            artifact_id,
            inverse,
            operation_id,
        )

    def list_spatial_annotations(
        self,
        item_id: str,
        *,
        representation_id: str = "",
        canvas_id: str = "",
    ) -> tuple[SpatialAnnotationView, ...]:
        with self._read_context():
            base = tuple(
                self._base.list_spatial_annotations(
                    item_id,
                    representation_id=representation_id,
                    canvas_id=canvas_id,
                )
            )
            projection = self._transforms.project_item(item_id)
            heads = self._active_display_heads(item_id, projection)
            logical_ids = set(heads)
            deleted = self._capture_deleted_artifact_ids(item_id)
            # Do not let an inactive display head reappear through its mapped
            # annotations when its capture root is lifecycle-deleted. Stale
            # history and extraction annotations remain independently visible.
            physical_ids = {
                head.artifact.key.artifact_id.casefold()
                for head in heads.values()
            }
            physical_ids.update(
                head.artifact.key.artifact_id.casefold()
                for head in projection.display_heads
                if head.root_key.artifact_id.casefold() in deleted
            )
            base_by_annotation_id = {
                value.key.annotation_id.casefold(): value
                for value in base
            }
            values = [
                value
                for value in base
                if not any(
                    linked.casefold() in logical_ids
                    for linked in value.linked_artifact_ids
                )
            ]
            values.extend(
                value
                for value in projection.spatial_annotations
                if (
                    not any(
                        linked.casefold() in physical_ids
                        for linked in value.linked_artifact_ids
                    )
                    and (
                        not representation_id
                        or value.source.representation_id == representation_id
                    )
                    and (not canvas_id or value.source.canvas_id == canvas_id)
                )
            )
            for identity, head in heads.items():
                for annotation in head.spatial_annotations:
                    if (
                        representation_id
                        and annotation.source.representation_id != representation_id
                    ) or (canvas_id and annotation.source.canvas_id != canvas_id):
                        continue
                    linked = tuple(
                        dict.fromkeys(
                            head.logical_key.artifact_id
                            if value.casefold()
                            == head.artifact.key.artifact_id.casefold()
                            else value
                            for value in annotation.linked_artifact_ids
                        )
                    )
                    transform_extension = annotation.extensions.get(
                        "correction_transform",
                        {},
                    )
                    source_annotation_id = (
                        transform_extension.get("source_annotation_id")
                        if isinstance(transform_extension, Mapping)
                        else None
                    )
                    key = (
                        SpatialAnnotationKey(item_id, source_annotation_id)
                        if isinstance(source_annotation_id, str)
                        and source_annotation_id
                        else annotation.key
                    )
                    original = base_by_annotation_id.get(
                        key.annotation_id.casefold()
                    )
                    extensions = (
                        {
                            **dict(original.extensions),
                            **dict(annotation.extensions),
                        }
                        if original is not None
                        else annotation.extensions
                    )
                    extensions = dict(extensions)
                    android_geometry = extensions.get("android_geometry")
                    if isinstance(android_geometry, Mapping):
                        projected_geometry = dict(android_geometry)
                        projected_geometry.pop("source_revision", None)
                        projected_geometry.pop("display_revision", None)
                        if projected_geometry:
                            extensions["android_geometry"] = projected_geometry
                        else:
                            extensions.pop("android_geometry", None)
                    values.append(
                        replace(
                            annotation,
                            key=key,
                            revision=self._display_head_revision(
                                "correction-annotation",
                                (
                                    original.revision
                                    if original is not None
                                    else ""
                                ),
                                annotation.revision,
                                key.annotation_id,
                            ),
                            order=(
                                original.order
                                if original is not None
                                else annotation.order
                            ),
                            label=(
                                original.label
                                if original is not None
                                else annotation.label
                            ),
                            linked_artifact_ids=linked,
                            extensions=extensions,
                        )
                    )
            extraction_links: dict[str, list[Any]] = {}
            for link in projection.extraction_links:
                extraction_links.setdefault(
                    link.annotation_id.casefold(),
                    [],
                ).append(link)
            if extraction_links:
                values = [
                    self._with_extraction_links(
                        value,
                        extraction_links.get(
                            value.key.annotation_id.casefold(),
                            (),
                        ),
                    )
                    for value in values
                ]
        identities = [value.key.annotation_id.casefold() for value in values]
        if len(identities) != len(set(identities)):
            raise RepositoryError(
                "a correction transform output reuses a spatial identity",
                code="invalid_correction_transform_storage",
                details={"item_id": item_id},
            )
        return tuple(sorted(values, key=lambda value: value.key.annotation_id))

    @staticmethod
    def _validated_capture_authorities(
        item_id: str,
        authorities: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Mapping[str, Any]]:
        authority_fields = frozenset(
            {
                "artifact_id",
                "source_revision",
                "source_sha256",
                "representation_id",
                "representation_revision",
                "canvas_id",
                "original_backed_up",
                "active_operation_id",
            }
        )
        for identity, authority in authorities.items():
            if (
                not isinstance(identity, str)
                or not isinstance(authority, Mapping)
                or frozenset(authority) != authority_fields
            ):
                raise RepositoryError(
                    "the correction capture index returned invalid authority pins",
                    code="invalid_corrections_index_projection",
                    details={"item_id": item_id},
                )
            artifact_id = authority["artifact_id"]
            source_revision = authority["source_revision"]
            source_sha256 = authority["source_sha256"]
            if (
                not isinstance(artifact_id, str)
                or identity != artifact_id.casefold()
                or any(
                    not isinstance(authority[field], str)
                    or not authority[field]
                    for field in authority_fields
                    - {
                        "source_revision",
                        "source_sha256",
                        "original_backed_up",
                        "active_operation_id",
                    }
                )
                or not isinstance(source_revision, str)
                or not isinstance(source_sha256, str)
                or bool(source_revision) != bool(source_sha256)
                or type(authority["original_backed_up"]) is not bool
                or not isinstance(authority["active_operation_id"], str)
                or (
                    not authority["original_backed_up"]
                    and authority["active_operation_id"]
                )
            ):
                raise RepositoryError(
                    "the correction capture index returned invalid authority pins",
                    code="invalid_corrections_index_projection",
                    details={"item_id": item_id},
                )
        return dict(authorities)

    @classmethod
    def _active_display_head_hints(
        cls,
        item_id: str,
        head_hints: Sequence[Any],
        *,
        values: Sequence[Mapping[str, Any]],
        authorities: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        hints_by_id = {
            artifact_id.casefold(): value
            for value in values
            if isinstance((artifact_id := value.get("artifact_id")), str)
        }
        active: dict[str, Any] = {}
        validated = cls._validated_capture_authorities(item_id, authorities)
        for head in head_hints:
            identity = head.logical_key.artifact_id.casefold()
            root_identity = head.root_key.artifact_id.casefold()
            authority = validated.get(root_identity)
            hint = hints_by_id.get(identity)
            source = head.root_source
            if (
                authority is None
                or hint is None
                or head.logical_key.item_id != item_id
                or head.root_key.item_id != item_id
                or (
                    root_identity == identity
                    and hint.get("resource_state")
                    != ResourceState.AVAILABLE.value
                )
                or authority["artifact_id"].casefold() != root_identity
                or authority["source_revision"]
                != head.root_source_revision
                or authority["source_sha256"] != head.root_source_sha256
                or authority["representation_id"]
                != source.representation_id
                or authority["representation_revision"]
                != source.representation_revision
                or authority["canvas_id"] != source.canvas_id
                or (
                    authority["original_backed_up"]
                    and authority["active_operation_id"] != head.operation_id
                )
            ):
                continue
            active[identity] = head
        return active

    def _capture_display_authorities(
        self,
        item_id: str,
    ) -> dict[str, Mapping[str, Any]] | None:
        snapshotter = getattr(self._base, "capture_index_hint_snapshot", None)
        if not callable(snapshotter):
            # Preserve the generic union's pre-#297 behavior for alternate
            # projectors which do not expose capture manifest authority.
            return None
        snapshot = snapshotter(item_id)
        authorities = (
            snapshot.get("authorities")
            if isinstance(snapshot, Mapping)
            else None
        )
        if not isinstance(authorities, Mapping):
            raise RepositoryError(
                "the correction capture index returned invalid authority pins",
                code="invalid_corrections_index_projection",
                details={"item_id": item_id},
            )
        return self._validated_capture_authorities(item_id, authorities)

    def _capture_deleted_artifact_ids(self, item_id: str) -> frozenset[str]:
        deleted_for = getattr(self._base, "capture_deleted_artifact_ids", None)
        if not callable(deleted_for):
            return frozenset()
        values = deleted_for(item_id)
        if (
            isinstance(values, (str, bytes))
            or not isinstance(values, Sequence)
            or any(not isinstance(value, str) or not value for value in values)
        ):
            raise RepositoryError(
                "the correction capture index returned invalid lifecycle pins",
                code="invalid_corrections_index_projection",
                details={"item_id": item_id},
            )
        identities = tuple(value.casefold() for value in values)
        if len(identities) != len(set(identities)):
            raise RepositoryError(
                "the correction capture index returned aliased lifecycle pins",
                code="invalid_corrections_index_projection",
                details={"item_id": item_id},
            )
        return frozenset(identities)

    def _active_display_heads(
        self,
        item_id: str,
        projection: Any,
        *,
        base_values: Sequence[RasterArtifactView] = (),
    ) -> dict[str, Any]:
        if not projection.display_heads:
            return {}
        base_by_id = {
            value.key.artifact_id.casefold(): value
            for value in base_values
        }
        authorities = self._capture_display_authorities(item_id)
        active: dict[str, Any] = {}
        for head in projection.display_heads:
            identity = head.logical_key.artifact_id.casefold()
            root_identity = head.root_key.artifact_id.casefold()
            authority = (
                authorities.get(root_identity)
                if authorities is not None
                else None
            )
            logical = base_by_id.get(identity)
            if logical is None:
                logical = self._base.get_raster_artifact(head.logical_key)
            root = base_by_id.get(root_identity)
            if root is None:
                root = self._base.get_raster_artifact(head.root_key)
            logical_original_backed_up = (
                logical is not None
                and isinstance(
                    logical.extensions.get("original_backup"),
                    Mapping,
                )
            )
            root_is_available = (
                root is not None
                and root.key == head.root_key
                and root.resource_state is ResourceState.AVAILABLE
                and root.resource is not None
                and root.content_sha256 == head.root_source_sha256
                and root.resource.revision == head.root_source_revision
                and root.source == head.root_source
            )
            # #297 deliberately removes a promoted original from the hot
            # capture directory and suppresses its RasterArtifactView.  Its
            # private manifest authority still pins the immutable backup bytes
            # and the one operation allowed to project from them.  This
            # authority-only fallback is never valid for a display root: a
            # missing display must continue to fail closed.
            backed_up_original_is_authoritative = (
                root_identity != identity
                and root is None
                and authority is not None
                and authority["original_backed_up"]
                and authority["active_operation_id"] == head.operation_id
                and authority["artifact_id"].casefold() == root_identity
                and authority["source_revision"]
                == head.root_source_revision
                and authority["source_sha256"] == head.root_source_sha256
                and authority["representation_id"]
                == head.root_source.representation_id
                and authority["representation_revision"]
                == head.root_source.representation_revision
                and authority["canvas_id"] == head.root_source.canvas_id
            )
            if (
                logical is None
                or logical.key != head.logical_key
                or logical.key.item_id != item_id
                or head.logical_key.item_id != item_id
                or head.root_key.item_id != item_id
                or not (
                    root_is_available
                    or backed_up_original_is_authoritative
                )
                or (
                    logical_original_backed_up
                    and (
                        authority is None
                        or not authority["original_backed_up"]
                        or authority["active_operation_id"]
                        != head.operation_id
                    )
                )
                or (
                    authority is not None
                    and authority["original_backed_up"]
                    and authority["active_operation_id"]
                    != head.operation_id
                )
            ):
                continue
            active[identity] = head
        return active

    @staticmethod
    def _display_alias(base: RasterArtifactView, head: Any) -> RasterArtifactView:
        if head is None:
            return base
        corrected = head.artifact
        extensions = dict(base.extensions)
        extensions.pop("recipe", None)
        extensions.pop("rendition", None)
        extensions.update(corrected.extensions)
        extensions["correction_display_head"] = {
            "operation_id": head.operation_id,
            "output_artifact_id": corrected.key.artifact_id,
            "root_source_revision": head.root_source_revision,
            "root_source_sha256": head.root_source_sha256,
        }
        return replace(
            base,
            revision=_CorrectionProjectionUnion._display_head_revision(
                "correction-display",
                base.revision,
                corrected.revision,
            ),
            kind="captured-image",
            media_type=corrected.media_type,
            content_sha256=corrected.content_sha256,
            dimensions=corrected.dimensions,
            source=corrected.source,
            resource_state=ResourceState.AVAILABLE,
            resource=corrected.resource,
            freshness=base.freshness,
            provenance=corrected.provenance,
            extensions=extensions,
        )

    @staticmethod
    def _display_head_revision(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
        return f"{prefix}:{digest}"

    @staticmethod
    def _with_extraction_links(
        annotation: SpatialAnnotationView,
        links: Sequence[Any],
    ) -> SpatialAnnotationView:
        if not links:
            return annotation
        ordered = tuple(
            sorted(
                links,
                key=lambda value: (
                    value.operation_id,
                    value.artifact_id,
                ),
            )
        )

        artifact_ids = tuple(
            dict.fromkeys(
                (
                    *annotation.linked_artifact_ids,
                    *(value.artifact_id for value in ordered),
                )
            )
        )
        if len(artifact_ids) > 64:
            raise RepositoryError(
                "a correction extraction exceeds the annotation link limit",
                code="invalid_correction_transform_storage",
                details={"annotation_id": annotation.key.annotation_id},
            )

        extensions = dict(annotation.extensions)
        existing = extensions.get("correction_extraction")
        existing_ids: tuple[str, ...] = ()
        if existing is not None:
            if not isinstance(existing, Mapping):
                raise RepositoryError(
                    "a correction extraction marker is invalid",
                    code="invalid_correction_transform_storage",
                    details={"annotation_id": annotation.key.annotation_id},
                )
            raw_ids = existing.get("artifact_ids")
            if (
                isinstance(raw_ids, (str, bytes))
                or not isinstance(raw_ids, Sequence)
                or any(not isinstance(value, str) for value in raw_ids)
            ):
                raise RepositoryError(
                    "a correction extraction marker is invalid",
                    code="invalid_correction_transform_storage",
                    details={"annotation_id": annotation.key.annotation_id},
                )
            existing_ids = tuple(raw_ids)
        extraction_ids = tuple(
            dict.fromkeys(
                (
                    *existing_ids,
                    *(value.artifact_id for value in ordered),
                )
            )
        )
        if len(extraction_ids) > 64:
            raise RepositoryError(
                "a correction extraction marker exceeds its link limit",
                code="invalid_correction_transform_storage",
                details={"annotation_id": annotation.key.annotation_id},
            )
        extensions["correction_extraction"] = {
            "artifact_ids": list(extraction_ids),
        }
        revision_parts = tuple(
            "\0".join(
                (
                    value.operation_id,
                    value.annotation_revision,
                    value.artifact_id,
                    value.artifact_revision,
                )
            )
            for value in ordered
        )
        return replace(
            annotation,
            revision=_CorrectionProjectionUnion._display_head_revision(
                "correction-extraction",
                annotation.revision,
                *revision_parts,
            ),
            linked_artifact_ids=artifact_ids,
            extensions=extensions,
        )

    def get_spatial_annotation(
        self,
        key: SpatialAnnotationKey,
    ) -> SpatialAnnotationView | None:
        if not isinstance(key, SpatialAnnotationKey):
            raise TypeError("key must be a SpatialAnnotationKey")
        return next(
            (
                value
                for value in self.list_spatial_annotations(key.item_id)
                if value.key == key
            ),
            None,
        )


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
    corrections_write_set: RecoverableWriteSet | None = None


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
    processing_presets: ProcessingPresetService | None = None
    provider_discovery: ProviderDiscoveryService | None = None
    correction_commands: CorrectionService | None = None
    correction_transforms: CorrectionTransformService | None = None
    correction_ocr_proposals: CorrectionOcrProposalQueryService | None = None
    correction_ocr_proposal_catalog: (
        CorrectionOcrProposalCatalogService | None
    ) = None
    correction_reocr: CorrectionReocrService | None = None
    document_artifacts: DocumentArtifactCatalogService | None = None
    document_resources: DocumentResourcePageService | None = None
    raster_artifacts: RasterArtifactProjectorPort | None = None
    spatial_annotations: SpatialAnnotationProjectorPort | None = None

    def __post_init__(self) -> None:
        if (self.canvas_query is None) != (self.canvas_preparation is None):
            raise ValueError(
                "canvas query and preparation services must be installed together"
            )
        if (self.raster_artifacts is None) != (
            self.spatial_annotations is None
        ):
            raise ValueError(
                "raster artifact and spatial annotation projectors must be "
                "installed together"
            )
        if (self.document_artifacts is None) != (
            self.document_resources is None
        ):
            raise ValueError(
                "document artifact and resource services must be installed "
                "together"
            )
        if self.secret_store is not None and not isinstance(
            self.secret_store,
            SecretStoreService,
        ):
            raise TypeError("secret_store must be a SecretStoreService or None")
        if self.processing_presets is not None and not isinstance(
            self.processing_presets,
            ProcessingPresetService,
        ):
            raise TypeError(
                "processing_presets must be a ProcessingPresetService or None"
            )
        if self.provider_discovery is not None and not isinstance(
            self.provider_discovery,
            ProviderDiscoveryService,
        ):
            raise TypeError(
                "provider_discovery must be a ProviderDiscoveryService or None"
            )
        if self.correction_commands is not None and not isinstance(
            self.correction_commands,
            CorrectionService,
        ):
            raise TypeError("correction_commands must be a CorrectionService or None")
        if self.correction_transforms is not None and not isinstance(
            self.correction_transforms,
            CorrectionTransformService,
        ):
            raise TypeError(
                "correction_transforms must be a "
                "CorrectionTransformService or None"
            )
        if (self.correction_transforms is None) != (
            self.correction_ocr_proposals is None
        ):
            raise ValueError(
                "correction transform and OCR proposal query services must "
                "be installed together"
            )
        if self.correction_ocr_proposals is not None and not isinstance(
            self.correction_ocr_proposals,
            CorrectionOcrProposalQueryService,
        ):
            raise TypeError(
                "correction_ocr_proposals must be a "
                "CorrectionOcrProposalQueryService or None"
            )
        if (self.correction_ocr_proposals is None) != (
            self.correction_ocr_proposal_catalog is None
        ):
            raise ValueError(
                "OCR proposal query and catalog services must be installed "
                "together"
            )
        if self.correction_ocr_proposal_catalog is not None and not isinstance(
            self.correction_ocr_proposal_catalog,
            CorrectionOcrProposalCatalogService,
        ):
            raise TypeError(
                "correction_ocr_proposal_catalog must be a "
                "CorrectionOcrProposalCatalogService or None"
            )
        if self.correction_reocr is not None and not isinstance(
            self.correction_reocr,
            CorrectionReocrService,
        ):
            raise TypeError(
                "correction_reocr must be a CorrectionReocrService or None"
            )
        if (
            self.correction_reocr is not None
            and self.correction_ocr_proposals is None
        ):
            raise ValueError(
                "standalone re-OCR requires the OCR proposal services"
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
            (PROCESSING_PRESET_SERVICE, self.processing_presets),
            (PROVIDER_DISCOVERY_SERVICE, self.provider_discovery),
            (CORRECTION_CAPTION_SERVICE, self.correction_commands),
            (CORRECTION_METADATA_SERVICE, self.correction_commands),
            (CORRECTION_REVIEW_SERVICE, self.correction_commands),
            (CORRECTION_SERVICE, self.correction_commands),
            (
                CORRECTION_OCR_PROPOSAL_QUERY_SERVICE,
                self.correction_ocr_proposals,
            ),
            (
                CORRECTION_OCR_PROPOSAL_CATALOG_SERVICE,
                self.correction_ocr_proposal_catalog,
            ),
            (CORRECTION_REOCR_SERVICE, self.correction_reocr),
            (CORRECTION_TRANSFORM_SERVICE, self.correction_transforms),
            (
                DOCUMENT_ARTIFACT_CATALOG_SERVICE,
                self.document_artifacts,
            ),
            (
                DOCUMENT_RESOURCE_PAGE_SERVICE,
                self.document_resources,
            ),
            (RASTER_ARTIFACT_QUERY_SERVICE, self.raster_artifacts),
            (
                SPATIAL_ANNOTATION_QUERY_SERVICE,
                self.spatial_annotations,
            ),
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
    corrections: CorrectionsBindings | None = None,
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
    if corrections is not None and not isinstance(
        corrections,
        CorrectionsBindings,
    ):
        raise TypeError(
            "corrections must be a CorrectionsBindings bundle or None"
        )
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

    corrections_lock = (
        _ReentrantContextFactory(corrections.lock_context_for)
        if corrections is not None
        else None
    )
    text_layer_lock = (
        corrections_lock
        if corrections_lock is not None
        else _ReentrantContextFactory(
            lambda: resources.workspace_lock_context_for("")
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
                # Corrections source reloads query this repository while its
                # authority lock is already held. Share the exact reentrant
                # wrapper when both aggregates are installed so a host lock
                # implemented with ``threading.Lock`` is not reacquired.
                lock_context_for=text_layer_lock,
                # Startup recovery is owned by the process host. Repeating it
                # here would introduce a second, narrower lock domain.
                recover=False,
            )
        )

    corrections_artifacts = None
    capture_document_artifacts = None
    capture_document_resources = None
    correction_commands = None
    correction_transforms = None
    correction_ocr_proposals = None
    correction_ocr_proposal_catalog = None
    correction_reocr = None
    if corrections is not None:
        assert corrections_lock is not None
        corrections_entry_directory_for = (
            corrections.entry_directory_for
            if corrections.entry_directory_for is not None
            else entry_directory_for
        )
        corrections_base = FilesystemCorrectionsArtifactRepository(
            resources.write_set,
            item_exists=corrections.item_exists_for,
            capture_id_for=corrections.capture_id_for,
            entry_directory_for=corrections_entry_directory_for,
            capture_directory_for=corrections.capture_directory_for,
            capture_authority_root=corrections.capture_authority_root,
            representation_revision_for=(
                corrections.representation_revision_for
            ),
            lock_context_for=corrections_lock,
        )
        capture_document_repository = (
            FilesystemCaptureDocumentArtifactRepository(
                resources.write_set,
                item_exists_for=corrections.item_exists_for,
                capture_id_for=corrections.capture_id_for,
                lock_context_for=corrections_lock,
            )
        )
        capture_document_artifacts = DocumentArtifactCatalogService(
            capture_document_repository
        )
        capture_document_resources = DocumentResourcePageService(
            capture_document_repository
        )
        correction_source_reader: (
            FilesystemCorrectionSourceSnapshotReader | None
        ) = None
        correction_projection_service: CorrectionProjectionService | None = None

        def capture_display_revision_for_publication(
            item_id: str,
            capture_id: str,
            artifact_id: str,
            manifest: Mapping[str, Any],
            content: bytes,
        ) -> str:
            if correction_projection_service is None:
                raise RepositoryError(
                    "the correction projection service is not bound",
                    code="correction_projection_authority_unavailable",
                    retryable=True,
                )
            value, annotations = (
                corrections_base.capture_display_projection_for_publication(
                    item_id,
                    capture_id,
                    artifact_id,
                    manifest,
                    content,
                )
            )
            return correction_projection_service.raster_revision_for_publication(
                value,
                annotations,
            )

        def correction_source_snapshot_for(
            key: RasterArtifactKey,
        ):
            if correction_source_reader is None:
                raise RepositoryError(
                    "the correction source reader is not bound",
                    code="correction_transform_authority_unavailable",
                    retryable=True,
                )
            return correction_source_reader(key)

        def correction_artifact_for(key: RasterArtifactKey):
            if correction_projection_service is None:
                return corrections_base.get_raster_artifact(key)
            return correction_projection_service.get_raster_artifact(key)

        capture_asset_lifecycle = None
        original_backups = None
        transform_write_set = resources.write_set
        if corrections.transaction_root is not None:
            transform_write_set = resources.corrections_write_set  # type: ignore[assignment]
            if not isinstance(transform_write_set, RecoverableWriteSet):
                raise ValueError(
                    "corrections_write_set is required for original backups"
                )
            if transform_write_set.root != corrections.transaction_root.resolve():
                raise ValueError(
                    "corrections_write_set must use the configured transaction_root"
                )
            try:
                resources.write_set.root.relative_to(transform_write_set.root)
            except ValueError as exc:
                raise ValueError(
                    "the engine workspace must be below transaction_root"
                ) from exc
            assert corrections.original_backup_root is not None
            original_backups = FilesystemCaptureOriginalBackupStore(
                transform_write_set,
                coordination_write_set=resources.write_set,
                storage_root=resources.write_set.root,
                capture_authority_root=corrections.capture_authority_root,
                backup_root=corrections.original_backup_root,
                capture_id_for=corrections.capture_id_for,
                capture_directory_for=corrections.capture_directory_for,
                artifact_for=correction_artifact_for,
                artifact_revision_for_publication=(
                    capture_display_revision_for_publication
                ),
                lock_context_for=corrections_lock,
                item_updated_at_publication_for=(
                    corrections.item_updated_at_publication_for
                ),
            )
            # Membership changes must advance the enclosing item timestamp.
            # Older read/backup-only hosts may omit that mutation authority;
            # they keep Corrections available but do not expose lifecycle
            # commands.
            if corrections.item_updated_at_publication_for is not None:
                capture_asset_lifecycle = FilesystemCaptureAssetLifecycleStore(
                    transform_write_set,
                    coordination_write_set=resources.write_set,
                    storage_root=resources.write_set.root,
                    capture_authority_root=corrections.capture_authority_root,
                    capture_id_for=corrections.capture_id_for,
                    capture_directory_for=corrections.capture_directory_for,
                    artifact_for=correction_artifact_for,
                    lock_context_for=corrections_lock,
                    item_updated_at_publication_for=(
                        corrections.item_updated_at_publication_for
                    ),
                )

        correction_transform_store = FilesystemCorrectionTransformStore(
            transform_write_set,
            source_snapshot_for=correction_source_snapshot_for,
            lock_context_for=corrections_lock,
            storage_root=resources.write_set.root,
            coordination_write_set=resources.write_set,
            publication_plan_for=(
                original_backups.plan_transform_publication
                if original_backups is not None
                else None
            ),
            recover=False,
        )
        correction_projection = _CorrectionProjectionUnion(
            corrections_base,
            correction_transform_store,
            write_set=resources.write_set,
            lock_context_for=corrections_lock,
            capture_asset_lifecycle=capture_asset_lifecycle,
            original_backups=original_backups,
        )
        aggregate_projector = CorrectionAggregateProjector(
            correction_projection,
            correction_projection,
        )
        correction_repository = FilesystemCorrectionRepository(
            resources.write_set,
            load_aggregate=aggregate_projector.project,
            reconcile_aggregate=reconcile_correction_aggregates,
            lock_context_for=corrections_lock,
            recover=False,
        )
        correction_commands = CorrectionService(correction_repository)
        corrections_artifacts = CorrectionProjectionService(
            correction_projection,
            correction_projection,
            correction_repository,
        )
        correction_projection_service = corrections_artifacts
        correction_source_reader = FilesystemCorrectionSourceSnapshotReader(
            corrections_artifacts,
            corrections_artifacts,
            corrections_artifacts,
            human_text_assertions_for=(
                CanonicalTextLayerHumanAssertionReader(
                    native_text_layers,
                    text_layer_item_id_for=(
                        corrections.text_layer_item_id_for
                    ),
                )
                if native_text_layers is not None
                else None
            ),
        )
        def correction_ocr_source_bytes_for(
            item_id: str,
            operation_id: str,
            output,
        ) -> bytes | None:
            resolution_operation = operation_id
            if operation_id.startswith(CORRECTION_REOCR_OPERATION_PREFIX):
                # A standalone re-OCR operation names a fresh identity while
                # the committed bytes still live under the owning transform
                # publication; recover that operation from the projection pin.
                resolution_operation = next(
                    (
                        pin["operation_id"]
                        for view in (
                            correction_transform_store.list_raster_artifacts(
                                item_id
                            )
                        )
                        if view.key.artifact_id.casefold()
                        == output.artifact_id.casefold()
                        for pin in (
                            view.extensions.get("correction_transform"),
                        )
                        if isinstance(pin, Mapping)
                        and isinstance(pin.get("operation_id"), str)
                    ),
                    "",
                )
                if not resolution_operation:
                    return None
            resolved = correction_transform_store.resolve_committed_output(
                item_id,
                resolution_operation,
                output,
            )
            if resolved is None:
                return None
            try:
                content = resolved.stream.read(
                    CORRECTION_OCR_MAX_SOURCE_BYTES + 1
                )
                if len(content) > CORRECTION_OCR_MAX_SOURCE_BYTES:
                    raise RepositoryError(
                        "the corrected OCR rendition exceeds its size budget",
                        code="correction_ocr_source_too_large",
                    )
                return content
            finally:
                resolved.stream.close()

        correction_ocr_repository = FilesystemCorrectionOcrProposalRepository(
            resources.write_set,
            source_bytes_for=correction_ocr_source_bytes_for,
            lock_context_for=corrections_lock,
            recover=False,
        )
        correction_ocr_proposals = CorrectionOcrProposalQueryService(
            correction_ocr_repository
        )
        correction_ocr_proposal_catalog = CorrectionOcrProposalCatalogService(
            correction_ocr_repository
        )
        correction_ocr = None
        if corrections.ocr_provider is not None:
            correction_ocr = CorrectionOcrFollowupService(
                resources.jobs,
                correction_ocr_repository,
                corrections.ocr_provider,
            )
            correction_reocr = CorrectionReocrService(
                correction_ocr,
                correction_projection,
            )
        correction_transform_worker = CorrectionTransformWorker(
            resources.jobs,
            correction_transform_store,
            ocr=correction_ocr,
        )
        correction_transforms = CorrectionTransformService(
            resources.jobs,
            executor=correction_transform_worker.run,
            start_guard_for=corrections.job_start_context_for,
            committed_transforms=correction_transform_store,
            ocr_outcomes=correction_ocr,
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

    secret_store = (
        None
        if secrets is None
        else SecretStoreService(secrets.repository)
    )

    # Presets are workspace-scoped user configuration with a fixed location, so
    # unlike the catalogue and entries they need no host-supplied path. They
    # still go through the shared containment gate and overlap checks: a
    # workspace path is never trusted just because composition chose it.
    processing_presets_path = resolve_workspace_path(
        resources.write_set.root,
        Path(PROCESSING_PRESET_RELATIVE),
        artifact="processing_presets",
        directory=False,
    )
    for other_path, other_artifact in (
        (catalogue_path, "catalogue"),
        (entries_path, "entries"),
    ):
        if workspace_paths_overlap(processing_presets_path, other_path):
            raise RepositoryError(
                "the processing presets and "
                f"{other_artifact} locations cannot overlap",
                code="unsafe_filesystem_engine_path",
                details={"artifact": "processing_presets"},
            )
    processing_presets = ProcessingPresetService(
        FilesystemProcessingPresetStore(processing_presets_path)
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
        processing_presets=processing_presets,
        provider_discovery=(None if providers is None else providers.service),
        correction_commands=correction_commands,
        correction_transforms=correction_transforms,
        correction_ocr_proposals=correction_ocr_proposals,
        correction_ocr_proposal_catalog=correction_ocr_proposal_catalog,
        correction_reocr=correction_reocr,
        document_artifacts=capture_document_artifacts,
        document_resources=capture_document_resources,
        raster_artifacts=corrections_artifacts,
        spatial_annotations=corrections_artifacts,
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
    "CorrectionsBindings",
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
