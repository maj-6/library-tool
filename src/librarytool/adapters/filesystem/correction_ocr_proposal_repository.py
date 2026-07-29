"""Recoverable storage for correction OCR machine proposals.

The repository can read only the checksum-pinned ``ocr-ready`` output supplied
by the transform store.  Its sole write is a new immutable proposal document;
there is deliberately no callback for canonical text or human assertions.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.correction_ocr import (
    CORRECTION_OCR_MAX_SOURCE_BYTES,
    CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT,
    CORRECTION_OCR_PROPOSAL_POLICY,
    CorrectionOcrProposalCatalogSnapshot,
    CorrectionOcrProposalProviderView,
    CorrectionOcrProposalSummaryView,
    CorrectionOcrProposalView,
    CorrectionOcrProviderSelection,
    CorrectionOcrRecognition,
    StoredCorrectionOcrProposal,
)
from ...engine.correction_transforms import (
    CommittedCorrectionOutput,
    OcrFollowupRequest,
)
from ...engine.errors import ConflictError, EngineError, NotFoundError, RepositoryError
from .corrections_artifact_repository import (
    _AuthorityDirectorySnapshot,
    _AuthoritySnapshot,
    _finish_verified_regular,
    _open_verified_regular,
    _stable_stat_identity,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    WriteSetError,
    _is_redirecting_path,
)


CorrectionOutputBytesLookup: TypeAlias = Callable[
    [str, str, CommittedCorrectionOutput], bytes | None
]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]

CORRECTION_OCR_PROPOSAL_SCHEMA = "librarytool.correction-ocr-proposal"
CORRECTION_OCR_PROPOSAL_VERSION = 1
CORRECTION_OCR_PROPOSAL_RECEIPT_SCHEMA = (
    "librarytool.correction-ocr-proposal-receipt"
)
CORRECTION_OCR_PROPOSAL_RECEIPT_VERSION = 1
_PROPOSAL_ROOT = PurePosixPath(
    ".engine/correction-transforms/ocr-proposals"
)
_RECEIPT_ROOT = PurePosixPath(
    ".engine/receipts/correction-ocr-proposals"
)
_OPERATION_ROOT = PurePosixPath(
    ".engine/receipts/correction-ocr-proposal-operations"
)
_MAX_PROPOSAL_BYTES = 128 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_CATALOG_DIRECTORY_ENTRIES = (
    CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT * 2
)
_MAX_CATALOG_JSON_DOCUMENTS = (
    CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT * 3
)
_MAX_CATALOG_JSON_BYTES = 256 * 1024 * 1024
_DOCUMENT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "proposal_ref",
        "operation_id",
        "item_id",
        "source",
        "provider",
        "recognition",
        "publication_policy",
    }
)
_SOURCE_FIELDS = frozenset(
    {"kind", "artifact_id", "artifact_revision", "content_sha256"}
)
_PROVIDER_FIELDS = frozenset({"provider_id", "model", "options"})
_RECOGNITION_FIELDS = frozenset(
    {"provider_id", "model", "options", "payload"}
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "proposal_ref",
        "operation_id",
        "item_id",
        "source_sha256",
        "proposal_sha256",
    }
)
_OPERATION_SCHEMA = "librarytool.correction-ocr-proposal-operation"
_OPERATION_VERSION = 1
_OPERATION_FIELDS = frozenset(
    {
        "schema",
        "version",
        "proposal_ref",
        "operation_id",
        "item_id",
    }
)


@dataclass(frozen=True, slots=True)
class _LoadedCorrectionOcrProposal:
    stored: StoredCorrectionOcrProposal
    view: CorrectionOcrProposalView


@dataclass(slots=True)
class _CatalogScanBudget:
    directory_entries: int = 0
    proposal_count: int = 0
    json_documents: int = 0
    json_bytes: int = 0

    @staticmethod
    def _exceeded(name: str, maximum: int) -> RepositoryError:
        return RepositoryError(
            "the OCR proposal catalog exceeds its read budget",
            code="correction_ocr_proposal_catalog_budget_exceeded",
            details={
                "artifact": "correction_ocr_proposal",
                "budget": name,
                "maximum": maximum,
            },
        )

    def consume_directory_entry(self) -> None:
        self.directory_entries += 1
        if self.directory_entries > _MAX_CATALOG_DIRECTORY_ENTRIES:
            raise self._exceeded(
                "directory_entries",
                _MAX_CATALOG_DIRECTORY_ENTRIES,
            )

    def set_proposal_count(self, count: int) -> None:
        self.proposal_count = count
        if count > CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT:
            raise self._exceeded(
                "proposal_count",
                CORRECTION_OCR_PROPOSAL_CATALOG_MAX_COUNT,
            )

    def start_json_document(self) -> None:
        self.json_documents += 1
        if self.json_documents > _MAX_CATALOG_JSON_DOCUMENTS:
            raise self._exceeded(
                "json_documents",
                _MAX_CATALOG_JSON_DOCUMENTS,
            )

    def consume_json_bytes(self, count: int) -> None:
        self.json_bytes += count
        if self.json_bytes > _MAX_CATALOG_JSON_BYTES:
            raise self._exceeded(
                "json_bytes",
                _MAX_CATALOG_JSON_BYTES,
            )


def _repository_error(
    message: str,
    *,
    code: str,
    cause: Exception | None = None,
    retryable: bool = False,
) -> RepositoryError:
    details = {"artifact": "correction_ocr_proposal"}
    if cause is not None:
        details["cause_type"] = type(cause).__name__
    return RepositoryError(
        message,
        code=code,
        details=details,
        retryable=retryable,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _repository_error(
            "the OCR proposal cannot be serialized",
            code="invalid_correction_ocr_proposal",
            cause=exc,
        ) from exc


def _strict_object(
    raw: Any,
    *,
    fields: frozenset[str],
    name: str,
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or frozenset(raw) != fields:
        raise ValueError(f"{name} must contain its exact schema fields")
    return raw


def _source_from_dict(raw: Any) -> CommittedCorrectionOutput:
    value = _strict_object(
        raw,
        fields=_SOURCE_FIELDS,
        name="OCR proposal source",
    )
    return CommittedCorrectionOutput(
        kind=value["kind"],
        artifact_id=value["artifact_id"],
        artifact_revision=value["artifact_revision"],
        content_sha256=value["content_sha256"],
    )


def _provider_from_dict(raw: Any) -> CorrectionOcrProviderSelection:
    value = _strict_object(
        raw,
        fields=_PROVIDER_FIELDS,
        name="OCR proposal provider",
    )
    return CorrectionOcrProviderSelection(
        provider_id=value["provider_id"],
        model=value["model"],
        options=value["options"],
    )


class FilesystemCorrectionOcrProposalRepository:
    """Read exact transform bytes and commit provider-neutral OCR proposals."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        source_bytes_for: CorrectionOutputBytesLookup,
        lock_context_for: LockContextFactory,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not callable(source_bytes_for):
            raise TypeError("source_bytes_for must be callable")
        if not callable(lock_context_for):
            raise TypeError("lock_context_for must be callable")
        self._write_set = write_set
        self._source_bytes_for = source_bytes_for
        self._lock_context_for = lock_context_for
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except WriteSetError as exc:
                raise _repository_error(
                    "the OCR proposal repository could not recover",
                    code="correction_ocr_recovery_failed",
                    cause=exc,
                    retryable=True,
                ) from exc
            except Exception as exc:
                raise _repository_error(
                    "the OCR proposal authority lock is unavailable",
                    code="correction_ocr_authority_unavailable",
                    cause=exc,
                    retryable=True,
                ) from exc

    def read_source(self, request: OcrFollowupRequest) -> bytes:
        self._require_request(request)
        try:
            # The resolver owns the source authority and returns an immutable,
            # checksum-pinned snapshot. Acquiring this repository's proposal
            # lock around it would nest the host's non-reentrant catalogue
            # lock when the production transform store is used.
            value = self._source_bytes_for(
                request.item_id,
                request.operation_id,
                request.source,
            )
        except WriteSetError as exc:
            raise _repository_error(
                "the OCR proposal workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the corrected OCR rendition is unavailable",
                code="correction_ocr_source_unavailable",
                cause=exc,
                retryable=True,
            ) from exc
        if value is None:
            raise NotFoundError(
                "the corrected OCR rendition does not exist",
                code="correction_ocr_source_not_found",
                details={"artifact_id": request.source.artifact_id},
            )
        if not isinstance(value, bytes):
            raise _repository_error(
                "the corrected OCR rendition is invalid",
                code="invalid_correction_ocr_source",
            )
        if len(value) > CORRECTION_OCR_MAX_SOURCE_BYTES:
            raise _repository_error(
                "the corrected OCR rendition exceeds its size budget",
                code="correction_ocr_source_too_large",
            )
        if hashlib.sha256(value).hexdigest() != request.source.content_sha256:
            raise _repository_error(
                "the corrected OCR rendition checksum changed",
                code="correction_ocr_source_checksum_mismatch",
            )
        return value

    def find_proposal(
        self,
        request: OcrFollowupRequest,
    ) -> StoredCorrectionOcrProposal | None:
        self._require_request(request)
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    return self._find_locked(request)
        except WriteSetError as exc:
            raise _repository_error(
                "the OCR proposal workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except (ConflictError, RepositoryError):
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the OCR proposal cannot be read",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc

    def get_proposal(
        self,
        item_id: str,
        proposal_ref: str,
    ) -> CorrectionOcrProposalView | None:
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 256
        ):
            raise ValueError("item_id must be a bounded identifier")
        self._require_proposal_ref(proposal_ref)
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    loaded = self._load_locked(proposal_ref)
                    if loaded is None or loaded.view.item_id != item_id:
                        return None
                    return loaded.view
        except WriteSetError as exc:
            raise _repository_error(
                "the OCR proposal workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except RepositoryError:
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the OCR proposal cannot be read",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc

    def list_proposals(
        self,
        item_id: str,
    ) -> CorrectionOcrProposalCatalogSnapshot:
        """Enumerate one verified item snapshot without creating identifiers."""

        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 256
        ):
            raise ValueError("item_id must be a bounded identifier")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    return self._list_proposals_locked(item_id)
        except WriteSetError as exc:
            raise _repository_error(
                "the OCR proposal workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=True,
            ) from exc
        except RepositoryError:
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the OCR proposal catalog cannot be read",
                code="invalid_correction_ocr_proposal_catalog",
                cause=exc,
            ) from exc

    def commit_proposal(
        self,
        request: OcrFollowupRequest,
        recognition: CorrectionOcrRecognition,
    ) -> StoredCorrectionOcrProposal:
        self._require_request(request)
        if not isinstance(recognition, CorrectionOcrRecognition):
            raise TypeError("recognition must be a CorrectionOcrRecognition")
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    existing = self._find_locked(request)
                    if existing is not None:
                        return existing
                    stored = StoredCorrectionOcrProposal(
                        self.proposal_ref_for(request),
                        request.source,
                        CorrectionOcrProviderSelection(
                            recognition.provider_id,
                            recognition.model,
                            recognition.options,
                        ),
                    )
                    document = {
                        "schema": CORRECTION_OCR_PROPOSAL_SCHEMA,
                        "version": CORRECTION_OCR_PROPOSAL_VERSION,
                        "proposal_ref": stored.proposal_ref,
                        "operation_id": request.operation_id,
                        "item_id": request.item_id,
                        "source": request.source.as_dict(),
                        "provider": stored.provider.as_dict(),
                        "recognition": recognition.as_dict(),
                        "publication_policy": CORRECTION_OCR_PROPOSAL_POLICY,
                    }
                    payload = _canonical_json(document)
                    if len(payload) > _MAX_PROPOSAL_BYTES:
                        raise _repository_error(
                            "the OCR proposal exceeds its size budget",
                            code="invalid_correction_ocr_proposal",
                        )
                    proposal_sha256 = hashlib.sha256(payload).hexdigest()
                    try:
                        CorrectionOcrProposalView(
                            proposal_ref=stored.proposal_ref,
                            item_id=request.item_id,
                            operation_id=request.operation_id,
                            source=stored.source,
                            provider=CorrectionOcrProposalProviderView(
                                stored.provider.provider_id,
                                stored.provider.model,
                            ),
                            recognition=recognition.payload,
                            publication_policy=CORRECTION_OCR_PROPOSAL_POLICY,
                            content_sha256=proposal_sha256,
                        )
                    except (TypeError, ValueError) as exc:
                        raise _repository_error(
                            "the OCR proposal contains private or invalid "
                            "public metadata",
                            code="invalid_correction_ocr_proposal",
                            cause=exc,
                        ) from exc
                    receipt = {
                        "schema": CORRECTION_OCR_PROPOSAL_RECEIPT_SCHEMA,
                        "version": CORRECTION_OCR_PROPOSAL_RECEIPT_VERSION,
                        "proposal_ref": stored.proposal_ref,
                        "operation_id": request.operation_id,
                        "item_id": request.item_id,
                        "source_sha256": request.source.content_sha256,
                        "proposal_sha256": proposal_sha256,
                    }
                    receipt_payload = _canonical_json(receipt)
                    if len(receipt_payload) > _MAX_RECEIPT_BYTES:
                        raise _repository_error(
                            "the OCR proposal receipt exceeds its size budget",
                            code="invalid_correction_ocr_proposal",
                        )
                    operation = {
                        "schema": _OPERATION_SCHEMA,
                        "version": _OPERATION_VERSION,
                        "proposal_ref": stored.proposal_ref,
                        "operation_id": request.operation_id,
                        "item_id": request.item_id,
                    }
                    operation_payload = _canonical_json(operation)
                    proposal_path = self._proposal_path(stored.proposal_ref)
                    receipt_path = self._receipt_path(stored.proposal_ref)
                    operation_path = self._operation_path(
                        request.operation_id
                    )
                    if (
                        self._path_exists(proposal_path)
                        or self._path_exists(receipt_path)
                        or self._path_exists(operation_path)
                    ):
                        raise ConflictError(
                            "the OCR proposal operation already exists",
                            code="correction_ocr_operation_conflict",
                            details={"operation_id": request.operation_id},
                        )
                    transaction = self._write_set.begin(
                        operation_id=request.operation_id,
                        scope="correction-ocr-proposal",
                        metadata={
                            "item_id": request.item_id,
                            "source_artifact_id": request.source.artifact_id,
                            "source_sha256": request.source.content_sha256,
                        },
                    )
                    transaction.stage_write(
                        self._relative(proposal_path),
                        payload,
                    )
                    transaction.stage_write(
                        self._relative(receipt_path),
                        receipt_payload,
                    )
                    transaction.stage_write(
                        self._relative(operation_path),
                        operation_payload,
                    )
                    transaction.commit(receipt=stored.as_dict())
                    return stored
        except WriteSetError as exc:
            raise _repository_error(
                "the OCR proposal transaction failed",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except (ConflictError, RepositoryError):
            raise
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the OCR proposal transaction failed",
                code="correction_ocr_transaction_failed",
                cause=exc,
                retryable=True,
            ) from exc

    @staticmethod
    def proposal_ref_for(request: OcrFollowupRequest) -> str:
        payload = _canonical_json(request.as_dict())
        return "cop-" + hashlib.sha256(payload).hexdigest()[:40]

    def _list_proposals_locked(
        self,
        item_id: str,
    ) -> CorrectionOcrProposalCatalogSnapshot:
        budget = _CatalogScanBudget()
        proposal_root = self._target(_PROPOSAL_ROOT)
        receipt_root = self._target(_RECEIPT_ROOT)
        proposal_state = self._catalog_directory_state(proposal_root)
        receipt_state = self._catalog_directory_state(receipt_root)
        proposal_refs = self._scan_catalog_directory(
            proposal_root,
            expected_state=proposal_state,
            budget=budget,
        )
        receipt_refs = self._scan_catalog_directory(
            receipt_root,
            expected_state=receipt_state,
            budget=budget,
        )
        self._require_catalog_directory_state(
            proposal_root,
            proposal_state,
        )
        self._require_catalog_directory_state(
            receipt_root,
            receipt_state,
        )
        if proposal_refs != receipt_refs:
            raise _repository_error(
                "the OCR proposal catalog contains an incomplete publication",
                code="invalid_correction_ocr_proposal_catalog",
            )
        budget.set_proposal_count(len(receipt_refs))

        proposals: list[CorrectionOcrProposalSummaryView] = []
        for proposal_ref in sorted(receipt_refs):
            loaded = self._load_locked(
                proposal_ref,
                catalog_budget=budget,
            )
            if loaded is None:
                raise _repository_error(
                    "the OCR proposal catalog changed while it was read",
                    code="invalid_correction_ocr_proposal_catalog",
                )
            if loaded.view.item_id == item_id:
                proposals.append(
                    CorrectionOcrProposalSummaryView.from_proposal(loaded.view)
                )

        self._require_catalog_directory_state(
            proposal_root,
            proposal_state,
        )
        self._require_catalog_directory_state(
            receipt_root,
            receipt_state,
        )
        return CorrectionOcrProposalCatalogSnapshot(
            item_id=item_id,
            proposals=tuple(proposals),
        )

    def _scan_catalog_directory(
        self,
        path: Path,
        *,
        expected_state: tuple[int, ...] | None,
        budget: _CatalogScanBudget,
    ) -> set[str]:
        if expected_state is None:
            return set()
        self._require_catalog_directory_state(path, expected_state)
        references: set[str] = set()
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    budget.consume_directory_entry()
                    if not entry.name.endswith(".json"):
                        raise ValueError(
                            "OCR proposal catalog entry has an invalid name"
                        )
                    proposal_ref = entry.name.removesuffix(".json")
                    self._require_proposal_ref(proposal_ref)
                    if entry.name != f"{proposal_ref}.json":
                        raise ValueError(
                            "OCR proposal catalog entry has an invalid name"
                        )
                    entry_path = self._safe_target(Path(entry.path))
                    # ``DirEntry.stat`` reports ``st_nlink == 0`` on some
                    # Windows/Python combinations.  The named-path snapshot is
                    # the same primitive used by the verified file reader.
                    info = entry_path.lstat()
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or _is_redirecting_path(entry_path)
                    ):
                        raise ValueError(
                            "OCR proposal catalog entry is not a private file"
                        )
                    if proposal_ref in references:
                        raise ValueError(
                            "OCR proposal catalog contains a duplicate reference"
                        )
                    references.add(proposal_ref)
            self._require_catalog_directory_state(path, expected_state)
            return references
        except RepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "the OCR proposal catalog cannot be enumerated safely",
                code="invalid_correction_ocr_proposal_catalog",
                cause=exc,
            ) from exc

    def _catalog_directory_state(
        self,
        path: Path,
    ) -> tuple[int, ...] | None:
        try:
            self._authority_snapshot(path / ".catalog-snapshot")
            if not os.path.lexists(path):
                return None
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or _is_redirecting_path(path)
            ):
                raise ValueError(
                    "OCR proposal catalog authority is not a private directory"
                )
            return _stable_stat_identity(info)
        except RepositoryError:
            raise
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "the OCR proposal catalog authority is unsafe",
                code="invalid_correction_ocr_proposal_catalog",
                cause=exc,
            ) from exc

    def _require_catalog_directory_state(
        self,
        path: Path,
        expected: tuple[int, ...] | None,
    ) -> None:
        if self._catalog_directory_state(path) != expected:
            raise _repository_error(
                "the OCR proposal catalog changed while it was read",
                code="invalid_correction_ocr_proposal_catalog",
            )

    def _find_locked(
        self,
        request: OcrFollowupRequest,
    ) -> StoredCorrectionOcrProposal | None:
        expected_ref = self.proposal_ref_for(request)
        operation_path = self._operation_path(request.operation_id)
        operation_exists = self._path_exists(operation_path)
        proposal_exists = self._path_exists(self._proposal_path(expected_ref))
        receipt_exists = self._path_exists(self._receipt_path(expected_ref))
        if not operation_exists and not proposal_exists and not receipt_exists:
            return None
        if not operation_exists:
            raise _repository_error(
                "the OCR proposal publication is incomplete",
                code="invalid_correction_ocr_proposal",
            )
        operation = self._read_operation(operation_path)
        if (
            operation["operation_id"] != request.operation_id
            or operation["item_id"] != request.item_id
            or operation["proposal_ref"] != expected_ref
        ):
            raise ConflictError(
                "the OCR proposal operation belongs to another request",
                code="correction_ocr_operation_conflict",
                details={"operation_id": request.operation_id},
            )
        loaded = self._load_locked(expected_ref)
        if loaded is None:
            raise _repository_error(
                "the OCR proposal publication is incomplete",
                code="invalid_correction_ocr_proposal",
            )
        if (
            loaded.view.operation_id != request.operation_id
            or loaded.view.item_id != request.item_id
            or loaded.view.source != request.source
        ):
            raise ConflictError(
                "the OCR proposal operation belongs to another request",
                code="correction_ocr_operation_conflict",
                details={"operation_id": request.operation_id},
            )
        return loaded.stored

    def _load_locked(
        self,
        proposal_ref: str,
        *,
        catalog_budget: _CatalogScanBudget | None = None,
    ) -> _LoadedCorrectionOcrProposal | None:
        self._require_proposal_ref(proposal_ref)
        proposal_path = self._proposal_path(proposal_ref)
        receipt_path = self._receipt_path(proposal_ref)
        proposal_exists = self._path_exists(proposal_path)
        receipt_exists = self._path_exists(receipt_path)
        if not proposal_exists and not receipt_exists:
            return None
        if proposal_exists != receipt_exists:
            raise _repository_error(
                "the OCR proposal publication is incomplete",
                code="invalid_correction_ocr_proposal",
            )
        payload = self._read_regular(
            proposal_path,
            maximum=_MAX_PROPOSAL_BYTES,
            catalog_budget=catalog_budget,
        )
        receipt_payload = self._read_regular(
            receipt_path,
            maximum=_MAX_RECEIPT_BYTES,
            catalog_budget=catalog_budget,
        )
        try:
            raw_receipt = json.loads(
                receipt_payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
            receipt = _strict_object(
                raw_receipt,
                fields=_RECEIPT_FIELDS,
                name="OCR proposal receipt",
            )
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
            document = _strict_object(
                raw,
                fields=_DOCUMENT_FIELDS,
                name="OCR proposal",
            )
            source = _source_from_dict(document["source"])
            provider = _provider_from_dict(document["provider"])
            recognition_raw = _strict_object(
                document["recognition"],
                fields=_RECOGNITION_FIELDS,
                name="OCR recognition",
            )
            recognition = CorrectionOcrRecognition(
                recognition_raw["provider_id"],
                recognition_raw["model"],
                recognition_raw["payload"],
                recognition_raw["options"],
            )
            stored = StoredCorrectionOcrProposal(
                document["proposal_ref"],
                source,
                provider,
            )
            bound_request = OcrFollowupRequest(
                document["operation_id"],
                document["item_id"],
                source,
            )
            operation_path = self._operation_path(
                bound_request.operation_id
            )
            if not self._path_exists(operation_path):
                raise ValueError("OCR proposal operation claim is missing")
            operation = self._read_operation(
                operation_path,
                catalog_budget=catalog_budget,
            )
            proposal_sha256 = hashlib.sha256(payload).hexdigest()
            if (
                document["schema"] != CORRECTION_OCR_PROPOSAL_SCHEMA
                or type(document["version"]) is not int
                or document["version"] != CORRECTION_OCR_PROPOSAL_VERSION
                or receipt["schema"]
                != CORRECTION_OCR_PROPOSAL_RECEIPT_SCHEMA
                or type(receipt["version"]) is not int
                or receipt["version"]
                != CORRECTION_OCR_PROPOSAL_RECEIPT_VERSION
                or document["publication_policy"]
                != CORRECTION_OCR_PROPOSAL_POLICY
                or stored.proposal_ref != proposal_ref
                or stored.proposal_ref != self.proposal_ref_for(bound_request)
                or receipt["proposal_ref"] != stored.proposal_ref
                or receipt["proposal_sha256"]
                != proposal_sha256
                or receipt["operation_id"] != bound_request.operation_id
                or receipt["item_id"] != bound_request.item_id
                or receipt["source_sha256"] != source.content_sha256
                or operation["proposal_ref"] != stored.proposal_ref
                or operation["operation_id"] != bound_request.operation_id
                or operation["item_id"] != bound_request.item_id
                or recognition.provider_id != provider.provider_id
                or recognition.model != provider.model
                or recognition.options != provider.options
                or payload != _canonical_json(document)
                or receipt_payload != _canonical_json(receipt)
            ):
                raise ValueError("OCR proposal is not bound to its request")
            return _LoadedCorrectionOcrProposal(
                stored=stored,
                view=CorrectionOcrProposalView(
                    proposal_ref=stored.proposal_ref,
                    item_id=bound_request.item_id,
                    operation_id=bound_request.operation_id,
                    source=stored.source,
                    provider=CorrectionOcrProposalProviderView(
                        stored.provider.provider_id,
                        stored.provider.model,
                    ),
                    recognition=recognition.payload,
                    publication_policy=document["publication_policy"],
                    content_sha256=proposal_sha256,
                ),
            )
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise _repository_error(
                "the OCR proposal is invalid",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc

    def _read_operation(
        self,
        path: Path,
        *,
        catalog_budget: _CatalogScanBudget | None = None,
    ) -> Mapping[str, Any]:
        payload = self._read_regular(
            path,
            maximum=_MAX_RECEIPT_BYTES,
            catalog_budget=catalog_budget,
        )
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {value}")
                ),
            )
            operation = _strict_object(
                raw,
                fields=_OPERATION_FIELDS,
                name="OCR proposal operation",
            )
            if (
                operation["schema"] != _OPERATION_SCHEMA
                or type(operation["version"]) is not int
                or operation["version"] != _OPERATION_VERSION
                or payload != _canonical_json(operation)
            ):
                raise ValueError("OCR proposal operation claim is invalid")
            self._require_proposal_ref(operation["proposal_ref"])
            return operation
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise _repository_error(
                "the OCR proposal operation claim is invalid",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc

    @staticmethod
    def _require_request(request: OcrFollowupRequest) -> None:
        if not isinstance(request, OcrFollowupRequest):
            raise TypeError("request must be an OcrFollowupRequest")

    @staticmethod
    def _require_proposal_ref(proposal_ref: object) -> None:
        if (
            not isinstance(proposal_ref, str)
            or len(proposal_ref) != 44
            or not proposal_ref.startswith("cop-")
            or any(value not in "0123456789abcdef" for value in proposal_ref[4:])
        ):
            raise ValueError(
                "proposal_ref must be an opaque OCR proposal reference"
            )

    def _read_regular(
        self,
        path: Path,
        *,
        maximum: int,
        catalog_budget: _CatalogScanBudget | None = None,
    ) -> bytes:
        descriptor = -1
        try:
            if catalog_budget is not None:
                catalog_budget.start_json_document()
            authority = self._authority_snapshot(path)
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or _is_redirecting_path(path)
            ):
                raise ValueError("OCR proposal is not a private regular file")
            descriptor, opened = _open_verified_regular(
                path,
                before,
                authority=authority,
            )
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                total += len(block)
                if total > maximum:
                    raise ValueError(
                        "OCR proposal document exceeds its size budget"
                    )
                if catalog_budget is not None:
                    catalog_budget.consume_json_bytes(len(block))
                chunks.append(block)
            _finish_verified_regular(
                path,
                descriptor,
                named_before=before,
                opened_before=opened,
            )
            self._authority_snapshot(path)
            return b"".join(chunks)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "the OCR proposal cannot be read safely",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _path_exists(self, path: Path) -> bool:
        self._authority_snapshot(path)
        if not os.path.lexists(path):
            return False
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise _repository_error(
                "the OCR proposal target cannot be inspected",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _is_redirecting_path(path)
        ):
            raise _repository_error(
                "the OCR proposal target is unsafe",
                code="invalid_correction_ocr_proposal",
            )
        return True

    def _authority_snapshot(self, path: Path) -> _AuthoritySnapshot:
        target = self._safe_target(path)
        root = self._write_set.root
        relative = target.relative_to(root)
        try:
            named_root = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise _repository_error(
                "the OCR proposal authority root cannot be inspected",
                code="unsafe_correction_ocr_path",
                cause=exc,
            ) from exc
        if _is_redirecting_path(root) or not stat.S_ISDIR(named_root.st_mode):
            raise _repository_error(
                "the OCR proposal authority root is unsafe",
                code="unsafe_correction_ocr_path",
            )

        directories: list[_AuthorityDirectorySnapshot] = []
        current = root
        for part in relative.parts[:-1]:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "the OCR proposal target crosses a redirecting path",
                    code="unsafe_correction_ocr_path",
                )
            try:
                named_directory = current.lstat()
            except FileNotFoundError:
                named_directory = None
            except OSError as exc:
                raise _repository_error(
                    "the OCR proposal authority path cannot be inspected",
                    code="unsafe_correction_ocr_path",
                    cause=exc,
                ) from exc
            if (
                named_directory is not None
                and not stat.S_ISDIR(named_directory.st_mode)
            ):
                raise _repository_error(
                    "an OCR proposal authority component is not a directory",
                    code="unsafe_correction_ocr_path",
                )
            directories.append(
                _AuthorityDirectorySnapshot(current, named_directory)
            )

        try:
            target.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "the OCR proposal target escapes its workspace",
                code="unsafe_correction_ocr_path",
                cause=exc,
            ) from exc
        return _AuthoritySnapshot(root, named_root, tuple(directories))

    def _safe_target(self, path: Path) -> Path:
        target = Path(path)
        try:
            relative = target.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                "the OCR proposal target escapes its workspace",
                code="unsafe_correction_ocr_path",
                cause=exc,
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise _repository_error(
                    "the OCR proposal target is unsafe",
                    code="unsafe_correction_ocr_path",
                )
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "the OCR proposal target crosses a redirecting path",
                    code="unsafe_correction_ocr_path",
                )
        return target

    def _proposal_path(self, proposal_ref: str) -> Path:
        self._require_proposal_ref(proposal_ref)
        return self._target(_PROPOSAL_ROOT / f"{proposal_ref}.json")

    def _receipt_path(self, proposal_ref: str) -> Path:
        self._require_proposal_ref(proposal_ref)
        return self._target(_RECEIPT_ROOT / f"{proposal_ref}.json")

    def _operation_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._target(_OPERATION_ROOT / f"{digest}.json")

    def _target(self, relative: PurePosixPath) -> Path:
        return self._write_set.root.joinpath(*relative.parts)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._write_set.root).as_posix()

__all__ = [
    "CORRECTION_OCR_PROPOSAL_SCHEMA",
    "CORRECTION_OCR_PROPOSAL_VERSION",
    "CORRECTION_OCR_PROPOSAL_RECEIPT_SCHEMA",
    "CORRECTION_OCR_PROPOSAL_RECEIPT_VERSION",
    "CorrectionOutputBytesLookup",
    "FilesystemCorrectionOcrProposalRepository",
]
