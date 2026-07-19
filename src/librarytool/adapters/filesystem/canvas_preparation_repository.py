"""Recoverable filesystem adapter for explicit canvas preparation.

The public query index remains beneath the managed item tree at
``.librarytool/canvases.json``.  Its source members are private adapter data
and are stripped by :mod:`canvas_query_repository`.  A second item-local file
retains the monotonic source-correlation ledger, including retired identities,
and a third binds those random correlations to exact asset bytes and private,
versioned source evidence.  Operation receipts live in the write-set's private
``.engine`` tree.  Evidence, paths, and positions are never identity: only a
persisted random correlation may carry a canvas ID across preparations.

Nothing is published while media is inspected or a preparation is staged.
``commit`` places the materialization, ledger, index, and durable receipt in one
:class:`RecoverableWriteSet` transaction.  Units hold the workspace lease and
the injected broad host lock for their entire lifetime, including receipt
replay, live-state lookup, local inspection, and publication.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.canvas_commands import (
    CanvasPreparationItemSnapshot,
    CanvasPreparationReceipt,
    CanvasPreparationRepresentationSnapshot,
    CanvasPreparationSequenceSummary,
    CanvasPreparationSnapshot,
    CanvasSourceIdentityBinding,
)
from ...engine.canvases import (
    CanvasExtent,
    CanvasKey,
    CanvasQueryService,
    CanvasSequenceView,
    CanvasView,
)
from ...engine.errors import (
    ConflictError,
    EngineError,
    RepositoryError,
    ValidationError,
)
from .canvas_query_repository import (
    CANVAS_INDEX_SCHEMA,
    CANVAS_INDEX_VERSION,
    FilesystemCanvasQueryRepository,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    RecoverableWriteTransaction,
    WriteSetError,
    _is_redirecting_path,
)


ItemSnapshotLookup: TypeAlias = Callable[[str], CanvasPreparationItemSnapshot | None]
RepresentationSnapshotLookup: TypeAlias = Callable[
    [str, str], CanvasPreparationRepresentationSnapshot | None
]
EntryDirectoryResolver: TypeAlias = Callable[[str], Path]
CanvasIdAllocator: TypeAlias = Callable[[frozenset[str]], str]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]

CANVAS_IDENTITY_LEDGER_SCHEMA = "librarytool.canvas-identity-ledger"
CANVAS_IDENTITY_LEDGER_VERSION = 1
CANVAS_IDENTITY_LEDGER_RELATIVE = PurePosixPath(".librarytool/canvas-identities.json")
CANVAS_SOURCE_MATERIALIZATION_SCHEMA = (
    "librarytool.canvas-source-materializations"
)
CANVAS_SOURCE_MATERIALIZATION_VERSION = 1
CANVAS_SOURCE_MATERIALIZATION_RELATIVE = PurePosixPath(
    ".librarytool/canvas-source-materializations.json"
)
CANVAS_PREPARATION_RECEIPT_SCHEMA = "librarytool.canvas-preparation-receipt"
CANVAS_PREPARATION_RECEIPT_VERSION = 1

_RECEIPT_ROOT = PurePosixPath(".engine/receipts/canvas-preparations")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CORRELATION_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FUZZY_HASH_RE = re.compile(r"^[0-9a-f]{16}$")
_MAX_INDEX_BYTES = 16 * 1024 * 1024
_MAX_LEDGER_BYTES = 16 * 1024 * 1024
_MAX_MATERIALIZATION_BYTES = 64 * 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_CANVASES = 100_000
_CORRELATION_ALLOCATION_ATTEMPTS = 16
_LEDGER_FIELDS = frozenset({"schema", "version", "item_id", "sequences"})
_LEDGER_SEQUENCE_FIELDS = frozenset({"representation_id", "bindings"})
_BINDING_FIELDS = frozenset({"canvas_id", "source_correlation", "active"})
_MATERIALIZATION_FIELDS = frozenset(
    {"schema", "version", "item_id", "sequences"}
)
_MATERIALIZATION_SEQUENCE_FIELDS = frozenset(
    {
        "representation_id",
        "representation_revision",
        "media_type",
        "generation",
        "asset",
        "sources",
    }
)
_MATERIALIZATION_ASSET_FIELDS = frozenset(
    {"sha256", "size", "source_count"}
)
_MATERIALIZATION_SOURCE_FIELDS = frozenset(
    {
        "source_correlation",
        "active",
        "born_generation",
        "last_active_generation",
        "last_retired_generation",
        "last_locator",
        "disposition",
        "evidence",
    }
)
_MATERIALIZATION_LOCATOR_FIELDS = frozenset({"position", "path"})
_MATERIALIZATION_EVIDENCE_FIELDS = frozenset(
    {
        "profile",
        "width_mpt",
        "height_mpt",
        "rotation",
        "strong_sha256",
        "fuzzy_hash",
    }
)
_MATERIALIZATION_DISPOSITIONS = frozenset(
    {
        "minted",
        "unchanged-asset",
        "explicit-map",
        "retired",
        "reactivated",
        "reset",
    }
)
_INDEX_FIELDS = frozenset({"schema", "version", "item_id", "sequences"})
_INDEX_SEQUENCE_FIELDS = frozenset(
    {"representation_id", "representation_revision", "canvases"}
)


@dataclass(frozen=True, slots=True)
class FilesystemCanvasEvidence:
    """Private, versioned matching evidence which is never source identity."""

    profile: str
    width_mpt: int
    height_mpt: int
    rotation: int
    strong_sha256: str = field(repr=False)
    fuzzy_hash: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class FilesystemCanvasObservation:
    """One ordered local source observation with no caller-provided identity.

    The repository mints a random correlation on first materialization.  Page
    fingerprints, positions, and paths are only reconciliation evidence and
    may never be substituted for that persisted random value.
    """

    source_position: int = field(repr=False)
    source_path: str = field(repr=False)
    evidence: FilesystemCanvasEvidence = field(repr=False)
    label: str = ""
    extent: CanvasExtent = field(default_factory=CanvasExtent)
    available: bool = True
    resource_kinds: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FilesystemCanvasInspection:
    """One coherent media inspection bound to exact asset bytes."""

    media_type: str
    asset_sha256: str = field(repr=False)
    asset_size: int
    observations: tuple[FilesystemCanvasObservation, ...]


MediaInspector: TypeAlias = Callable[
    [CanvasPreparationRepresentationSnapshot, Path],
    FilesystemCanvasInspection,
]
SourceCorrelationFactory: TypeAlias = Callable[[frozenset[bytes]], bytes]


@dataclass(frozen=True, slots=True)
class _LedgerSequence:
    representation_id: str
    bindings: tuple[CanvasSourceIdentityBinding, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _IndexedCanvas:
    canvas_id: str
    position: int
    path: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _IndexedSequence:
    representation_id: str
    representation_revision: str
    canvases: tuple[_IndexedCanvas, ...]


@dataclass(frozen=True, slots=True)
class _MaterializedSource:
    source_correlation: bytes = field(repr=False)
    active: bool
    born_generation: int
    last_active_generation: int
    last_retired_generation: int | None
    position: int = field(repr=False)
    path: str = field(repr=False)
    disposition: str
    evidence: FilesystemCanvasEvidence = field(repr=False)


@dataclass(frozen=True, slots=True)
class _MaterializedSequence:
    representation_id: str
    representation_revision: str
    media_type: str
    generation: int
    asset_sha256: str = field(repr=False)
    asset_size: int
    source_count: int
    sources: tuple[_MaterializedSource, ...] = field(repr=False)


def _random_source_correlation(_reserved: frozenset[bytes]) -> bytes:
    return secrets.token_bytes(32)


class _StaticCanvasRecordRepository:
    def __init__(self, record: Mapping[str, Any]) -> None:
        self._record = record

    def get_sequence_record(
        self,
        item_id: str,
        representation_id: str,
    ) -> Mapping[str, Any] | None:
        if (
            self._record.get("item_id") != item_id
            or self._record.get("representation_id") != representation_id
        ):
            return None
        return self._record


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _repository_error(
    message: str,
    *,
    code: str,
    item_id: str = "",
    representation_id: str = "",
    retryable: bool = False,
    **details: Any,
) -> RepositoryError:
    safe: dict[str, Any] = {}
    if item_id:
        safe["item_id"] = item_id
    if representation_id:
        safe["representation_id"] = representation_id
    safe.update(details)
    return RepositoryError(
        message,
        code=code,
        details=safe,
        retryable=retryable,
    )


def _identifier(
    value: Any,
    *,
    field_name: str,
    item_id: str = "",
    representation_id: str = "",
) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _repository_error(
            "a canvas preparation identity is invalid",
            code="invalid_canvas_preparation_identity",
            item_id=item_id,
            representation_id=representation_id,
            field=field_name,
        )
    return value


def _stored_revision(
    value: Any,
    *,
    item_id: str,
    representation_id: str,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(character.isspace() for character in value)
        or any(
            ord(character) == 127
            or ord(character) < 32
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise _repository_error(
            "a canvas source materialization revision is invalid",
            code="invalid_canvas_source_materialization",
            item_id=item_id,
            representation_id=representation_id,
        )
    return value


def _media_type(
    value: Any,
    *,
    item_id: str,
    representation_id: str,
    code: str = "invalid_canvas_source_materialization",
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value != value.strip()
        or "/" not in value
        or any(
            ord(character) < 33
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise _repository_error(
            "a canvas media type is invalid",
            code=code,
            item_id=item_id,
            representation_id=representation_id,
        )
    return value


def _plain_json(
    value: Any,
    *,
    active: set[int] | None = None,
    depth: int = 0,
) -> Any:
    if depth > 64:
        raise ValueError("JSON is nested too deeply")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if any(
            ord(character) == 127
            or (ord(character) < 32 and character not in "\n\r\t")
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        ):
            raise ValueError("JSON contains unsafe text")
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError("JSON contains a non-finite number")
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON contains a reference cycle")
        active.add(identity)
        try:
            result: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or key in result:
                    raise ValueError("JSON contains an invalid object key")
                result[key] = _plain_json(
                    item,
                    active=active,
                    depth=depth + 1,
                )
            return result
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON contains a reference cycle")
        active.add(identity)
        try:
            return [_plain_json(item, active=active, depth=depth + 1) for item in value]
        finally:
            active.remove(identity)
    raise TypeError("JSON contains an unsupported value")


def _json_bytes(value: Any, *, artifact: str) -> bytes:
    try:
        plain = _plain_json(value)
        return (
            json.dumps(
                plain,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _repository_error(
            "a canvas preparation artifact cannot be serialized",
            code="invalid_canvas_preparation_artifact",
            artifact=artifact,
            cause_type=type(exc).__name__,
        ) from exc


def _bounded_payload(
    payload: bytes,
    *,
    maximum_bytes: int,
    artifact: str,
    item_id: str,
    representation_id: str,
) -> bytes:
    if len(payload) > maximum_bytes:
        raise _repository_error(
            "a canvas preparation artifact exceeds its size limit",
            code="canvas_preparation_artifact_too_large",
            item_id=item_id,
            representation_id=representation_id,
            artifact=artifact,
            maximum_bytes=maximum_bytes,
        )
    return payload


def _producer_revision(
    *,
    representation_revision: str,
    canvas_id: str,
    correlation: bytes,
    source_position: int,
    source_path: str,
    public: Mapping[str, Any],
) -> str:
    payload = _json_bytes(
        {
            "schema": "librarytool.canvas-producer-state/1",
            "representation_revision": representation_revision,
            "canvas_id": canvas_id,
            "source_correlation": correlation.hex(),
            "source": {"position": source_position, "path": source_path},
            "public": public,
        },
        artifact="producer_state",
    )
    return f"producer-{hashlib.sha256(payload).hexdigest()}"


def _command_hash(
    *,
    item_id: str,
    representation_id: str,
    representation_revision: str,
) -> str:
    payload = json.dumps(
        {
            "action": "prepare",
            "item_id": item_id,
            "representation_id": representation_id,
            "expected_representation_revision": representation_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class FilesystemCanvasPreparationRepository:
    """Open operation-scoped recoverable canvas preparation units."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_snapshot_for: ItemSnapshotLookup,
        representation_snapshot_for: RepresentationSnapshotLookup,
        entry_directory_for: EntryDirectoryResolver,
        inspect_media: MediaInspector,
        allocate_canvas_id: CanvasIdAllocator,
        lock_context_for: LockContextFactory,
        source_correlation_factory: SourceCorrelationFactory = (
            _random_source_correlation
        ),
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (item_snapshot_for, "item_snapshot_for"),
            (representation_snapshot_for, "representation_snapshot_for"),
            (entry_directory_for, "entry_directory_for"),
            (inspect_media, "inspect_media"),
            (allocate_canvas_id, "allocate_canvas_id"),
            (lock_context_for, "lock_context_for"),
            (source_correlation_factory, "source_correlation_factory"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._item_snapshot_for = item_snapshot_for
        self._representation_snapshot_for = representation_snapshot_for
        self._entry_directory_for = entry_directory_for
        self._inspect_media = inspect_media
        self._allocate_canvas_id = allocate_canvas_id
        self._source_correlation_factory = source_correlation_factory
        self._lock_context_for = lock_context_for
        # Reuse the exact v1 index parser/projector without invoking its public
        # read path (which would acquire locks and live callbacks a second time).
        self._query_repository = FilesystemCanvasQueryRepository(
            write_set,
            item_exists=lambda _item_id: False,
            representation_revision_for=lambda _item_id, _representation_id: None,
            entry_directory_for=entry_directory_for,
            lock_context_for=lock_context_for,
        )
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except Exception as exc:
                raise _repository_error(
                    "the canvas preparation repository could not recover",
                    code="canvas_preparation_recovery_failed",
                    cause_type=type(exc).__name__,
                ) from exc

    @contextmanager
    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> Iterator["FilesystemCanvasPreparationUnitOfWork"]:
        operation = _identifier(operation_id, field_name="operation_id")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    unit = FilesystemCanvasPreparationUnitOfWork(
                        self._write_set,
                        operation_id=operation,
                        item_snapshot_for=self._item_snapshot_for,
                        representation_snapshot_for=(self._representation_snapshot_for),
                        inspect_media=self._inspect_media,
                        allocate_canvas_id=self._allocate_canvas_id,
                        source_correlation_factory=(
                            self._source_correlation_factory
                        ),
                        query_repository=self._query_repository,
                    )
                    try:
                        yield unit
                    finally:
                        unit.close()
        except WriteSetError as exc:
            raise _repository_error(
                "the canvas preparation workspace is unavailable",
                code="canvas_preparation_workspace_unavailable",
                cause_type=type(exc).__name__,
            ) from exc


class FilesystemCanvasPreparationUnitOfWork:
    """One locked live snapshot and memory-only canvas staging buffer."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        item_snapshot_for: ItemSnapshotLookup,
        representation_snapshot_for: RepresentationSnapshotLookup,
        inspect_media: MediaInspector,
        allocate_canvas_id: CanvasIdAllocator,
        source_correlation_factory: SourceCorrelationFactory,
        query_repository: FilesystemCanvasQueryRepository,
    ) -> None:
        self._write_set = write_set
        self._operation_id = operation_id
        self._item_snapshot_for = item_snapshot_for
        self._representation_snapshot_for = representation_snapshot_for
        self._inspect_media = inspect_media
        self._allocate_canvas_id = allocate_canvas_id
        self._source_correlation_factory = source_correlation_factory
        self._query_repository = query_repository
        self._closed = False
        self._committed = False
        self._receipt_checked = False
        self._item: CanvasPreparationItemSnapshot | None = None
        self._representation: CanvasPreparationRepresentationSnapshot | None = None
        self._entry_directory: Path | None = None
        self._index_path: Path | None = None
        self._ledger_path: Path | None = None
        self._materialization_path: Path | None = None
        self._base_index: dict[str, Any] | None = None
        self._base_ledger: dict[str, Any] | None = None
        self._base_materialization: dict[str, Any] | None = None
        self._ledger_sequences: dict[str, _LedgerSequence] = {}
        self._materialization_sequences: dict[str, _MaterializedSequence] = {}
        self._before_loaded = False
        self._before: CanvasPreparationSnapshot | None = None
        self._after: CanvasPreparationSnapshot | None = None
        self._staged_index: bytes | None = None
        self._staged_ledger: bytes | None = None
        self._staged_materialization: bytes | None = None

    def close(self) -> None:
        self._closed = True
        self._staged_index = None
        self._staged_ledger = None
        self._staged_materialization = None

    def receipt(self, operation_id: str) -> CanvasPreparationReceipt | None:
        self._ensure_open()
        if operation_id != self._operation_id:
            raise _repository_error(
                "the receipt request is outside this operation",
                code="receipt_scope_mismatch",
            )
        path = self._receipt_path(operation_id)
        self._receipt_checked = True
        if not self._regular_file_exists(
            path,
            artifact="canvas_preparation_receipt",
            allow_missing=True,
        ):
            return None
        raw = self._read_json(
            path,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            artifact="canvas_preparation_receipt",
        )
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema", "version", "receipt"}
            or raw.get("schema") != CANVAS_PREPARATION_RECEIPT_SCHEMA
            or type(raw.get("version")) is not int
            or raw.get("version") != CANVAS_PREPARATION_RECEIPT_VERSION
        ):
            raise _repository_error(
                "a canvas preparation receipt is invalid",
                code="invalid_canvas_preparation_receipt",
            )
        try:
            receipt = CanvasPreparationReceipt.from_storage_dict(raw["receipt"])
        except (TypeError, ValueError) as exc:
            raise _repository_error(
                "a canvas preparation receipt is invalid",
                code="invalid_canvas_preparation_receipt",
                cause_type=type(exc).__name__,
            ) from exc
        if receipt.operation_id != operation_id:
            raise _repository_error(
                "the stored receipt belongs to another operation",
                code="receipt_scope_mismatch",
            )
        return receipt

    def get_item(
        self,
        item_id: str,
    ) -> CanvasPreparationItemSnapshot | None:
        self._ensure_after_receipt()
        if self._item is not None:
            return self._item
        try:
            value = self._item_snapshot_for(item_id)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the live item state could not be queried",
                code="canvas_preparation_authority_unavailable",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if value is not None and not isinstance(value, CanvasPreparationItemSnapshot):
            raise _repository_error(
                "the live item state is invalid",
                code="invalid_canvas_preparation_authority_snapshot",
                item_id=item_id,
            )
        self._item = value
        return value

    def get_representation(
        self,
        item_id: str,
        representation_id: str,
    ) -> CanvasPreparationRepresentationSnapshot | None:
        self._ensure_after_receipt()
        if self._representation is not None:
            return self._representation
        try:
            value = self._representation_snapshot_for(item_id, representation_id)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the live representation state could not be queried",
                code="canvas_preparation_authority_unavailable",
                item_id=item_id,
                representation_id=representation_id,
                cause_type=type(exc).__name__,
            ) from exc
        if value is not None and not isinstance(
            value, CanvasPreparationRepresentationSnapshot
        ):
            raise _repository_error(
                "the live representation state is invalid",
                code="invalid_canvas_preparation_authority_snapshot",
                item_id=item_id,
                representation_id=representation_id,
            )
        self._representation = value
        return value

    def get_preparation(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> CanvasPreparationSnapshot | None:
        self._ensure_open()
        if representation != self._representation:
            raise _repository_error(
                "the preparation request is outside the locked snapshot",
                code="canvas_preparation_repository_scope_mismatch",
            )
        if self._before_loaded:
            return self._before
        (
            entry_directory,
            index_path,
            ledger_path,
            materialization_path,
        ) = self._artifact_paths(
            representation.item_id
        )
        self._entry_directory = entry_directory
        self._index_path = index_path
        self._ledger_path = ledger_path
        self._materialization_path = materialization_path
        index_exists = self._query_repository._index_exists(
            index_path,
            item_id=representation.item_id,
        )
        ledger_exists = self._regular_file_exists(
            ledger_path,
            artifact="canvas_identity_ledger",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
            allow_missing=True,
        )
        materialization_exists = self._regular_file_exists(
            materialization_path,
            artifact="canvas_source_materialization",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
            allow_missing=True,
        )
        if index_exists and ledger_exists and not materialization_exists:
            raise _repository_error(
                "legacy canvas identities require an explicit migration",
                code="canvas_source_materialization_required",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
            )
        if len({index_exists, ledger_exists, materialization_exists}) != 1:
            raise _repository_error(
                "the canvas preparation artifacts are incomplete",
                code="canvas_preparation_artifact_mismatch",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
            )
        if not index_exists:
            self._base_index = self._empty_index(representation.item_id)
            self._base_ledger = self._empty_ledger(representation.item_id)
            self._base_materialization = self._empty_materialization(
                representation.item_id
            )
            self._before_loaded = True
            self._before = None
            return None

        raw_index = self._query_repository._read_index(
            index_path,
            item_id=representation.item_id,
        )
        raw_ledger = self._read_json(
            ledger_path,
            maximum_bytes=_MAX_LEDGER_BYTES,
            artifact="canvas_identity_ledger",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )
        raw_materialization = self._read_json(
            materialization_path,
            maximum_bytes=_MAX_MATERIALIZATION_BYTES,
            artifact="canvas_source_materialization",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )
        public, index_sequences = self._validated_index(
            raw_index,
            representation=representation,
            entry_directory=entry_directory,
        )
        ledger, ledger_sequences = self._validated_ledger(
            raw_ledger,
            item_id=representation.item_id,
        )
        materialization, materialization_sequences = (
            self._validated_materialization(
                raw_materialization,
                item_id=representation.item_id,
                entry_directory=entry_directory,
            )
        )
        self._validate_artifact_alignment(
            index_sequences,
            ledger_sequences,
            materialization_sequences,
            item_id=representation.item_id,
        )
        self._base_index = raw_index
        self._base_ledger = ledger
        self._base_materialization = materialization
        self._ledger_sequences = ledger_sequences
        self._materialization_sequences = materialization_sequences
        target_ledger = ledger_sequences.get(representation.representation_id)
        target_materialization = materialization_sequences.get(
            representation.representation_id
        )
        if not (
            (public is None)
            == (target_ledger is None)
            == (target_materialization is None)
        ):
            raise _repository_error(
                "the canvas preparation artifacts disagree",
                code="canvas_preparation_artifact_mismatch",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
            )
        if public is None:
            self._before = None
        else:
            assert target_ledger is not None
            self._before = CanvasPreparationSnapshot(
                sequence=self._sequence_view(public),
                identities=target_ledger.bindings,
            )
        self._before_loaded = True
        return self._before

    def stage_prepare(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
        before: CanvasPreparationSnapshot | None,
    ) -> CanvasPreparationSnapshot:
        self._ensure_open()
        if not self._before_loaded or before != self._before:
            raise _repository_error(
                "the preparation is outside the locked source snapshot",
                code="canvas_preparation_repository_scope_mismatch",
            )
        if representation != self._representation:
            raise _repository_error(
                "the representation is outside the locked source snapshot",
                code="canvas_preparation_repository_scope_mismatch",
            )
        if self._after is not None:
            raise _repository_error(
                "canvas preparation was already staged",
                code="canvas_preparation_already_staged",
            )
        assert self._entry_directory is not None
        try:
            inspected = self._inspect_media(representation, self._entry_directory)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the local representation could not be inspected",
                code="canvas_media_inspection_failed",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
                cause_type=type(exc).__name__,
            ) from exc
        inspection, observations = self._inspection(
            inspected,
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )
        materialized_sources, correlation_by_position, generation = (
            self._materialized_sources(
                inspection,
                observations,
                representation=representation,
            )
        )
        correlations = tuple(
            correlation_by_position[observation.source_position]
            for observation in observations
        )
        bindings, assigned = self._assign_identities(
            correlations,
            representation=representation,
        )
        canvas_records = [
            self._canvas_record(
                observation,
                correlation=correlation_by_position[
                    observation.source_position
                ],
                canvas_id=assigned[
                    correlation_by_position[observation.source_position]
                ],
                order=order,
                representation=representation,
            )
            for order, observation in enumerate(observations)
        ]
        sequence_record = {
            "representation_id": representation.representation_id,
            "representation_revision": representation.revision,
            "canvases": canvas_records,
        }
        index = self._updated_index(sequence_record, representation=representation)
        ledger = self._updated_ledger(bindings, representation=representation)
        materialization = self._updated_materialization(
            inspection,
            materialized_sources,
            generation=generation,
            representation=representation,
        )

        public, index_sequences = self._validated_index(
            index,
            representation=representation,
            entry_directory=self._entry_directory,
        )
        assert public is not None
        validated_ledger, ledger_sequences = self._validated_ledger(
            ledger,
            item_id=representation.item_id,
        )
        validated_materialization, materialization_sequences = (
            self._validated_materialization(
                materialization,
                item_id=representation.item_id,
                entry_directory=self._entry_directory,
            )
        )
        self._validate_artifact_alignment(
            index_sequences,
            ledger_sequences,
            materialization_sequences,
            item_id=representation.item_id,
        )
        target = ledger_sequences[representation.representation_id]
        try:
            after = CanvasPreparationSnapshot(
                sequence=self._sequence_view(public),
                identities=target.bindings,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise _repository_error(
                "the staged canvas preparation is invalid",
                code="invalid_canvas_preparation_snapshot",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
                cause_type=type(exc).__name__,
            ) from exc
        self._staged_index = _bounded_payload(
            _json_bytes(index, artifact="canvas_index"),
            maximum_bytes=_MAX_INDEX_BYTES,
            artifact="canvas_index",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )
        self._staged_ledger = _bounded_payload(
            _json_bytes(validated_ledger, artifact="canvas_identity_ledger"),
            maximum_bytes=_MAX_LEDGER_BYTES,
            artifact="canvas_identity_ledger",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )
        self._staged_materialization = _bounded_payload(
            _json_bytes(
                validated_materialization,
                artifact="canvas_source_materialization",
            ),
            maximum_bytes=_MAX_MATERIALIZATION_BYTES,
            artifact="canvas_source_materialization",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )
        self._after = after
        return after

    def commit(self, receipt: CanvasPreparationReceipt) -> None:
        self._ensure_open()
        if self._committed:
            raise _repository_error(
                "the canvas preparation is already committed",
                code="canvas_preparation_unit_committed",
            )
        if (
            self._after is None
            or self._staged_index is None
            or self._staged_ledger is None
            or self._staged_materialization is None
            or self._representation is None
            or self._index_path is None
            or self._ledger_path is None
            or self._materialization_path is None
        ):
            raise _repository_error(
                "no canvas preparation has been staged",
                code="canvas_preparation_not_staged",
            )
        self._validate_receipt(receipt)
        if self.receipt(self._operation_id) is not None:
            raise _repository_error(
                "a canvas preparation receipt already exists",
                code="canvas_preparation_receipt_exists",
            )
        receipt_payload = _bounded_payload(
            _json_bytes(
                {
                    "schema": CANVAS_PREPARATION_RECEIPT_SCHEMA,
                    "version": CANVAS_PREPARATION_RECEIPT_VERSION,
                    "receipt": receipt.as_storage_dict(),
                },
                artifact="canvas_preparation_receipt",
            ),
            maximum_bytes=_MAX_RECEIPT_BYTES,
            artifact="canvas_preparation_receipt",
            item_id=self._representation.item_id,
            representation_id=self._representation.representation_id,
        )
        try:
            transaction = self._write_set.begin(
                operation_id=self._operation_id,
                scope="canvas-preparation",
                metadata={
                    "item_id": self._representation.item_id,
                    "representation_id": self._representation.representation_id,
                },
            )
            self._stage_publication(transaction, receipt_payload)
            transaction.commit(receipt=receipt.as_public_dict())
        except Exception as exc:
            raise _repository_error(
                "the canvas preparation transaction failed",
                code="canvas_preparation_transaction_failed",
                item_id=self._representation.item_id,
                representation_id=self._representation.representation_id,
                cause_type=type(exc).__name__,
                retryable=True,
            ) from exc
        self._committed = True

    def _stage_publication(
        self,
        transaction: RecoverableWriteTransaction,
        receipt_payload: bytes,
    ) -> None:
        assert self._index_path is not None
        assert self._ledger_path is not None
        assert self._materialization_path is not None
        assert self._staged_index is not None
        assert self._staged_ledger is not None
        assert self._staged_materialization is not None
        transaction.stage_write(
            self._relative(self._materialization_path),
            self._staged_materialization,
        )
        transaction.stage_write(
            self._relative(self._ledger_path),
            self._staged_ledger,
        )
        transaction.stage_write(
            self._relative(self._index_path),
            self._staged_index,
        )
        transaction.stage_write(
            self._receipt_relative(self._operation_id),
            receipt_payload,
        )

    def _validated_index(
        self,
        raw: Any,
        *,
        representation: CanvasPreparationRepresentationSnapshot,
        entry_directory: Path,
    ) -> tuple[Mapping[str, Any] | None, dict[str, _IndexedSequence]]:
        if (
            not isinstance(raw, dict)
            or set(raw) != _INDEX_FIELDS
            or raw.get("schema") != CANVAS_INDEX_SCHEMA
            or type(raw.get("version")) is not int
            or raw.get("version") != CANVAS_INDEX_VERSION
            or raw.get("item_id") != representation.item_id
            or not isinstance(raw.get("sequences"), list)
        ):
            raise _repository_error(
                "the canvas index is invalid",
                code="invalid_canvas_index",
                item_id=representation.item_id,
            )
        requested_revision = representation.revision
        for value in raw["sequences"]:
            if (
                isinstance(value, dict)
                and set(value) == _INDEX_SEQUENCE_FIELDS
                and value.get("representation_id") == representation.representation_id
                and isinstance(value.get("representation_revision"), str)
                and value["representation_revision"]
            ):
                requested_revision = value["representation_revision"]
                break
        public = self._query_repository._sequence_record(
            raw,
            item_id=representation.item_id,
            representation_id=representation.representation_id,
            requested_revision=requested_revision,
            entry_directory=entry_directory,
        )
        sequences: dict[str, _IndexedSequence] = {}
        for value in raw["sequences"]:
            assert isinstance(value, dict)
            representation_id = value["representation_id"]
            sequences[representation_id] = _IndexedSequence(
                representation_id=representation_id,
                representation_revision=value["representation_revision"],
                canvases=tuple(
                    _IndexedCanvas(
                        canvas_id=canvas["canvas_id"],
                        position=canvas["source"]["position"],
                        path=canvas["source"]["path"],
                    )
                    for canvas in value["canvases"]
                ),
            )
        return public, sequences

    def _validated_ledger(
        self,
        raw: Any,
        *,
        item_id: str,
    ) -> tuple[dict[str, Any], dict[str, _LedgerSequence]]:
        if (
            not isinstance(raw, dict)
            or set(raw) != _LEDGER_FIELDS
            or raw.get("schema") != CANVAS_IDENTITY_LEDGER_SCHEMA
            or type(raw.get("version")) is not int
            or raw.get("version") != CANVAS_IDENTITY_LEDGER_VERSION
            or raw.get("item_id") != item_id
            or not isinstance(raw.get("sequences"), list)
        ):
            raise _repository_error(
                "the canvas identity ledger is invalid",
                code="invalid_canvas_identity_ledger",
                item_id=item_id,
            )
        sequences: dict[str, _LedgerSequence] = {}
        aliases: dict[str, str] = {}
        normalized: list[dict[str, Any]] = []
        for raw_sequence in raw["sequences"]:
            if (
                not isinstance(raw_sequence, dict)
                or set(raw_sequence) != _LEDGER_SEQUENCE_FIELDS
                or not isinstance(raw_sequence.get("bindings"), list)
            ):
                raise _repository_error(
                    "a canvas identity ledger sequence is invalid",
                    code="invalid_canvas_identity_ledger",
                    item_id=item_id,
                )
            representation_id = _identifier(
                raw_sequence.get("representation_id"),
                field_name="representation_id",
                item_id=item_id,
            )
            alias = representation_id.casefold()
            if alias in aliases:
                raise _repository_error(
                    "the canvas identity ledger contains aliased sequences",
                    code="duplicate_canvas_representation_identity",
                    item_id=item_id,
                )
            aliases[alias] = representation_id
            bindings: list[CanvasSourceIdentityBinding] = []
            ids: set[str] = set()
            correlations: set[bytes] = set()
            normalized_bindings: list[dict[str, Any]] = []
            for raw_binding in raw_sequence["bindings"]:
                if (
                    not isinstance(raw_binding, dict)
                    or set(raw_binding) != _BINDING_FIELDS
                ):
                    raise _repository_error(
                        "a canvas identity binding is invalid",
                        code="invalid_canvas_identity_ledger",
                        item_id=item_id,
                        representation_id=representation_id,
                    )
                canvas_id = _identifier(
                    raw_binding.get("canvas_id"),
                    field_name="canvas_id",
                    item_id=item_id,
                    representation_id=representation_id,
                )
                correlation_hex = raw_binding.get("source_correlation")
                active = raw_binding.get("active")
                if (
                    not isinstance(correlation_hex, str)
                    or not _CORRELATION_RE.fullmatch(correlation_hex)
                    or not isinstance(active, bool)
                ):
                    raise _repository_error(
                        "a canvas identity binding is invalid",
                        code="invalid_canvas_identity_ledger",
                        item_id=item_id,
                        representation_id=representation_id,
                    )
                correlation = bytes.fromhex(correlation_hex)
                if canvas_id.casefold() in ids or correlation in correlations:
                    raise _repository_error(
                        "the canvas identity ledger contains duplicates",
                        code="duplicate_canvas_identity_binding",
                        item_id=item_id,
                        representation_id=representation_id,
                    )
                ids.add(canvas_id.casefold())
                correlations.add(correlation)
                try:
                    binding = CanvasSourceIdentityBinding(
                        canvas_id,
                        correlation,
                        active=active,
                    )
                except (TypeError, ValueError) as exc:
                    raise _repository_error(
                        "a canvas identity binding is invalid",
                        code="invalid_canvas_identity_ledger",
                        item_id=item_id,
                        representation_id=representation_id,
                    ) from exc
                bindings.append(binding)
                normalized_bindings.append(
                    {
                        "canvas_id": canvas_id,
                        "source_correlation": correlation_hex,
                        "active": active,
                    }
                )
            sequence = _LedgerSequence(
                representation_id,
                tuple(bindings),
            )
            sequences[representation_id] = sequence
            normalized.append(
                {
                    "representation_id": representation_id,
                    "bindings": sorted(
                        normalized_bindings,
                        key=lambda value: (
                            value["canvas_id"].casefold(),
                            value["canvas_id"],
                        ),
                    ),
                }
            )
        normalized.sort(
            key=lambda value: (
                value["representation_id"].casefold(),
                value["representation_id"],
            )
        )
        return (
            {
                "schema": CANVAS_IDENTITY_LEDGER_SCHEMA,
                "version": CANVAS_IDENTITY_LEDGER_VERSION,
                "item_id": item_id,
                "sequences": normalized,
            },
            sequences,
        )

    def _validated_materialization(
        self,
        raw: Any,
        *,
        item_id: str,
        entry_directory: Path,
    ) -> tuple[dict[str, Any], dict[str, _MaterializedSequence]]:
        if (
            not isinstance(raw, dict)
            or set(raw) != _MATERIALIZATION_FIELDS
            or raw.get("schema") != CANVAS_SOURCE_MATERIALIZATION_SCHEMA
            or type(raw.get("version")) is not int
            or raw.get("version") != CANVAS_SOURCE_MATERIALIZATION_VERSION
            or raw.get("item_id") != item_id
            or not isinstance(raw.get("sequences"), list)
        ):
            raise _repository_error(
                "the canvas source materialization is invalid",
                code="invalid_canvas_source_materialization",
                item_id=item_id,
            )
        sequences: dict[str, _MaterializedSequence] = {}
        aliases: set[str] = set()
        normalized_sequences: list[dict[str, Any]] = []
        for raw_sequence in raw["sequences"]:
            if (
                not isinstance(raw_sequence, dict)
                or set(raw_sequence) != _MATERIALIZATION_SEQUENCE_FIELDS
                or not isinstance(raw_sequence.get("asset"), dict)
                or set(raw_sequence["asset"]) != _MATERIALIZATION_ASSET_FIELDS
                or not isinstance(raw_sequence.get("sources"), list)
            ):
                raise _repository_error(
                    "a canvas source materialization sequence is invalid",
                    code="invalid_canvas_source_materialization",
                    item_id=item_id,
                )
            representation_id = _identifier(
                raw_sequence.get("representation_id"),
                field_name="representation_id",
                item_id=item_id,
            )
            alias = representation_id.casefold()
            if alias in aliases:
                raise _repository_error(
                    "the canvas source materialization aliases a representation",
                    code="duplicate_canvas_representation_identity",
                    item_id=item_id,
                )
            aliases.add(alias)
            revision = _stored_revision(
                raw_sequence.get("representation_revision"),
                item_id=item_id,
                representation_id=representation_id,
            )
            media_type = _media_type(
                raw_sequence.get("media_type"),
                item_id=item_id,
                representation_id=representation_id,
            )
            generation = raw_sequence.get("generation")
            asset = raw_sequence["asset"]
            asset_sha256 = asset.get("sha256")
            asset_size = asset.get("size")
            source_count = asset.get("source_count")
            if (
                type(generation) is not int
                or generation < 1
                or not isinstance(asset_sha256, str)
                or not _SHA256_RE.fullmatch(asset_sha256)
                or type(asset_size) is not int
                or asset_size < 0
                or type(source_count) is not int
                or source_count < 0
                or source_count > _MAX_CANVASES
            ):
                raise _repository_error(
                    "a canvas source materialization asset is invalid",
                    code="invalid_canvas_source_materialization",
                    item_id=item_id,
                    representation_id=representation_id,
                )
            sources: list[_MaterializedSource] = []
            normalized_sources: list[dict[str, Any]] = []
            correlations: set[bytes] = set()
            active_positions: set[int] = set()
            for raw_source in raw_sequence["sources"]:
                source, normalized_source = self._materialized_source(
                    raw_source,
                    generation=generation,
                    item_id=item_id,
                    representation_id=representation_id,
                    entry_directory=entry_directory,
                )
                if source.source_correlation in correlations:
                    raise _repository_error(
                        "the canvas source materialization contains duplicates",
                        code="invalid_canvas_source_materialization",
                        item_id=item_id,
                        representation_id=representation_id,
                    )
                correlations.add(source.source_correlation)
                if source.active:
                    if source.position in active_positions:
                        raise _repository_error(
                            "active canvas source positions are duplicated",
                            code="invalid_canvas_source_materialization",
                            item_id=item_id,
                            representation_id=representation_id,
                        )
                    active_positions.add(source.position)
                sources.append(source)
                normalized_sources.append(normalized_source)
            if source_count != len(active_positions):
                raise _repository_error(
                    "the materialized asset source count is inconsistent",
                    code="invalid_canvas_source_materialization",
                    item_id=item_id,
                    representation_id=representation_id,
                )
            sequence = _MaterializedSequence(
                representation_id=representation_id,
                representation_revision=revision,
                media_type=media_type,
                generation=generation,
                asset_sha256=asset_sha256,
                asset_size=asset_size,
                source_count=source_count,
                sources=tuple(sources),
            )
            sequences[representation_id] = sequence
            normalized_sources.sort(key=lambda value: value["source_correlation"])
            normalized_sequences.append(
                {
                    "representation_id": representation_id,
                    "representation_revision": revision,
                    "media_type": media_type,
                    "generation": generation,
                    "asset": {
                        "sha256": asset_sha256,
                        "size": asset_size,
                        "source_count": source_count,
                    },
                    "sources": normalized_sources,
                }
            )
        normalized_sequences.sort(
            key=lambda value: (
                value["representation_id"].casefold(),
                value["representation_id"],
            )
        )
        return (
            {
                "schema": CANVAS_SOURCE_MATERIALIZATION_SCHEMA,
                "version": CANVAS_SOURCE_MATERIALIZATION_VERSION,
                "item_id": item_id,
                "sequences": normalized_sequences,
            },
            sequences,
        )

    def _materialized_source(
        self,
        raw: Any,
        *,
        generation: int,
        item_id: str,
        representation_id: str,
        entry_directory: Path,
    ) -> tuple[_MaterializedSource, dict[str, Any]]:
        if (
            not isinstance(raw, dict)
            or set(raw) != _MATERIALIZATION_SOURCE_FIELDS
            or not isinstance(raw.get("last_locator"), dict)
            or set(raw["last_locator"]) != _MATERIALIZATION_LOCATOR_FIELDS
        ):
            raise _repository_error(
                "a materialized canvas source is invalid",
                code="invalid_canvas_source_materialization",
                item_id=item_id,
                representation_id=representation_id,
            )
        correlation_hex = raw.get("source_correlation")
        active = raw.get("active")
        born = raw.get("born_generation")
        last_active = raw.get("last_active_generation")
        last_retired = raw.get("last_retired_generation")
        disposition = raw.get("disposition")
        locator = raw["last_locator"]
        position = locator.get("position")
        path = locator.get("path")
        if (
            not isinstance(correlation_hex, str)
            or not _CORRELATION_RE.fullmatch(correlation_hex)
            or not isinstance(active, bool)
            or type(born) is not int
            or born < 1
            or born > generation
            or type(last_active) is not int
            or last_active < born
            or last_active > generation
            or (
                last_retired is not None
                and (
                    type(last_retired) is not int
                    or last_retired < born
                    or last_retired > generation
                )
            )
            or (active and last_retired is not None and last_retired >= last_active)
            or (not active and last_retired is None)
            or (not active and last_retired < last_active)
            or disposition not in _MATERIALIZATION_DISPOSITIONS
            or type(position) is not int
            or position < 0
        ):
            raise _repository_error(
                "a materialized canvas source is invalid",
                code="invalid_canvas_source_materialization",
                item_id=item_id,
                representation_id=representation_id,
            )
        self._query_repository._validate_source_path(
            path,
            entry_directory=entry_directory,
            item_id=item_id,
            representation_id=representation_id,
        )
        evidence, normalized_evidence = self._evidence(
            raw.get("evidence"),
            item_id=item_id,
            representation_id=representation_id,
            code="invalid_canvas_source_materialization",
        )
        source = _MaterializedSource(
            source_correlation=bytes.fromhex(correlation_hex),
            active=active,
            born_generation=born,
            last_active_generation=last_active,
            last_retired_generation=last_retired,
            position=position,
            path=path,
            disposition=disposition,
            evidence=evidence,
        )
        return source, {
            "source_correlation": correlation_hex,
            "active": active,
            "born_generation": born,
            "last_active_generation": last_active,
            "last_retired_generation": last_retired,
            "last_locator": {"position": position, "path": path},
            "disposition": disposition,
            "evidence": normalized_evidence,
        }

    @staticmethod
    def _evidence(
        value: Any,
        *,
        item_id: str,
        representation_id: str,
        code: str,
    ) -> tuple[FilesystemCanvasEvidence, dict[str, Any]]:
        if isinstance(value, FilesystemCanvasEvidence):
            raw = {
                "profile": value.profile,
                "width_mpt": value.width_mpt,
                "height_mpt": value.height_mpt,
                "rotation": value.rotation,
                "strong_sha256": value.strong_sha256,
                "fuzzy_hash": value.fuzzy_hash,
            }
        else:
            raw = value
        if not isinstance(raw, dict) or set(raw) != _MATERIALIZATION_EVIDENCE_FIELDS:
            raise _repository_error(
                "canvas source evidence is invalid",
                code=code,
                item_id=item_id,
                representation_id=representation_id,
            )
        profile = raw.get("profile")
        width_mpt = raw.get("width_mpt")
        height_mpt = raw.get("height_mpt")
        rotation = raw.get("rotation")
        strong_sha256 = raw.get("strong_sha256")
        fuzzy_hash = raw.get("fuzzy_hash")
        if (
            not isinstance(profile, str)
            or not _IDENTIFIER_RE.fullmatch(profile)
            or type(width_mpt) is not int
            or width_mpt < 1
            or type(height_mpt) is not int
            or height_mpt < 1
            or type(rotation) is not int
            or rotation not in {0, 90, 180, 270}
            or not isinstance(strong_sha256, str)
            or not _SHA256_RE.fullmatch(strong_sha256)
            or not isinstance(fuzzy_hash, str)
            or (fuzzy_hash != "" and not _FUZZY_HASH_RE.fullmatch(fuzzy_hash))
        ):
            raise _repository_error(
                "canvas source evidence is invalid",
                code=code,
                item_id=item_id,
                representation_id=representation_id,
            )
        evidence = FilesystemCanvasEvidence(
            profile=profile,
            width_mpt=width_mpt,
            height_mpt=height_mpt,
            rotation=rotation,
            strong_sha256=strong_sha256,
            fuzzy_hash=fuzzy_hash,
        )
        return evidence, {
            "profile": profile,
            "width_mpt": width_mpt,
            "height_mpt": height_mpt,
            "rotation": rotation,
            "strong_sha256": strong_sha256,
            "fuzzy_hash": fuzzy_hash,
        }

    @staticmethod
    def _validate_artifact_alignment(
        index: Mapping[str, _IndexedSequence],
        ledger: Mapping[str, _LedgerSequence],
        materialization: Mapping[str, _MaterializedSequence],
        *,
        item_id: str,
    ) -> None:
        if set(index) != set(ledger) or set(index) != set(materialization):
            raise _repository_error(
                "the canvas preparation artifacts disagree",
                code="canvas_preparation_artifact_mismatch",
                item_id=item_id,
            )
        for representation_id, indexed in index.items():
            ledger_bindings = ledger[representation_id].bindings
            materialized = materialization[representation_id]
            index_by_id = {canvas.canvas_id: canvas for canvas in indexed.canvases}
            ledger_by_source = {
                binding.source_correlation: binding for binding in ledger_bindings
            }
            source_by_correlation = {
                source.source_correlation: source
                for source in materialized.sources
            }
            active_ids = {
                binding.canvas_id for binding in ledger_bindings if binding.active
            }
            if (
                indexed.representation_revision
                != materialized.representation_revision
                or active_ids != set(index_by_id)
                or len(active_ids) != len(index_by_id)
                or set(ledger_by_source) != set(source_by_correlation)
                or materialized.source_count != len(index_by_id)
            ):
                raise _repository_error(
                    "the canvas preparation artifacts disagree",
                    code="canvas_preparation_artifact_mismatch",
                    item_id=item_id,
                    representation_id=representation_id,
                )
            for correlation, binding in ledger_by_source.items():
                source = source_by_correlation[correlation]
                if binding.active != source.active:
                    raise _repository_error(
                        "the canvas preparation artifacts disagree",
                        code="canvas_preparation_artifact_mismatch",
                        item_id=item_id,
                        representation_id=representation_id,
                    )
                if not binding.active:
                    continue
                indexed_canvas = index_by_id.get(binding.canvas_id)
                if (
                    indexed_canvas is None
                    or indexed_canvas.position != source.position
                    or indexed_canvas.path != source.path
                ):
                    raise _repository_error(
                        "the canvas preparation artifacts disagree",
                        code="canvas_preparation_artifact_mismatch",
                        item_id=item_id,
                        representation_id=representation_id,
                    )

    def _inspection(
        self,
        value: Any,
        *,
        item_id: str,
        representation_id: str,
    ) -> tuple[FilesystemCanvasInspection, tuple[FilesystemCanvasObservation, ...]]:
        if not isinstance(value, FilesystemCanvasInspection):
            raise _repository_error(
                "the local media inspector returned an invalid inspection",
                code="invalid_canvas_inspection_result",
                item_id=item_id,
                representation_id=representation_id,
            )
        media_type = _media_type(
            value.media_type,
            item_id=item_id,
            representation_id=representation_id,
            code="invalid_canvas_inspection_result",
        )
        if (
            not isinstance(value.asset_sha256, str)
            or not _SHA256_RE.fullmatch(value.asset_sha256)
            or type(value.asset_size) is not int
            or value.asset_size < 0
            or isinstance(value.observations, (str, bytes))
            or not isinstance(value.observations, Sequence)
        ):
            raise _repository_error(
                "the local media inspector returned an invalid inspection",
                code="invalid_canvas_inspection_result",
                item_id=item_id,
                representation_id=representation_id,
            )
        if len(value.observations) > _MAX_CANVASES:
            raise _repository_error(
                "the local media inspector returned too many observations",
                code="canvas_candidate_limit_exceeded",
                item_id=item_id,
                representation_id=representation_id,
                maximum=_MAX_CANVASES,
            )
        observations = tuple(value.observations)
        positions: set[int] = set()
        normalized: list[FilesystemCanvasObservation] = []
        for observation in observations:
            if not isinstance(observation, FilesystemCanvasObservation):
                raise _repository_error(
                    "the local media inspector returned invalid observations",
                    code="invalid_canvas_inspection_result",
                    item_id=item_id,
                    representation_id=representation_id,
                )
            position = observation.source_position
            if type(position) is not int or position < 0 or position in positions:
                raise _repository_error(
                    "a canvas source position is invalid or duplicated",
                    code="invalid_canvas_inspection_result",
                    item_id=item_id,
                    representation_id=representation_id,
                )
            positions.add(position)
            assert self._entry_directory is not None
            self._query_repository._validate_source_path(
                observation.source_path,
                entry_directory=self._entry_directory,
                item_id=item_id,
                representation_id=representation_id,
            )
            evidence, _normalized_evidence = self._evidence(
                observation.evidence,
                item_id=item_id,
                representation_id=representation_id,
                code="invalid_canvas_inspection_result",
            )
            normalized.append(
                FilesystemCanvasObservation(
                    source_position=position,
                    source_path=observation.source_path,
                    evidence=evidence,
                    label=observation.label,
                    extent=observation.extent,
                    available=observation.available,
                    resource_kinds=observation.resource_kinds,
                    metadata=observation.metadata,
                )
            )
        inspection = FilesystemCanvasInspection(
            media_type=media_type,
            asset_sha256=value.asset_sha256,
            asset_size=value.asset_size,
            observations=tuple(normalized),
        )
        return inspection, tuple(normalized)

    def _materialized_sources(
        self,
        inspection: FilesystemCanvasInspection,
        observations: tuple[FilesystemCanvasObservation, ...],
        *,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> tuple[tuple[_MaterializedSource, ...], dict[int, bytes], int]:
        prior = self._materialization_sequences.get(
            representation.representation_id
        )
        if prior is None:
            generation = 1
            correlations: set[bytes] = set()
            sources: list[_MaterializedSource] = []
            by_position: dict[int, bytes] = {}
            for observation in observations:
                correlation = self._allocate_source_correlation(
                    correlations,
                    representation=representation,
                )
                correlations.add(correlation)
                by_position[observation.source_position] = correlation
                sources.append(
                    _MaterializedSource(
                        source_correlation=correlation,
                        active=True,
                        born_generation=generation,
                        last_active_generation=generation,
                        last_retired_generation=None,
                        position=observation.source_position,
                        path=observation.source_path,
                        disposition="minted",
                        evidence=observation.evidence,
                    )
                )
            return tuple(sources), by_position, generation

        if prior.asset_sha256 != inspection.asset_sha256:
            if prior.representation_revision == representation.revision:
                raise ConflictError(
                    "the representation bytes changed without a new revision",
                    code="canvas_source_revision_drift",
                    details={
                        "item_id": representation.item_id,
                        "representation_id": representation.representation_id,
                        "representation_revision": representation.revision,
                    },
                )
            raise ConflictError(
                "the changed representation requires canvas reconciliation",
                code="canvas_source_reconciliation_required",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "indexed_revision": prior.representation_revision,
                    "authoritative_revision": representation.revision,
                    "before_count": prior.source_count,
                    "current_count": len(observations),
                },
            )
        if (
            prior.media_type != inspection.media_type
            or prior.asset_size != inspection.asset_size
        ):
            raise _repository_error(
                "identical canvas asset evidence is inconsistent",
                code="canvas_source_materialization_mismatch",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
            )
        active_by_position = {
            source.position: source for source in prior.sources if source.active
        }
        positions = {value.source_position for value in observations}
        if (
            prior.source_count != len(observations)
            or len(active_by_position) != len(observations)
            or set(active_by_position) != positions
        ):
            raise _repository_error(
                "identical asset bytes produced a different canvas source set",
                code="canvas_source_materialization_mismatch",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
            )
        observation_by_position = {
            value.source_position: value for value in observations
        }
        sources = []
        by_position = {}
        for source in prior.sources:
            if not source.active:
                sources.append(source)
                continue
            observation = observation_by_position[source.position]
            by_position[source.position] = source.source_correlation
            sources.append(
                _MaterializedSource(
                    source_correlation=source.source_correlation,
                    active=True,
                    born_generation=source.born_generation,
                    last_active_generation=source.last_active_generation,
                    last_retired_generation=source.last_retired_generation,
                    position=observation.source_position,
                    path=observation.source_path,
                    disposition="unchanged-asset",
                    evidence=observation.evidence,
                )
            )
        return tuple(sources), by_position, prior.generation

    def _allocate_source_correlation(
        self,
        reserved: set[bytes],
        *,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> bytes:
        for _attempt in range(_CORRELATION_ALLOCATION_ATTEMPTS):
            try:
                value = self._source_correlation_factory(frozenset(reserved))
            except EngineError:
                raise
            except Exception as exc:
                raise _repository_error(
                    "a canvas source correlation could not be allocated",
                    code="canvas_source_correlation_allocation_failed",
                    item_id=representation.item_id,
                    representation_id=representation.representation_id,
                    cause_type=type(exc).__name__,
                ) from exc
            if not isinstance(value, bytes) or len(value) != 32:
                raise _repository_error(
                    "the canvas source correlation allocator returned invalid data",
                    code="invalid_allocated_canvas_source_correlation",
                    item_id=representation.item_id,
                    representation_id=representation.representation_id,
                )
            if value not in reserved:
                return value
        raise _repository_error(
            "a unique canvas source correlation could not be allocated",
            code="canvas_source_correlation_collision",
            item_id=representation.item_id,
            representation_id=representation.representation_id,
        )

    def _assign_identities(
        self,
        correlations: tuple[bytes, ...],
        *,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> tuple[
        tuple[CanvasSourceIdentityBinding, ...],
        dict[bytes, str],
    ]:
        prior = self._ledger_sequences.get(representation.representation_id)
        previous = () if prior is None else prior.bindings
        by_source = {binding.source_correlation: binding for binding in previous}
        reserved_ids = {binding.canvas_id for binding in previous}
        reserved_aliases = {value.casefold() for value in reserved_ids}
        assigned: dict[bytes, str] = {}
        current_sources = set(correlations)
        bindings: list[CanvasSourceIdentityBinding] = []
        for binding in previous:
            active = binding.source_correlation in current_sources
            bindings.append(
                CanvasSourceIdentityBinding(
                    binding.canvas_id,
                    binding.source_correlation,
                    active=active,
                )
            )
            if active:
                assigned[binding.source_correlation] = binding.canvas_id
        for correlation in correlations:
            if correlation in assigned:
                continue
            if correlation in by_source:
                canvas_id = by_source[correlation].canvas_id
                assigned[correlation] = canvas_id
                continue
            try:
                allocated = self._allocate_canvas_id(frozenset(reserved_ids))
            except EngineError:
                raise
            except Exception as exc:
                raise _repository_error(
                    "a canvas identity could not be allocated",
                    code="canvas_identity_allocation_failed",
                    item_id=representation.item_id,
                    representation_id=representation.representation_id,
                    cause_type=type(exc).__name__,
                ) from exc
            try:
                canvas_id = _identifier(
                    allocated,
                    field_name="canvas_id",
                    item_id=representation.item_id,
                    representation_id=representation.representation_id,
                )
            except RepositoryError as exc:
                raise _repository_error(
                    "the canvas identity allocator returned an invalid identity",
                    code="invalid_allocated_canvas_identity",
                    item_id=representation.item_id,
                    representation_id=representation.representation_id,
                ) from exc
            if canvas_id.casefold() in reserved_aliases:
                raise _repository_error(
                    "the canvas identity allocator reused a reserved identity",
                    code="canvas_identity_reserved",
                    item_id=representation.item_id,
                    representation_id=representation.representation_id,
                )
            reserved_ids.add(canvas_id)
            reserved_aliases.add(canvas_id.casefold())
            assigned[correlation] = canvas_id
            bindings.append(
                CanvasSourceIdentityBinding(canvas_id, correlation, active=True)
            )
        return tuple(bindings), assigned

    def _canvas_record(
        self,
        observation: FilesystemCanvasObservation,
        *,
        correlation: bytes,
        canvas_id: str,
        order: int,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> dict[str, Any]:
        try:
            provisional = CanvasView(
                key=CanvasKey(
                    representation.item_id,
                    representation.representation_id,
                    canvas_id,
                ),
                revision="candidate-v1",
                order=order,
                label=observation.label,
                extent=observation.extent,
                available=observation.available,
                resource_kinds=observation.resource_kinds,
                metadata=observation.metadata,
            )
            public = provisional.as_dict()
        except (TypeError, ValueError, ValidationError) as exc:
            raise _repository_error(
                "a canvas candidate contains invalid public state",
                code="invalid_canvas_inspection_result",
                item_id=representation.item_id,
                representation_id=representation.representation_id,
                cause_type=type(exc).__name__,
            ) from exc
        public_state = {
            "order": public["order"],
            "label": public["label"],
            "extent": public["extent"],
            "available": public["available"],
            "resource_kinds": public["resource_kinds"],
            "metadata": public["metadata"],
        }
        revision = _producer_revision(
            representation_revision=representation.revision,
            canvas_id=canvas_id,
            correlation=correlation,
            source_position=observation.source_position,
            source_path=observation.source_path,
            public=public_state,
        )
        return {
            "canvas_id": canvas_id,
            "revision": revision,
            **public_state,
            "source": {
                "position": observation.source_position,
                "path": observation.source_path,
            },
        }

    def _updated_index(
        self,
        sequence: dict[str, Any],
        *,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> dict[str, Any]:
        assert self._base_index is not None
        sequences = [
            value
            for value in self._base_index["sequences"]
            if value["representation_id"] != representation.representation_id
        ]
        sequences.append(sequence)
        sequences.sort(
            key=lambda value: (
                value["representation_id"].casefold(),
                value["representation_id"],
            )
        )
        return {
            "schema": CANVAS_INDEX_SCHEMA,
            "version": CANVAS_INDEX_VERSION,
            "item_id": representation.item_id,
            "sequences": sequences,
        }

    def _updated_ledger(
        self,
        bindings: tuple[CanvasSourceIdentityBinding, ...],
        *,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> dict[str, Any]:
        assert self._base_ledger is not None
        sequences = [
            value
            for value in self._base_ledger["sequences"]
            if value["representation_id"] != representation.representation_id
        ]
        sequences.append(
            {
                "representation_id": representation.representation_id,
                "bindings": [
                    {
                        "canvas_id": binding.canvas_id,
                        "source_correlation": binding.source_correlation.hex(),
                        "active": binding.active,
                    }
                    for binding in bindings
                ],
            }
        )
        sequences.sort(
            key=lambda value: (
                value["representation_id"].casefold(),
                value["representation_id"],
            )
        )
        return {
            "schema": CANVAS_IDENTITY_LEDGER_SCHEMA,
            "version": CANVAS_IDENTITY_LEDGER_VERSION,
            "item_id": representation.item_id,
            "sequences": sequences,
        }

    def _updated_materialization(
        self,
        inspection: FilesystemCanvasInspection,
        sources: tuple[_MaterializedSource, ...],
        *,
        generation: int,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> dict[str, Any]:
        assert self._base_materialization is not None
        sequences = [
            value
            for value in self._base_materialization["sequences"]
            if value["representation_id"] != representation.representation_id
        ]
        serialized_sources = []
        for source in sources:
            _evidence, evidence = self._evidence(
                source.evidence,
                item_id=representation.item_id,
                representation_id=representation.representation_id,
                code="invalid_canvas_inspection_result",
            )
            serialized_sources.append(
                {
                    "source_correlation": source.source_correlation.hex(),
                    "active": source.active,
                    "born_generation": source.born_generation,
                    "last_active_generation": source.last_active_generation,
                    "last_retired_generation": source.last_retired_generation,
                    "last_locator": {
                        "position": source.position,
                        "path": source.path,
                    },
                    "disposition": source.disposition,
                    "evidence": evidence,
                }
            )
        serialized_sources.sort(key=lambda value: value["source_correlation"])
        sequences.append(
            {
                "representation_id": representation.representation_id,
                "representation_revision": representation.revision,
                "media_type": inspection.media_type,
                "generation": generation,
                "asset": {
                    "sha256": inspection.asset_sha256,
                    "size": inspection.asset_size,
                    "source_count": len(inspection.observations),
                },
                "sources": serialized_sources,
            }
        )
        sequences.sort(
            key=lambda value: (
                value["representation_id"].casefold(),
                value["representation_id"],
            )
        )
        return {
            "schema": CANVAS_SOURCE_MATERIALIZATION_SCHEMA,
            "version": CANVAS_SOURCE_MATERIALIZATION_VERSION,
            "item_id": representation.item_id,
            "sequences": sequences,
        }

    def _artifact_paths(
        self,
        item_id: str,
    ) -> tuple[Path, Path, Path, Path]:
        # The query adapter owns the canonical v1 index path grammar.
        entry_directory, index_path = self._query_repository._index_path(item_id)
        ledger_path = entry_directory.joinpath(*CANVAS_IDENTITY_LEDGER_RELATIVE.parts)
        self._query_repository._assert_safe_components(
            ledger_path,
            item_id=item_id,
            message="the canvas identity ledger path is unsafe",
            code="unsafe_canvas_identity_ledger_path",
        )
        if ledger_path.parent.exists() and not ledger_path.parent.is_dir():
            raise _repository_error(
                "the canvas identity ledger parent is not a directory",
                code="unsafe_canvas_identity_ledger_path",
                item_id=item_id,
            )
        materialization_path = entry_directory.joinpath(
            *CANVAS_SOURCE_MATERIALIZATION_RELATIVE.parts
        )
        self._query_repository._assert_safe_components(
            materialization_path,
            item_id=item_id,
            message="the canvas source materialization path is unsafe",
            code="unsafe_canvas_source_materialization_path",
        )
        if (
            materialization_path.parent.exists()
            and not materialization_path.parent.is_dir()
        ):
            raise _repository_error(
                "the canvas source materialization parent is not a directory",
                code="unsafe_canvas_source_materialization_path",
                item_id=item_id,
            )
        return entry_directory, index_path, ledger_path, materialization_path

    def _receipt_path(self, operation_id: str) -> Path:
        relative = self._receipt_relative(operation_id)
        path = self._write_set.root.joinpath(*PurePosixPath(relative).parts)
        self._assert_safe_components(
            path,
            code="unsafe_canvas_preparation_receipt_path",
        )
        return path

    @staticmethod
    def _receipt_relative(operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return (_RECEIPT_ROOT / f"{digest}.json").as_posix()

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self._write_set.root).as_posix()
        except ValueError as exc:
            raise _repository_error(
                "a canvas preparation target escapes its workspace",
                code="unsafe_canvas_preparation_path",
            ) from exc

    def _assert_safe_components(self, path: Path, *, code: str) -> None:
        try:
            relative = path.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                "a canvas preparation path escapes its workspace",
                code=code,
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a canvas preparation path redirects outside its namespace",
                    code=code,
                )
        try:
            path.resolve(strict=False).relative_to(self._write_set.root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a canvas preparation path escapes its workspace",
                code=code,
            ) from exc

    def _regular_file_exists(
        self,
        path: Path,
        *,
        artifact: str,
        item_id: str = "",
        representation_id: str = "",
        allow_missing: bool,
    ) -> bool:
        self._assert_safe_components(
            path,
            code="unsafe_canvas_preparation_path",
        )
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return False
            raise _repository_error(
                "a canvas preparation artifact is missing",
                code="invalid_canvas_preparation_artifact",
                item_id=item_id,
                representation_id=representation_id,
                artifact=artifact,
            )
        except OSError as exc:
            raise _repository_error(
                "a canvas preparation artifact cannot be inspected",
                code="canvas_preparation_repository_unavailable",
                item_id=item_id,
                representation_id=representation_id,
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting_path(path) or not stat.S_ISREG(info.st_mode):
            raise _repository_error(
                "a canvas preparation artifact is not a private regular file",
                code="unsafe_canvas_preparation_path",
                item_id=item_id,
                representation_id=representation_id,
                artifact=artifact,
            )
        return True

    def _read_json(
        self,
        path: Path,
        *,
        maximum_bytes: int,
        artifact: str,
        item_id: str = "",
        representation_id: str = "",
    ) -> Any:
        self._regular_file_exists(
            path,
            artifact=artifact,
            item_id=item_id,
            representation_id=representation_id,
            allow_missing=False,
        )
        try:
            with path.open("rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("artifact is not a regular file")
                encoded = stream.read(maximum_bytes + 1)
            if len(encoded) > maximum_bytes:
                raise ValueError("artifact exceeds its size limit")
            return json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise _repository_error(
                "a canvas preparation artifact cannot be decoded",
                code="invalid_canvas_preparation_artifact",
                item_id=item_id,
                representation_id=representation_id,
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc

    def _validate_receipt(self, receipt: CanvasPreparationReceipt) -> None:
        assert self._representation is not None
        assert self._after is not None
        expected_before = (
            None
            if self._before is None
            else CanvasPreparationSequenceSummary.from_sequence(self._before.sequence)
        )
        expected_after = CanvasPreparationSequenceSummary.from_sequence(
            self._after.sequence
        )
        expected_hash = _command_hash(
            item_id=self._representation.item_id,
            representation_id=self._representation.representation_id,
            representation_revision=self._representation.revision,
        )
        if (
            not isinstance(receipt, CanvasPreparationReceipt)
            or receipt.operation_id != self._operation_id
            or receipt.command_sha256 != expected_hash
            or receipt.item_id != self._representation.item_id
            or receipt.representation_id != self._representation.representation_id
            or receipt.representation_revision != self._representation.revision
            or receipt.before != expected_before
            or receipt.after != expected_after
        ):
            raise _repository_error(
                "the preparation receipt is outside the staged operation",
                code="receipt_scope_mismatch",
                item_id=self._representation.item_id,
                representation_id=self._representation.representation_id,
            )

    @staticmethod
    def _sequence_view(record: Mapping[str, Any]) -> CanvasSequenceView:
        return CanvasQueryService(_StaticCanvasRecordRepository(record)).list(
            record["item_id"],
            record["representation_id"],
        )

    @staticmethod
    def _empty_index(item_id: str) -> dict[str, Any]:
        return {
            "schema": CANVAS_INDEX_SCHEMA,
            "version": CANVAS_INDEX_VERSION,
            "item_id": item_id,
            "sequences": [],
        }

    @staticmethod
    def _empty_ledger(item_id: str) -> dict[str, Any]:
        return {
            "schema": CANVAS_IDENTITY_LEDGER_SCHEMA,
            "version": CANVAS_IDENTITY_LEDGER_VERSION,
            "item_id": item_id,
            "sequences": [],
        }

    @staticmethod
    def _empty_materialization(item_id: str) -> dict[str, Any]:
        return {
            "schema": CANVAS_SOURCE_MATERIALIZATION_SCHEMA,
            "version": CANVAS_SOURCE_MATERIALIZATION_VERSION,
            "item_id": item_id,
            "sequences": [],
        }

    def _ensure_after_receipt(self) -> None:
        self._ensure_open()
        if not self._receipt_checked:
            raise _repository_error(
                "durable receipt lookup must precede live state access",
                code="canvas_preparation_replay_not_checked",
            )

    def _ensure_open(self) -> None:
        if self._closed:
            raise _repository_error(
                "the canvas preparation unit is closed",
                code="canvas_preparation_unit_closed",
            )


__all__ = [
    "CANVAS_IDENTITY_LEDGER_RELATIVE",
    "CANVAS_IDENTITY_LEDGER_SCHEMA",
    "CANVAS_IDENTITY_LEDGER_VERSION",
    "CANVAS_PREPARATION_RECEIPT_SCHEMA",
    "CANVAS_PREPARATION_RECEIPT_VERSION",
    "CANVAS_SOURCE_MATERIALIZATION_RELATIVE",
    "CANVAS_SOURCE_MATERIALIZATION_SCHEMA",
    "CANVAS_SOURCE_MATERIALIZATION_VERSION",
    "FilesystemCanvasEvidence",
    "FilesystemCanvasInspection",
    "FilesystemCanvasObservation",
    "FilesystemCanvasPreparationRepository",
]
