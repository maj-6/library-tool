"""Filesystem-backed engine adapters."""

from .attached_pdf_inspector import (
    ATTACHED_PDF_PARSER_ISOLATION,
    ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE,
    AttachedPdfAssetLookup,
    FilesystemAttachedPdfAssetSnapshot,
    FilesystemAttachedPdfInspector,
)
from .canvas_preparation_repository import (
    FilesystemCanvasEvidence,
    FilesystemCanvasInspection,
    FilesystemCanvasObservation,
    FilesystemCanvasPreparationRepository,
)
from .canvas_query_repository import FilesystemCanvasQueryRepository
from .correction_repository import FilesystemCorrectionRepository
from .correction_transform_store import FilesystemCorrectionTransformStore
from .corrections_artifact_repository import (
    FilesystemCorrectionsArtifactRepository,
    FilesystemRasterResourceResolverPort,
    ResolvedRasterResource,
)
from .job_history import FilesystemJobHistoryRepository
from .item_command_repository import FilesystemItemCommandRepository
from .item_lifecycle_repository import (
    EMPTY_MANAGED_TREE_REVISION,
    FilesystemItemLifecycleRepository,
    FilesystemItemLifecycleReservationRepository,
)
from .item_repository import FilesystemItemQueryRepository
from .interchange_repository import FilesystemInterchangeRepository
from .lib_open_repository import FilesystemOpenLibRepository
from .recoverable_write_set import (
    RecoverableWriteSet,
    RecoveryResult,
    RecoveryRequiredError,
    UnsafeTargetError,
    WriteSetError,
)
from .representation_command_repository import (
    FilesystemRepresentationCommandRepository,
)
from .replica_repository import FilesystemReplicaRepository
from .session_lease import (
    WorkspaceAlreadyOpenError,
    WorkspaceSessionError,
    WorkspaceSessionLease,
)
from .translation_repository import (
    FilesystemTranslationRepository,
    translation_id_for_language,
)
from .text_layer_aggregate_repository import (
    FilesystemTextLayerAggregateRepository,
)
from .whl_catalogue_codec import WhlCatalogueItemCodec

__all__ = [
    "ATTACHED_PDF_PARSER_ISOLATION",
    "ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE",
    "AttachedPdfAssetLookup",
    "EMPTY_MANAGED_TREE_REVISION",
    "FilesystemAttachedPdfAssetSnapshot",
    "FilesystemAttachedPdfInspector",
    "FilesystemCanvasEvidence",
    "FilesystemCanvasInspection",
    "FilesystemCanvasObservation",
    "FilesystemCanvasPreparationRepository",
    "FilesystemCanvasQueryRepository",
    "FilesystemCorrectionRepository",
    "FilesystemCorrectionTransformStore",
    "FilesystemCorrectionsArtifactRepository",
    "FilesystemRasterResourceResolverPort",
    "FilesystemItemCommandRepository",
    "FilesystemItemLifecycleRepository",
    "FilesystemItemLifecycleReservationRepository",
    "FilesystemItemQueryRepository",
    "FilesystemInterchangeRepository",
    "FilesystemOpenLibRepository",
    "FilesystemJobHistoryRepository",
    "FilesystemReplicaRepository",
    "FilesystemRepresentationCommandRepository",
    "FilesystemTranslationRepository",
    "FilesystemTextLayerAggregateRepository",
    "RecoverableWriteSet",
    "ResolvedRasterResource",
    "RecoveryResult",
    "RecoveryRequiredError",
    "UnsafeTargetError",
    "WriteSetError",
    "WorkspaceAlreadyOpenError",
    "WorkspaceSessionError",
    "WorkspaceSessionLease",
    "WhlCatalogueItemCodec",
    "translation_id_for_language",
]
