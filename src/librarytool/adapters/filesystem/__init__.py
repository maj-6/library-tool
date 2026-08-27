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
from .book_review_import import FilesystemBookReviewImportAdapter
from .capture_archive_repository import FilesystemCaptureArchiveRepository
from .capture_document_artifact_repository import (
    FilesystemCaptureDocumentArtifactRepository,
)
from .capture_asset_lifecycle import FilesystemCaptureAssetLifecycleStore
from .capture_original_backups import FilesystemCaptureOriginalBackupStore
from .correction_repository import FilesystemCorrectionRepository
from .correction_ocr_proposal_repository import (
    FilesystemCorrectionOcrProposalRepository,
)
from .correction_transform_store import (
    CorrectionTransformPublicationPlan,
    CorrectionTransformOutputResolverPort,
    FilesystemCorrectionTransformStore,
)
from .correction_source_snapshot import (
    CanonicalTextLayerHumanAssertionReader,
    FilesystemCorrectionSourceSnapshotReader,
)
from .corrections_artifact_repository import (
    FilesystemCorrectionsArtifactRepository,
    FilesystemRasterResourceResolverPort,
    ResolvedRasterResource,
)
from .job_history import FilesystemJobHistoryRepository
from .processing_preset_store import (
    PROCESSING_PRESET_RELATIVE,
    FilesystemProcessingPresetStore,
)
from .item_command_repository import FilesystemItemCommandRepository
from .item_lifecycle_repository import (
    EMPTY_MANAGED_TREE_REVISION,
    FilesystemItemLifecycleRepository,
    FilesystemItemLifecycleReservationRepository,
)
from .item_repository import FilesystemItemQueryRepository
from .interchange_repository import FilesystemInterchangeRepository
from .lib_open_repository import FilesystemOpenLibRepository
from .manual_entry_item_codec import ManualEntryItemCodec
from .recoverable_write_set import (
    RecoverableWriteSet,
    RecoveryResult,
    RecoveryRequiredError,
    UnsafeTargetError,
    WriteSetError,
)
from .scan_assessment_repository import (
    SCAN_ASSESSMENT_MANIFEST_NAME,
    SCAN_ASSESSMENT_RELATIVE_ROOT,
    SCAN_ASSESSMENT_TEXT_NAME,
    FilesystemScanAssessmentRepository,
)
from .portable_book_bundle import (
    FilesystemPortableBookBundleService,
    PortableBookBundleZipCodec,
    ResolvedManualBookAuthority,
    catalogue_source_evidence,
    catalogue_source_sha256,
    resolve_manual_book_authority,
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
    "CanonicalTextLayerHumanAssertionReader",
    "EMPTY_MANAGED_TREE_REVISION",
    "FilesystemAttachedPdfAssetSnapshot",
    "FilesystemAttachedPdfInspector",
    "FilesystemBookReviewImportAdapter",
    "FilesystemCanvasEvidence",
    "FilesystemCanvasInspection",
    "FilesystemCanvasObservation",
    "FilesystemCanvasPreparationRepository",
    "FilesystemCanvasQueryRepository",
    "FilesystemCaptureArchiveRepository",
    "FilesystemCaptureAssetLifecycleStore",
    "FilesystemCaptureDocumentArtifactRepository",
    "FilesystemCaptureOriginalBackupStore",
    "FilesystemCorrectionRepository",
    "FilesystemCorrectionOcrProposalRepository",
    "FilesystemCorrectionTransformStore",
    "CorrectionTransformPublicationPlan",
    "CorrectionTransformOutputResolverPort",
    "FilesystemCorrectionSourceSnapshotReader",
    "FilesystemCorrectionsArtifactRepository",
    "FilesystemRasterResourceResolverPort",
    "FilesystemItemCommandRepository",
    "FilesystemItemLifecycleRepository",
    "FilesystemItemLifecycleReservationRepository",
    "FilesystemItemQueryRepository",
    "FilesystemInterchangeRepository",
    "FilesystemOpenLibRepository",
    "FilesystemJobHistoryRepository",
    "FilesystemProcessingPresetStore",
    "PROCESSING_PRESET_RELATIVE",
    "ManualEntryItemCodec",
    "FilesystemReplicaRepository",
    "FilesystemRepresentationCommandRepository",
    "FilesystemScanAssessmentRepository",
    "FilesystemPortableBookBundleService",
    "FilesystemTranslationRepository",
    "FilesystemTextLayerAggregateRepository",
    "RecoverableWriteSet",
    "SCAN_ASSESSMENT_MANIFEST_NAME",
    "SCAN_ASSESSMENT_RELATIVE_ROOT",
    "SCAN_ASSESSMENT_TEXT_NAME",
    "PortableBookBundleZipCodec",
    "ResolvedManualBookAuthority",
    "catalogue_source_evidence",
    "catalogue_source_sha256",
    "resolve_manual_book_authority",
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
