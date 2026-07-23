"""Recoverable filesystem persistence for capture-to-lib associations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from ...engine.capture_archives import (
    AssociateCaptureArchiveCommand,
    CaptureArchiveAssociation,
    CaptureArchiveDisposition,
    CaptureArchivePublication,
    CaptureArchiveReceipt,
    CaptureArchiveResult,
    capture_book_id,
)
from ...engine.errors import ConflictError, EngineError, RepositoryError
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


_ASSOCIATION_ROOT = PurePosixPath(".engine/capture-lib/associations")
_RECEIPT_ROOT = PurePosixPath(".engine/receipts/capture-lib")
_ARCHIVE_ROOT = PurePosixPath(".engine/capture-lib/objects")
_MAX_DOCUMENT_BYTES = 128 * 1024
_MAX_ARCHIVE_BYTES = 250 * 1024 * 1024


def _canonical_json(value: Any, *, artifact: str) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise RepositoryError(
            f"{artifact} is not portable JSON",
            code="invalid_capture_archive_storage",
            details={"artifact": artifact},
        ) from exc
    if len(payload) > _MAX_DOCUMENT_BYTES:
        raise RepositoryError(
            f"{artifact} is too large",
            code="invalid_capture_archive_storage",
            details={
                "artifact": artifact,
                "maximum_bytes": _MAX_DOCUMENT_BYTES,
            },
        )
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _strict_json(payload: bytes, *, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number {token}")
            ),
        )
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise RepositoryError(
            f"{artifact} is invalid JSON",
            code="invalid_capture_archive_storage",
            details={"artifact": artifact},
        ) from exc
    if not isinstance(value, dict):
        raise RepositoryError(
            f"{artifact} must be an object",
            code="invalid_capture_archive_storage",
            details={"artifact": artifact},
        )
    if payload != _canonical_json(value, artifact=artifact):
        raise RepositoryError(
            f"{artifact} is not canonical JSON",
            code="invalid_capture_archive_storage",
            details={"artifact": artifact},
        )
    return value


def _repository_failure(
    message: str,
    *,
    code: str,
    cause: Exception,
    retryable: bool,
) -> RepositoryError:
    return RepositoryError(
        message,
        code=code,
        details={"cause": type(cause).__name__},
        retryable=retryable,
    )


class FilesystemCaptureArchiveRepository:
    """Publish archive bytes, association sidecar, and receipt atomically."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        self._write_set = write_set
        if recover:
            try:
                with write_set.recovery_lease():
                    write_set.recover_all()
            except WriteSetError as exc:
                raise _repository_failure(
                    "the capture archive repository could not recover",
                    code="capture_archive_recovery_failed",
                    cause=exc,
                    retryable=True,
                ) from exc

    def replay(
        self,
        command: AssociateCaptureArchiveCommand,
    ) -> CaptureArchiveResult | None:
        if not isinstance(command, AssociateCaptureArchiveCommand):
            raise TypeError("command must be an AssociateCaptureArchiveCommand")
        try:
            with self._write_set.workspace_lease():
                receipt = self._read_receipt(command.operation_id)
                if receipt is None:
                    return None
                if receipt.command_sha256 != command.fingerprint:
                    raise ConflictError(
                        "capture archive operation was reused for another source",
                        code="capture_archive_operation_conflict",
                        details={"operation_id": command.operation_id},
                    )
                self._validate_receipt_authority(receipt)
                return CaptureArchiveResult(receipt, replayed=True)
        except (ConflictError, RepositoryError):
            raise
        except WriteSetError as exc:
            raise _repository_failure(
                "the capture archive workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_failure(
                "the capture archive receipt could not be loaded",
                code="capture_archive_repository_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def publish(
        self,
        publication: CaptureArchivePublication,
    ) -> CaptureArchiveResult:
        if not isinstance(publication, CaptureArchivePublication):
            raise TypeError("publication must be a CaptureArchivePublication")
        try:
            with self._write_set.workspace_lease():
                prior = self._read_receipt(publication.operation_id)
                if prior is not None:
                    if prior.command_sha256 != publication.command_sha256:
                        raise ConflictError(
                            "capture archive operation was reused for another source",
                            code="capture_archive_operation_conflict",
                            details={"operation_id": publication.operation_id},
                        )
                    self._validate_receipt_authority(prior)
                    return CaptureArchiveResult(prior, replayed=True)

                existing = self._read_association(publication.capture_id)
                if existing is not None:
                    self._validate_existing_source(
                        capture_id=publication.capture_id,
                        book_id=publication.book_id,
                        source_revision=publication.source_revision,
                        source_fingerprint=publication.source_fingerprint,
                        existing=existing,
                    )
                    receipt = CaptureArchiveReceipt(
                        operation_id=publication.operation_id,
                        command_sha256=publication.command_sha256,
                        disposition=CaptureArchiveDisposition.EXISTING,
                        association=existing,
                    )
                    self._publish_receipt(receipt)
                    return CaptureArchiveResult(receipt, replayed=False)

                if len(publication.archive) > _MAX_ARCHIVE_BYTES:
                    raise RepositoryError(
                        "the capture archive is too large",
                        code="capture_archive_too_large",
                        details={"maximum_bytes": _MAX_ARCHIVE_BYTES},
                    )
                if publication.book_id != capture_book_id(publication.capture_id):
                    raise RepositoryError(
                        "the capture publication has a non-canonical book identity",
                        code="invalid_capture_archive_publication",
                    )
                association = publication.association
                archive_path = self._archive_path(association.archive_sha256)
                association_path = self._association_path(publication.capture_id)
                receipt_path = self._receipt_path(publication.operation_id)
                for path, artifact in (
                    (archive_path, "capture_archive"),
                    (association_path, "capture_archive_association"),
                    (receipt_path, "capture_archive_receipt"),
                ):
                    if self._path_exists(path, artifact=artifact):
                        raise RepositoryError(
                            "an immutable capture archive target already exists",
                            code="capture_archive_target_exists",
                            details={"artifact": artifact},
                        )
                self._validate_archive_payload(
                    association,
                    publication.archive,
                )
                receipt = CaptureArchiveReceipt(
                    operation_id=publication.operation_id,
                    command_sha256=publication.command_sha256,
                    disposition=CaptureArchiveDisposition.CREATED,
                    association=association,
                )
                transaction = self._write_set.begin(
                    operation_id=publication.operation_id,
                    scope="capture-lib-association",
                    metadata={
                        "capture_id": publication.capture_id,
                        "book_id": publication.book_id,
                        "command_sha256": publication.command_sha256,
                    },
                )
                transaction.stage_write(
                    self._relative(archive_path),
                    publication.archive,
                )
                transaction.stage_write(
                    self._relative(association_path),
                    _canonical_json(
                        association.as_dict(),
                        artifact="capture_archive_association",
                    ),
                )
                transaction.stage_write(
                    self._relative(receipt_path),
                    _canonical_json(
                        receipt.as_dict(),
                        artifact="capture_archive_receipt",
                    ),
                )
                transaction.commit(receipt=receipt.as_dict())
                return CaptureArchiveResult(receipt, replayed=False)
        except (ConflictError, RepositoryError):
            raise
        except WriteSetError as exc:
            raise _repository_failure(
                "the capture archive transaction failed",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_failure(
                "the capture archive transaction failed",
                code="capture_archive_transaction_failed",
                cause=exc,
                retryable=True,
            ) from exc

    def bind_existing(
        self,
        command: AssociateCaptureArchiveCommand,
    ) -> CaptureArchiveResult | None:
        if not isinstance(command, AssociateCaptureArchiveCommand):
            raise TypeError("command must be an AssociateCaptureArchiveCommand")
        try:
            with self._write_set.workspace_lease():
                prior = self._read_receipt(command.operation_id)
                if prior is not None:
                    if prior.command_sha256 != command.fingerprint:
                        raise ConflictError(
                            "capture archive operation was reused for another source",
                            code="capture_archive_operation_conflict",
                            details={"operation_id": command.operation_id},
                        )
                    self._validate_receipt_authority(prior)
                    return CaptureArchiveResult(prior, replayed=True)

                existing = self._read_association(command.source.capture_id)
                if existing is None:
                    return None
                self._validate_existing_source(
                    capture_id=command.source.capture_id,
                    book_id=capture_book_id(command.source.capture_id),
                    source_revision=command.source.source_revision,
                    source_fingerprint=command.source.fingerprint,
                    existing=existing,
                )
                receipt = CaptureArchiveReceipt(
                    operation_id=command.operation_id,
                    command_sha256=command.fingerprint,
                    disposition=CaptureArchiveDisposition.EXISTING,
                    association=existing,
                )
                self._publish_receipt(receipt)
                return CaptureArchiveResult(receipt, replayed=False)
        except (ConflictError, RepositoryError):
            raise
        except WriteSetError as exc:
            raise _repository_failure(
                "the capture archive workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_failure(
                "the existing capture archive could not be associated",
                code="capture_archive_repository_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def get(self, capture_id: str) -> CaptureArchiveAssociation | None:
        capture_book_id(capture_id)
        try:
            with self._write_set.workspace_lease():
                association = self._read_association(capture_id)
                if association is not None:
                    self._validate_archive(association)
                return association
        except RepositoryError:
            raise
        except WriteSetError as exc:
            raise _repository_failure(
                "the capture archive workspace is unavailable",
                code=exc.code,
                cause=exc,
                retryable=exc.retryable,
            ) from exc
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_failure(
                "the capture archive association could not be loaded",
                code="capture_archive_repository_unavailable",
                cause=exc,
                retryable=True,
            ) from exc

    def _validate_existing_source(
        self,
        *,
        capture_id: str,
        book_id: str,
        source_revision: str,
        source_fingerprint: str,
        existing: CaptureArchiveAssociation,
    ) -> None:
        if existing.book_id != book_id:
            raise RepositoryError(
                "the capture association has a non-canonical book identity",
                code="invalid_capture_archive_storage",
            )
        if existing.source_revision != source_revision:
            raise ConflictError(
                "the capture already has an archive for another source revision",
                code="capture_archive_reseal_required",
                details={
                    "capture_id": capture_id,
                    "current_source_revision": existing.source_revision,
                    "requested_source_revision": source_revision,
                },
            )
        if existing.source_fingerprint != source_fingerprint:
            raise ConflictError(
                "the capture source changed without advancing its revision",
                code="capture_source_revision_conflict",
                details={
                    "capture_id": capture_id,
                    "source_revision": source_revision,
                },
            )
        self._validate_archive(existing)

    def _publish_receipt(self, receipt: CaptureArchiveReceipt) -> None:
        path = self._receipt_path(receipt.operation_id)
        if self._path_exists(path, artifact="capture_archive_receipt"):
            raise RepositoryError(
                "an immutable capture archive receipt already exists",
                code="capture_archive_target_exists",
                details={"artifact": "capture_archive_receipt"},
            )
        transaction = self._write_set.begin(
            operation_id=receipt.operation_id,
            scope="capture-lib-receipt",
            metadata={
                "capture_id": receipt.association.capture_id,
                "book_id": receipt.association.book_id,
                "command_sha256": receipt.command_sha256,
            },
        )
        transaction.stage_write(
            self._relative(path),
            _canonical_json(
                receipt.as_dict(),
                artifact="capture_archive_receipt",
            ),
        )
        transaction.commit(receipt=receipt.as_dict())

    def _validate_receipt_authority(
        self,
        receipt: CaptureArchiveReceipt,
    ) -> None:
        association = self._read_association(receipt.association.capture_id)
        if association is None or association != receipt.association:
            raise RepositoryError(
                "the capture archive receipt is not bound to its association",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive_receipt"},
            )
        self._validate_archive(association)

    def _validate_archive(
        self,
        association: CaptureArchiveAssociation,
    ) -> None:
        if association.archive_bytes > _MAX_ARCHIVE_BYTES:
            raise RepositoryError(
                "the associated capture archive is too large",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive"},
            )
        payload = self._read_regular(
            self._archive_path(association.archive_sha256),
            maximum=_MAX_ARCHIVE_BYTES,
            artifact="capture_archive",
        )
        self._validate_archive_payload(association, payload)

    @staticmethod
    def _validate_archive_payload(
        association: CaptureArchiveAssociation,
        payload: bytes,
    ) -> None:
        if (
            len(payload) != association.archive_bytes
            or hashlib.sha256(payload).hexdigest() != association.archive_sha256
        ):
            raise RepositoryError(
                "the associated capture archive checksum is invalid",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive"},
            )
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                book_members = [
                    info for info in archive.infolist() if info.filename == "book.json"
                ]
                if len(book_members) != 1:
                    raise ValueError("book.json must exist exactly once")
                book_info = book_members[0]
                if (
                    book_info.is_dir()
                    or book_info.flag_bits & 0x1
                    or book_info.file_size > 10 * 1024 * 1024
                ):
                    raise ValueError("book.json member is unsafe")
                book = json.loads(
                    archive.read(book_info).decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=lambda token: (_ for _ in ()).throw(
                        ValueError(f"non-finite number {token}")
                    ),
                )
        except (
            RecursionError,
            UnicodeError,
            ValueError,
            zipfile.BadZipFile,
            RuntimeError,
        ) as exc:
            raise RepositoryError(
                "the associated capture archive envelope is invalid",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive"},
            ) from exc
        if (
            not isinstance(book, dict)
            or book.get("format_version") != association.format_version
            or book.get("book_id") != association.book_id
        ):
            raise RepositoryError(
                "the associated capture archive has another identity",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive"},
            )

    def _read_association(
        self,
        capture_id: str,
    ) -> CaptureArchiveAssociation | None:
        path = self._association_path(capture_id)
        if not self._path_exists(
            path,
            artifact="capture_archive_association",
        ):
            return None
        raw = _strict_json(
            self._read_regular(
                path,
                maximum=_MAX_DOCUMENT_BYTES,
                artifact="capture_archive_association",
            ),
            artifact="capture_archive_association",
        )
        try:
            association = CaptureArchiveAssociation.from_dict(raw)
        except Exception as exc:
            raise RepositoryError(
                "the capture archive association is invalid",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive_association"},
            ) from exc
        if association.capture_id != capture_id:
            raise RepositoryError(
                "the capture archive association belongs to another capture",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive_association"},
            )
        if association.book_id != capture_book_id(capture_id):
            raise RepositoryError(
                "the capture archive association has a non-canonical book identity",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive_association"},
            )
        return association

    def _read_receipt(
        self,
        operation_id: str,
    ) -> CaptureArchiveReceipt | None:
        path = self._receipt_path(operation_id)
        if not self._path_exists(path, artifact="capture_archive_receipt"):
            return None
        raw = _strict_json(
            self._read_regular(
                path,
                maximum=_MAX_DOCUMENT_BYTES,
                artifact="capture_archive_receipt",
            ),
            artifact="capture_archive_receipt",
        )
        try:
            receipt = CaptureArchiveReceipt.from_dict(raw)
        except Exception as exc:
            raise RepositoryError(
                "the capture archive receipt is invalid",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive_receipt"},
            ) from exc
        if receipt.operation_id != operation_id:
            raise RepositoryError(
                "the capture archive receipt belongs to another operation",
                code="invalid_capture_archive_storage",
                details={"artifact": "capture_archive_receipt"},
            )
        return receipt

    def _path_exists(self, path: Path, *, artifact: str) -> bool:
        self._safe_target(path, artifact=artifact)
        if not os.path.lexists(path):
            return False
        try:
            info = path.lstat()
        except OSError as exc:
            raise _repository_failure(
                f"{artifact} could not be inspected",
                code="capture_archive_storage_unavailable",
                cause=exc,
                retryable=True,
            ) from exc
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _is_redirecting_path(path)
        ):
            raise RepositoryError(
                f"{artifact} is not an owned regular file",
                code="invalid_capture_archive_storage",
                details={"artifact": artifact},
            )
        return True

    def _read_regular(
        self,
        path: Path,
        *,
        maximum: int,
        artifact: str,
    ) -> bytes:
        descriptor = -1
        try:
            authority = self._authority_snapshot(path, artifact=artifact)
            named_before = path.lstat()
            if (
                not stat.S_ISREG(named_before.st_mode)
                or named_before.st_nlink != 1
                or _is_redirecting_path(path)
            ):
                raise ValueError("target is not a private regular file")
            descriptor, opened_before = _open_verified_regular(
                path,
                named_before,
                authority=authority,
            )
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise ValueError("target exceeds its encoded size budget")
            _finish_verified_regular(
                path,
                descriptor,
                named_before=named_before,
                opened_before=opened_before,
            )
            self._authority_snapshot(path, artifact=artifact)
            return b"".join(chunks)
        except RepositoryError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise RepositoryError(
                f"{artifact} cannot be read as a private regular file",
                code="invalid_capture_archive_storage",
                details={
                    "artifact": artifact,
                    "cause": type(exc).__name__,
                },
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _authority_snapshot(
        self,
        path: Path,
        *,
        artifact: str,
    ) -> _AuthoritySnapshot:
        target = self._safe_target(path, artifact=artifact)
        root = self._write_set.root
        relative = target.relative_to(root)
        try:
            named_root = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise _repository_failure(
                "the capture archive authority root cannot be inspected",
                code="invalid_capture_archive_storage",
                cause=exc,
                retryable=True,
            ) from exc
        if _is_redirecting_path(root) or not stat.S_ISDIR(named_root.st_mode):
            raise RepositoryError(
                "the capture archive authority root is unsafe",
                code="invalid_capture_archive_storage",
                details={"artifact": artifact},
            )

        directories: list[_AuthorityDirectorySnapshot] = []
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            if _is_redirecting_path(current):
                raise RepositoryError(
                    f"{artifact} crosses a redirecting filesystem path",
                    code="invalid_capture_archive_storage",
                    details={"artifact": artifact},
                )
            try:
                named_directory = current.lstat()
            except FileNotFoundError:
                named_directory = None
            except OSError as exc:
                raise _repository_failure(
                    f"{artifact} authority path cannot be inspected",
                    code="invalid_capture_archive_storage",
                    cause=exc,
                    retryable=True,
                ) from exc
            if named_directory is not None and not stat.S_ISDIR(
                named_directory.st_mode
            ):
                raise RepositoryError(
                    f"{artifact} authority component is not a directory",
                    code="invalid_capture_archive_storage",
                    details={"artifact": artifact},
                )
            directories.append(_AuthorityDirectorySnapshot(current, named_directory))
        try:
            target.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise RepositoryError(
                f"{artifact} escapes the capture archive workspace",
                code="invalid_capture_archive_storage",
                details={"artifact": artifact},
            ) from exc
        return _AuthoritySnapshot(root, named_root, tuple(directories))

    def _safe_target(self, path: Path, *, artifact: str) -> Path:
        target = Path(path)
        try:
            relative = target.relative_to(self._write_set.root)
        except ValueError as exc:
            raise RepositoryError(
                f"{artifact} escaped the capture archive root",
                code="invalid_capture_archive_storage",
                details={"artifact": artifact},
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise RepositoryError(
                    f"{artifact} has an unsafe storage target",
                    code="invalid_capture_archive_storage",
                    details={"artifact": artifact},
                )
            current = current / part
            if _is_redirecting_path(current):
                raise RepositoryError(
                    f"{artifact} redirects through a filesystem link",
                    code="invalid_capture_archive_storage",
                    details={"artifact": artifact},
                )
        return target

    def _association_path(self, capture_id: str) -> Path:
        digest = hashlib.sha256(capture_id.encode("utf-8")).hexdigest()
        return self._target(_ASSOCIATION_ROOT / f"{digest}.json")

    def _receipt_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._target(_RECEIPT_ROOT / f"{digest}.json")

    def _archive_path(self, archive_sha256: str) -> Path:
        return self._target(_ARCHIVE_ROOT / f"{archive_sha256}.lib")

    def _target(self, relative: PurePosixPath) -> Path:
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise RepositoryError(
                "capture archive storage target is invalid",
                code="invalid_capture_archive_storage",
            )
        return self._write_set.root.joinpath(*relative.parts)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._write_set.root).as_posix()


__all__ = ["FilesystemCaptureArchiveRepository"]
