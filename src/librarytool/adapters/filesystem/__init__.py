"""Filesystem-backed engine adapters."""

from .canvas_query_repository import FilesystemCanvasQueryRepository
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

__all__ = [
    "EMPTY_MANAGED_TREE_REVISION",
    "FilesystemCanvasQueryRepository",
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
    "RecoverableWriteSet",
    "RecoveryResult",
    "RecoveryRequiredError",
    "UnsafeTargetError",
    "WriteSetError",
    "WorkspaceAlreadyOpenError",
    "WorkspaceSessionError",
    "WorkspaceSessionLease",
    "translation_id_for_language",
]
