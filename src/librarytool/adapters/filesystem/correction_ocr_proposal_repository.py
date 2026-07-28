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
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.correction_ocr import (
    CORRECTION_OCR_MAX_SOURCE_BYTES,
    CORRECTION_OCR_PROPOSAL_POLICY,
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
_MAX_PROPOSAL_BYTES = 128 * 1024 * 1024
_MAX_RECEIPT_BYTES = 64 * 1024
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
                    proposal_path = self._proposal_path(
                        request.operation_id
                    )
                    receipt_path = self._receipt_path(request.operation_id)
                    if (
                        self._path_exists(proposal_path)
                        or self._path_exists(receipt_path)
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

    def _find_locked(
        self,
        request: OcrFollowupRequest,
    ) -> StoredCorrectionOcrProposal | None:
        proposal_path = self._proposal_path(request.operation_id)
        receipt_path = self._receipt_path(request.operation_id)
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
        )
        receipt_payload = self._read_regular(
            receipt_path,
            maximum=_MAX_RECEIPT_BYTES,
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
            if (
                document["operation_id"] != request.operation_id
                or document["item_id"] != request.item_id
                or source != request.source
                or receipt["operation_id"] != request.operation_id
                or receipt["item_id"] != request.item_id
                or receipt["source_sha256"]
                != request.source.content_sha256
            ):
                raise ConflictError(
                    "the OCR proposal operation belongs to another request",
                    code="correction_ocr_operation_conflict",
                    details={"operation_id": request.operation_id},
                )
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
                or stored.proposal_ref != self.proposal_ref_for(request)
                or receipt["proposal_ref"] != stored.proposal_ref
                or receipt["proposal_sha256"]
                != hashlib.sha256(payload).hexdigest()
                or recognition.provider_id != provider.provider_id
                or recognition.model != provider.model
                or recognition.options != provider.options
                or payload != _canonical_json(document)
                or receipt_payload != _canonical_json(receipt)
            ):
                raise ValueError("OCR proposal is not bound to its request")
            return stored
        except ConflictError:
            raise
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise _repository_error(
                "the OCR proposal is invalid",
                code="invalid_correction_ocr_proposal",
                cause=exc,
            ) from exc

    @staticmethod
    def _require_request(request: OcrFollowupRequest) -> None:
        if not isinstance(request, OcrFollowupRequest):
            raise TypeError("request must be an OcrFollowupRequest")

    def _read_regular(self, path: Path, *, maximum: int) -> bytes:
        descriptor = -1
        try:
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

    def _proposal_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._target(_PROPOSAL_ROOT / f"{digest}.json")

    def _receipt_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._target(_RECEIPT_ROOT / f"{digest}.json")

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
