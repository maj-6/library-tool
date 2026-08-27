"""Atomic filesystem commit adapter for selected reviewed-book imports.

One invocation updates exactly one source reference.  The manual-entry mapping
or CH annotation sidecar, scan-assessment Markdown/manifest, and bounded audit
receipts are staged into one :class:`RecoverableWriteSet` transaction.  The
adapter never enumerates a selection and cannot implement an implicit
``import all`` operation; it only accepts the CAS-pinned request produced by a
ready engine dry-run plan.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ...catalog_enrichment.importers import (
    SourceRecord,
    iter_ch_records,
    iter_manual_records,
)
from ...engine.book_review_import import (
    AtomicBookReviewImportRequest,
    BookReviewImportError,
    DestinationReviewState,
    MAX_REASONING_BYTES,
    ReviewImportReceiptBinding,
    ReviewSourceRef,
    catalog_source_record_sha256,
)
from ...engine.manual_source_authority import (
    ManualSourceAuthorityError,
    resolve_manual_source_authority,
)
from ...engine.scan_assessments import (
    MAX_SCAN_ASSESSMENT_MANIFEST_BYTES,
    ScanAssessmentDraft,
    ScanAssessmentKey,
    ScanAssessmentManifest,
    ScanAssessmentProvenance,
    ScanAssessmentView,
    canonical_scan_assessment_json,
    scan_assessment_locator_digest,
)
from .manual_entry_item_codec import ManualEntryItemCodec
from .capture_archive_repository import FilesystemCaptureArchiveRepository
from .recoverable_write_set import RecoverableWriteSet
from .scan_assessment_repository import (
    SCAN_ASSESSMENT_MANIFEST_NAME,
    SCAN_ASSESSMENT_RELATIVE_ROOT,
    SCAN_ASSESSMENT_TEXT_NAME,
    FilesystemScanAssessmentRepository,
)
from .whl_catalogue_codec import WhlCatalogueItemCodec


BOOK_REVIEW_IMPORT_RECEIPT_SCHEMA = "librarytool.book-review-import-receipt/1"
CH_ANNOTATIONS_SCHEMA = "librarytool.ch-annotations/1"
BOOK_REVIEW_IMPORT_RELATIVE_ROOT = PurePosixPath("output/book_review_import")

MAX_IMPORT_RECEIPT_BYTES = 16 * 1024
MAX_CATALOG_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_CH_ANNOTATIONS_BYTES = 16 * 1024 * 1024
MAX_BUILDS_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_BUILD_IDENTITY_BYTES = 1024 * 1024
MAX_PREIMPORT_BACKUP_BYTES = 512 * 1024 * 1024
MAX_PREIMPORT_BACKUP_FILES = 50_000
BOOK_REVIEW_PREIMPORT_BACKUP_SCHEMA = "librarytool.book-review-preimport-backup/1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$", re.ASCII)
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "operation_id",
        "operation_sha256",
        "request_sha256",
        "export_sha256",
        "review_record_id",
        "namespace",
        "source_id",
        "source_hash_before_import",
        "source_hash_after_import",
        "record_revision_before",
        "record_revision_after",
        "assessment_revision_before",
        "assessment_revision_after",
        "assessment_sha256",
        "metadata_sha256",
        "committed_at",
    }
)


def _failure(
    message: str,
    *,
    code: str,
    source_ref: ReviewSourceRef | None = None,
    **details: Any,
) -> BookReviewImportError:
    if source_ref is not None:
        details["source_ref"] = source_ref.key
    return BookReviewImportError(message, code=code, details=details)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _failure(
            "book-review import data is not strict JSON",
            code="invalid_book_review_import_data",
            cause_type=type(exc).__name__,
        ) from exc


def _digest_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _failure(
            f"{field} must be a lower-case SHA-256 digest",
            code="invalid_book_review_import_receipt",
            field=field,
        )
    return value


def _timestamp(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise _failure(
            f"{field} must be an RFC 3339 timestamp",
            code="invalid_book_review_import_receipt",
            field=field,
        )
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise _failure(
            f"{field} must be an RFC 3339 timestamp",
            code="invalid_book_review_import_receipt",
            field=field,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _failure(
            f"{field} must include a UTC offset",
            code="invalid_book_review_import_receipt",
            field=field,
        )
    return value


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _failure(
                "JSON document contains a duplicate key",
                code="invalid_book_review_import_json",
                field=key,
            )
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _failure(
        "JSON document contains a non-finite number",
        code="invalid_book_review_import_json",
        value=value,
    )


def _is_redirecting(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    if os.name != "nt" or not _REPARSE_POINT_ATTRIBUTE:
        return False
    return bool(int(getattr(info, "st_file_attributes", 0)) & _REPARSE_POINT_ATTRIBUTE)


def _safe_relative(value: str | PurePosixPath, *, field: str) -> PurePosixPath:
    raw = str(value)
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or path.as_posix() != raw
        or "\\" in raw
        or ":" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0].casefold() == ".transactions"
    ):
        raise ValueError(f"{field} must be a normalized relative POSIX path")
    return path


def _safe_absolute(root: Path, relative: PurePosixPath) -> Path:
    current = root
    try:
        root_info = os.lstat(current)
    except OSError as exc:
        raise _failure(
            "mutable data root is unavailable",
            code="book_review_destination_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    if _is_redirecting(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise _failure(
            "mutable data root is unsafe",
            code="unsafe_book_review_destination",
        )
    for part in relative.parts[:-1]:
        current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise _failure(
                "mutable destination authority cannot be inspected",
                code="book_review_destination_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting(info) or not stat.S_ISDIR(info.st_mode):
            raise _failure(
                "mutable destination authority crosses an unsafe node",
                code="unsafe_book_review_destination",
            )
    return root.joinpath(*relative.parts)


def _safe_read_only_absolute(value: str | Path, *, field: str) -> Path:
    """Validate a separately rooted, read-only regular file authority."""

    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    path = Path(os.path.abspath(path))
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _failure(
            f"{field} is unavailable",
            code="book_review_destination_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    if _is_redirecting(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _failure(
            f"{field} is not one private regular file",
            code="unsafe_book_review_destination",
        )
    current = path.parent
    while True:
        try:
            parent_info = os.lstat(current)
        except OSError as exc:
            raise _failure(
                f"{field} parent authority is unavailable",
                code="book_review_destination_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
            raise _failure(
                f"{field} crosses an unsafe parent authority",
                code="unsafe_book_review_destination",
            )
        if current == current.parent:
            break
        current = current.parent
    return path


def _stable_bytes(
    path: Path, *, maximum: int, missing_ok: bool = False
) -> bytes | None:
    try:
        named_before = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise _failure(
            "book-review destination document is missing",
            code="book_review_destination_unavailable",
        )
    except OSError as exc:
        raise _failure(
            "book-review destination document is unavailable",
            code="book_review_destination_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    if (
        _is_redirecting(named_before)
        or not stat.S_ISREG(named_before.st_mode)
        or named_before.st_nlink != 1
    ):
        raise _failure(
            "book-review destination document is not one private regular file",
            code="unsafe_book_review_destination",
        )
    if named_before.st_size > maximum:
        raise _failure(
            "book-review destination document exceeds its byte limit",
            code="book_review_destination_too_large",
            maximum=maximum,
        )
    try:
        payload = path.read_bytes()
        named_after = os.lstat(path)
    except OSError as exc:
        raise _failure(
            "book-review destination document could not be read",
            code="book_review_destination_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    if (
        not os.path.samestat(named_before, named_after)
        or named_before.st_size != len(payload)
        or named_before.st_mtime_ns != named_after.st_mtime_ns
    ):
        raise _failure(
            "book-review destination document changed while read",
            code="book_review_destination_changed",
        )
    return payload


def _decode_json(
    payload: bytes,
    *,
    label: str,
    expected: type,
) -> Any:
    try:
        value = json.loads(
            payload.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except BookReviewImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _failure(
            f"{label} is not strict UTF-8 JSON",
            code="invalid_book_review_import_json",
            cause_type=type(exc).__name__,
        ) from exc
    if not isinstance(value, expected):
        raise _failure(
            f"{label} has an invalid top-level shape",
            code="invalid_book_review_import_json",
        )
    return value


def _document_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8", errors="strict")


def _later_timestamp(now: datetime, prior: str = "") -> str:
    value = now.astimezone(timezone.utc)
    if prior:
        candidate = prior[:-1] + "+00:00" if prior.endswith("Z") else prior
        try:
            parsed = datetime.fromisoformat(candidate).astimezone(timezone.utc)
        except ValueError:
            parsed = None
        if parsed is not None and value <= parsed:
            value = parsed + timedelta(microseconds=1)
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _advance_timestamp(candidate: str, prior: str = "") -> str:
    parsed = datetime.fromisoformat(
        candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
    )
    return _later_timestamp(parsed, prior)


def ch_absent_revision(source_hash: str) -> str:
    """CAS token representing no annotation for one exact shipped CH row."""

    return "cha-absent-" + _sha256(source_hash, field="source_hash")


@dataclass(frozen=True, slots=True)
class _AuditReceipt:
    operation_id: str
    operation_sha256: str
    request_sha256: str
    export_sha256: str
    review_record_id: str
    source_ref: ReviewSourceRef
    source_hash_before_import: str
    source_hash_after_import: str
    record_revision_before: str
    record_revision_after: str
    assessment_revision_before: str
    assessment_revision_after: str
    assessment_sha256: str
    metadata_sha256: str
    committed_at: str

    def __post_init__(self) -> None:
        if not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise _failure(
                "receipt operation_id is invalid",
                code="invalid_book_review_import_receipt",
            )
        for field in (
            "operation_sha256",
            "request_sha256",
            "export_sha256",
            "source_hash_before_import",
            "source_hash_after_import",
            "assessment_sha256",
            "metadata_sha256",
        ):
            _sha256(getattr(self, field), field=field)
        for field in (
            "record_revision_before",
            "record_revision_after",
            "assessment_revision_after",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise _failure(
                    f"receipt {field} is invalid",
                    code="invalid_book_review_import_receipt",
                    field=field,
                )
        if self.assessment_revision_before and (
            not isinstance(self.assessment_revision_before, str)
            or len(self.assessment_revision_before) > 512
        ):
            raise _failure(
                "receipt assessment_revision_before is invalid",
                code="invalid_book_review_import_receipt",
            )
        _timestamp(self.committed_at, field="committed_at")

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": BOOK_REVIEW_IMPORT_RECEIPT_SCHEMA,
            "operation_id": self.operation_id,
            "operation_sha256": self.operation_sha256,
            "request_sha256": self.request_sha256,
            "export_sha256": self.export_sha256,
            "review_record_id": self.review_record_id,
            "namespace": self.source_ref.namespace,
            "source_id": self.source_ref.source_id,
            "source_hash_before_import": self.source_hash_before_import,
            "source_hash_after_import": self.source_hash_after_import,
            "record_revision_before": self.record_revision_before,
            "record_revision_after": self.record_revision_after,
            "assessment_revision_before": self.assessment_revision_before,
            "assessment_revision_after": self.assessment_revision_after,
            "assessment_sha256": self.assessment_sha256,
            "metadata_sha256": self.metadata_sha256,
            "committed_at": self.committed_at,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "_AuditReceipt":
        if (
            not isinstance(value, Mapping)
            or frozenset(value) != _RECEIPT_FIELDS
            or value.get("schema") != BOOK_REVIEW_IMPORT_RECEIPT_SCHEMA
        ):
            raise _failure(
                "book-review import receipt has invalid fields",
                code="invalid_book_review_import_receipt",
            )
        return cls(
            operation_id=value["operation_id"],
            operation_sha256=value["operation_sha256"],
            request_sha256=value["request_sha256"],
            export_sha256=value["export_sha256"],
            review_record_id=value["review_record_id"],
            source_ref=ReviewSourceRef(value["namespace"], value["source_id"]),
            source_hash_before_import=value["source_hash_before_import"],
            source_hash_after_import=value["source_hash_after_import"],
            record_revision_before=value["record_revision_before"],
            record_revision_after=value["record_revision_after"],
            assessment_revision_before=value["assessment_revision_before"],
            assessment_revision_after=value["assessment_revision_after"],
            assessment_sha256=value["assessment_sha256"],
            metadata_sha256=value["metadata_sha256"],
            committed_at=value["committed_at"],
        )

    @property
    def binding(self) -> ReviewImportReceiptBinding:
        return ReviewImportReceiptBinding(
            export_sha256=self.export_sha256,
            source_hash_before_import=self.source_hash_before_import,
            source_hash_after_import=self.source_hash_after_import,
        )


@dataclass(frozen=True, slots=True)
class _TargetMutation:
    relative: PurePosixPath
    payload: bytes
    source_hash_after: str
    record_revision_after: str
    canonical_item_id: str = ""
    capture_id: str = ""


@dataclass(frozen=True, slots=True)
class _ManualAuthority:
    storage_kind: str
    storage_id: str
    relative: PurePosixPath
    document: dict[str, Any]
    row: Mapping[str, Any]
    record_revision: str
    canonical_item_id: str
    capture_id: str
    association_state: str
    association_book_id: str

    @property
    def identity_sha256(self) -> str | None:
        if not self.capture_id:
            return None
        return _digest_json(
            {
                "storage_kind": self.storage_kind,
                "storage_id": self.storage_id,
                "canonical_item_id": self.canonical_item_id,
                "capture_id": self.capture_id,
                "association_state": self.association_state,
                "association_book_id": self.association_book_id,
            }
        )


class FilesystemBookReviewImportAdapter:
    """Read destination state and atomically apply one selected import unit."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        manual_entries: str | PurePosixPath = "output/manual_entries.json",
        ch_library: str | PurePosixPath = "output/ch_library.json",
        ch_library_path: str | Path | None = None,
        builds: str | PurePosixPath = "output/whl_builds.json",
        entries: str | PurePosixPath = "output/entries",
        ch_annotations: str | PurePosixPath = "output/ch_annotations.json",
        scan_assessment_root: str | PurePosixPath = SCAN_ASSESSMENT_RELATIVE_ROOT,
        import_root: str | PurePosixPath = BOOK_REVIEW_IMPORT_RELATIVE_ROOT,
        captures: str | PurePosixPath = "captures",
        clock: Callable[[], datetime] | None = None,
        revision_nonce: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        self._write_set = write_set
        self._manual_relative = _safe_relative(manual_entries, field="manual_entries")
        self._ch_relative = _safe_relative(ch_library, field="ch_library")
        self._ch_read_only_path = (
            _safe_read_only_absolute(ch_library_path, field="ch_library_path")
            if ch_library_path is not None
            else None
        )
        self._builds_relative = _safe_relative(builds, field="builds")
        self._entries_relative = _safe_relative(entries, field="entries")
        self._ch_annotations_relative = _safe_relative(
            ch_annotations, field="ch_annotations"
        )
        self._scan_root = _safe_relative(
            scan_assessment_root, field="scan_assessment_root"
        )
        self._import_root = _safe_relative(import_root, field="import_root")
        self._captures_relative = _safe_relative(captures, field="captures")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._revision_nonce = revision_nonce or (lambda: secrets.token_hex(32))
        self._assessments = FilesystemScanAssessmentRepository(
            write_set,
            relative_root=self._scan_root,
        )

    # -- destination read port ---------------------------------------

    def read(self, ref: ReviewSourceRef) -> DestinationReviewState | None:
        if not isinstance(ref, ReviewSourceRef):
            raise TypeError("ref must be ReviewSourceRef")
        with self._write_set.workspace_lease():
            return self._read_state_unlocked(ref)

    def snapshot(self, refs: Sequence[ReviewSourceRef]) -> dict[str, Any]:
        """Return an explicit, bounded destination snapshot for dry-run input."""

        selected = tuple(refs)
        if not selected:
            raise _failure(
                "destination snapshot requires an explicit non-empty selection",
                code="explicit_selection_required",
            )
        if any(not isinstance(ref, ReviewSourceRef) for ref in selected):
            raise TypeError("refs must contain ReviewSourceRef values")
        if len(set(selected)) != len(selected):
            raise _failure(
                "destination snapshot selection contains a duplicate",
                code="duplicate_selection",
            )
        records: list[dict[str, Any]] = []
        with self._write_set.workspace_lease():
            for ref in sorted(selected):
                state = self._read_state_unlocked(ref)
                if state is None:
                    raise _failure(
                        "selected Desktop source does not exist",
                        code="book_review_destination_missing",
                        source_ref=ref,
                    )
                receipt = state.import_receipt
                records.append(
                    {
                        **ref.as_dict(),
                        "record_revision": state.record_revision,
                        "metadata": state.metadata_dict,
                        "assessment_sha256": state.assessment_sha256,
                        "assessment_revision": state.assessment_revision,
                        "authority_sha256": state.authority_sha256,
                        "import_receipt": (
                            None
                            if receipt is None
                            else {
                                "export_sha256": receipt.export_sha256,
                                "source_hash_before_import": (
                                    receipt.source_hash_before_import
                                ),
                                "source_hash_after_import": (
                                    receipt.source_hash_after_import
                                ),
                            }
                        ),
                    }
                )
        return {
            "schema": "librarytool.book-review-destination-snapshot/v1",
            "records": records,
        }

    def create_preimport_backup(
        self,
        requests: Sequence[AtomicBookReviewImportRequest],
        destination: str | Path,
    ) -> dict[str, Any]:
        """Persist byte-exact mutable preimages before selected commit writes.

        The ZIP is deliberately outside the mutable workspace. Its manifest
        contains only identities, CAS pins, sizes, and hashes; existing
        assessment prose is present solely as a restorable file member.
        """

        selected = tuple(requests)
        if not selected:
            raise _failure(
                "pre-import backup requires explicit commit requests",
                code="explicit_selection_required",
            )
        if any(
            not isinstance(request, AtomicBookReviewImportRequest)
            for request in selected
        ):
            raise TypeError(
                "requests must contain AtomicBookReviewImportRequest values"
            )
        refs = tuple(request.unit.source_ref for request in selected)
        if len(set(refs)) != len(refs):
            raise _failure(
                "pre-import backup contains a duplicate source reference",
                code="duplicate_selection",
            )
        backup_path = self._preimport_backup_path(destination)
        file_limits: dict[PurePosixPath, int] = {}
        request_rows: list[dict[str, Any]] = []
        with self._write_set.workspace_lease():
            for request in selected:
                ref = request.unit.source_ref
                source, raw_document = self._current_source(ref)
                source_hash = catalog_source_record_sha256(source)
                state = self._read_state_from_current(ref, source, raw_document)
                if source_hash != request.unit.expected_source_hash:
                    raise _failure(
                        "current source hash does not match the dry-run plan",
                        code="current_source_hash_mismatch",
                        source_ref=ref,
                    )
                if state.record_revision != request.expected_record_revision:
                    raise _failure(
                        "destination record changed after the dry-run plan",
                        code="destination_record_revision_conflict",
                        source_ref=ref,
                    )
                if state.assessment_revision != request.expected_assessment_revision:
                    raise _failure(
                        "scan assessment changed after the dry-run plan",
                        code="assessment_revision_conflict",
                        source_ref=ref,
                    )
                if state.authority_sha256 != request.expected_authority_sha256:
                    raise _failure(
                        "active holding authority changed after the dry-run plan",
                        code="destination_authority_conflict",
                        source_ref=ref,
                    )

                if ref.namespace == "manual_entries":
                    authority = self._manual_authority(ref, raw_document)
                    file_limits[self._manual_relative] = MAX_CATALOG_DOCUMENT_BYTES
                    if authority.storage_kind == "build":
                        file_limits[self._builds_relative] = MAX_BUILDS_DOCUMENT_BYTES
                else:
                    file_limits[self._ch_annotations_relative] = (
                        MAX_CH_ANNOTATIONS_BYTES
                    )
                assessment_directory = self._assessment_directory(ref)
                file_limits[assessment_directory / SCAN_ASSESSMENT_TEXT_NAME] = (
                    MAX_REASONING_BYTES
                )
                file_limits[assessment_directory / SCAN_ASSESSMENT_MANIFEST_NAME] = (
                    MAX_SCAN_ASSESSMENT_MANIFEST_BYTES
                )
                operation_sha256 = hashlib.sha256(
                    request.operation_id.encode("utf-8")
                ).hexdigest()
                file_limits[self._operation_receipt_relative(operation_sha256)] = (
                    MAX_IMPORT_RECEIPT_BYTES
                )
                file_limits[self._binding_relative(ref)] = MAX_IMPORT_RECEIPT_BYTES
                request_rows.append(
                    {
                        **ref.as_dict(),
                        "operation_sha256": operation_sha256,
                        "source_sha256": source_hash,
                        "record_revision": state.record_revision,
                        "assessment_revision": state.assessment_revision,
                        "authority_sha256": state.authority_sha256,
                    }
                )

            if len(file_limits) > MAX_PREIMPORT_BACKUP_FILES:
                raise _failure(
                    "pre-import backup contains too many files",
                    code="book_review_preimport_backup_too_large",
                    maximum=MAX_PREIMPORT_BACKUP_FILES,
                )
            files: dict[PurePosixPath, bytes | None] = {}
            file_rows: list[dict[str, Any]] = []
            total = 0
            for relative in sorted(file_limits, key=str):
                payload = _stable_bytes(
                    _safe_absolute(self._write_set.root, relative),
                    maximum=file_limits[relative],
                    missing_ok=True,
                )
                files[relative] = payload
                size = len(payload) if payload is not None else 0
                total += size
                if total > MAX_PREIMPORT_BACKUP_BYTES:
                    raise _failure(
                        "pre-import backup exceeds its byte limit",
                        code="book_review_preimport_backup_too_large",
                        maximum=MAX_PREIMPORT_BACKUP_BYTES,
                    )
                file_rows.append(
                    {
                        "relative_path": relative.as_posix(),
                        "archive_member": (
                            "files/" + relative.as_posix()
                            if payload is not None
                            else None
                        ),
                        "exists": payload is not None,
                        "byte_size": size,
                        "sha256": (
                            hashlib.sha256(payload).hexdigest()
                            if payload is not None
                            else None
                        ),
                    }
                )
            created_at = self._now(refs[0])
            manifest = {
                "schema": BOOK_REVIEW_PREIMPORT_BACKUP_SCHEMA,
                "created_at": created_at,
                "source_count": len(request_rows),
                "sources": request_rows,
                "file_count": len(file_rows),
                "files": file_rows,
            }
            try:
                self._write_preimport_backup(backup_path, manifest, files)
            except BookReviewImportError:
                raise
            except Exception as exc:
                raise _failure(
                    "pre-import backup could not be persisted",
                    code="book_review_preimport_backup_unavailable",
                    cause_type=type(exc).__name__,
                ) from exc

        payload = _stable_bytes(
            backup_path,
            maximum=MAX_PREIMPORT_BACKUP_BYTES,
        )
        assert payload is not None
        return {
            "schema": BOOK_REVIEW_PREIMPORT_BACKUP_SCHEMA,
            "created_at": created_at,
            "source_count": len(request_rows),
            "file_count": len(file_rows),
            "byte_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _preimport_backup_path(self, value: str | Path) -> Path:
        configured = Path(value).expanduser()
        try:
            parent = configured.parent.resolve(strict=True)
            parent_info = os.lstat(parent)
        except OSError as exc:
            raise _failure(
                "pre-import backup parent is unavailable",
                code="book_review_preimport_backup_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
            raise _failure(
                "pre-import backup parent is unsafe",
                code="unsafe_book_review_preimport_backup",
            )
        path = parent / configured.name
        try:
            path.relative_to(self._write_set.root)
        except ValueError:
            pass
        else:
            raise _failure(
                "pre-import backup must be outside the mutable data root",
                code="unsafe_book_review_preimport_backup",
            )
        if os.path.lexists(path):
            raise _failure(
                "pre-import backup destination already exists",
                code="book_review_preimport_backup_exists",
            )
        return path

    @staticmethod
    def _write_preimport_backup(
        destination: Path,
        manifest: Mapping[str, Any],
        files: Mapping[PurePosixPath, bytes | None],
    ) -> None:
        descriptor = -1
        created = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(destination, flags, 0o600)
            created = True
            with os.fdopen(descriptor, "w+b", closefd=True) as stream:
                descriptor = -1
                with zipfile.ZipFile(
                    stream,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as archive:
                    archive.writestr("manifest.json", _canonical_json(manifest) + b"\n")
                    for relative in sorted(files, key=str):
                        payload = files[relative]
                        if payload is not None:
                            archive.writestr("files/" + relative.as_posix(), payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            if created:
                try:
                    os.unlink(destination)
                except OSError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    # -- atomic commit port ------------------------------------------

    def apply_atomically(
        self, request: AtomicBookReviewImportRequest
    ) -> DestinationReviewState:
        if not isinstance(request, AtomicBookReviewImportRequest):
            raise TypeError("request must be AtomicBookReviewImportRequest")
        if not _OPERATION_ID_RE.fullmatch(request.operation_id):
            raise _failure(
                "book-review import operation_id is invalid",
                code="invalid_book_review_import_operation",
            )
        request_sha256 = self._request_sha256(request)
        operation_sha256 = hashlib.sha256(request.operation_id.encode()).hexdigest()
        ref = request.unit.source_ref

        with self._write_set.workspace_lease():
            replay = self._read_operation_receipt(operation_sha256)
            if replay is not None:
                if replay.request_sha256 != request_sha256 or replay.source_ref != ref:
                    raise _failure(
                        "operation_id was already used for another import request",
                        code="book_review_import_operation_conflict",
                        source_ref=ref,
                    )
                return self._verified_replay_state(replay)

            source, raw_document = self._current_source(ref)
            source_hash_before = catalog_source_record_sha256(source)
            if source_hash_before != request.unit.expected_source_hash:
                raise _failure(
                    "current source hash does not match the dry-run plan",
                    code="current_source_hash_mismatch",
                    source_ref=ref,
                    expected=request.unit.expected_source_hash,
                    actual=source_hash_before,
                )
            before = self._read_state_from_current(ref, source, raw_document)
            if before.record_revision != request.expected_record_revision:
                raise _failure(
                    "destination record changed after the dry-run plan",
                    code="destination_record_revision_conflict",
                    source_ref=ref,
                    expected=request.expected_record_revision,
                    actual=before.record_revision,
                )
            if before.assessment_revision != request.expected_assessment_revision:
                raise _failure(
                    "scan assessment changed after the dry-run plan",
                    code="assessment_revision_conflict",
                    source_ref=ref,
                    expected=request.expected_assessment_revision,
                    actual=before.assessment_revision,
                )
            if before.authority_sha256 != request.expected_authority_sha256:
                raise _failure(
                    "active holding authority changed after the dry-run plan",
                    code="destination_authority_conflict",
                    source_ref=ref,
                    expected=request.expected_authority_sha256,
                    actual=before.authority_sha256,
                )
            if request.create_assessment != (before.assessment_revision is None):
                raise _failure(
                    "assessment create/update mode no longer matches the destination",
                    code="assessment_revision_conflict",
                    source_ref=ref,
                )

            now = self._now(ref)
            target = self._target_mutation(
                request,
                source=source,
                raw_document=raw_document,
                before=before,
                now=now,
            )
            assessment = self._assessment_view(
                request,
                current=self._assessment(ref),
                active_source_hash=target.source_hash_after,
                canonical_item_id=target.canonical_item_id,
                capture_id=target.capture_id,
                now=now,
                request_sha256=request_sha256,
            )
            receipt = _AuditReceipt(
                operation_id=request.operation_id,
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
                export_sha256=request.export_sha256,
                review_record_id=request.unit.review_record_id,
                source_ref=ref,
                source_hash_before_import=source_hash_before,
                source_hash_after_import=target.source_hash_after,
                record_revision_before=before.record_revision,
                record_revision_after=target.record_revision_after,
                assessment_revision_before=before.assessment_revision or "",
                assessment_revision_after=assessment.revision,
                assessment_sha256=assessment.manifest.content_sha256,
                metadata_sha256=_digest_json(request.unit.metadata_dict),
                committed_at=now,
            )
            receipt_payload = _canonical_json(receipt.as_dict()) + b"\n"
            if len(receipt_payload) > MAX_IMPORT_RECEIPT_BYTES:
                raise _failure(
                    "book-review import receipt exceeds its byte limit",
                    code="book_review_import_receipt_too_large",
                    source_ref=ref,
                )
            manifest_payload = (
                canonical_scan_assessment_json(assessment.manifest.as_dict()) + b"\n"
            )
            if len(manifest_payload) > MAX_SCAN_ASSESSMENT_MANIFEST_BYTES:
                raise _failure(
                    "scan-assessment manifest exceeds its byte limit",
                    code="scan_assessment_manifest_too_large",
                    source_ref=ref,
                )

            # Re-project immediately before publication. This catches a
            # source document or capture-OCR dependency that changed while the
            # postimage and active manifest binding were being rendered.
            confirmation_source, confirmation_document = self._current_source(ref)
            confirmation_hash = catalog_source_record_sha256(confirmation_source)
            if confirmation_hash != source_hash_before:
                raise _failure(
                    "source projection changed while the commit was prepared",
                    code="current_source_hash_mismatch",
                    source_ref=ref,
                    expected=source_hash_before,
                    actual=confirmation_hash,
                )
            confirmation_state = self._read_state_from_current(
                ref,
                confirmation_source,
                confirmation_document,
            )
            if confirmation_state.record_revision != before.record_revision:
                raise _failure(
                    "destination record changed while the commit was prepared",
                    code="destination_record_revision_conflict",
                    source_ref=ref,
                    expected=before.record_revision,
                    actual=confirmation_state.record_revision,
                )
            if confirmation_state.assessment_revision != before.assessment_revision:
                raise _failure(
                    "scan assessment changed while the commit was prepared",
                    code="assessment_revision_conflict",
                    source_ref=ref,
                    expected=before.assessment_revision,
                    actual=confirmation_state.assessment_revision,
                )
            if confirmation_state.authority_sha256 != before.authority_sha256:
                raise _failure(
                    "active holding authority changed while the commit was prepared",
                    code="destination_authority_conflict",
                    source_ref=ref,
                    expected=before.authority_sha256,
                    actual=confirmation_state.authority_sha256,
                )

            transaction = self._write_set.begin(
                operation_id=request.operation_id,
                scope="book_review_import",
                metadata={
                    "source_locator": scan_assessment_locator_digest(
                        ScanAssessmentKey(ref.namespace, ref.source_id)
                    ),
                    "request_sha256": request_sha256,
                },
            )
            transaction.stage_write(target.relative, target.payload)
            assessment_directory = self._assessment_directory(ref)
            transaction.stage_write(
                assessment_directory / SCAN_ASSESSMENT_TEXT_NAME,
                assessment.text.encode("utf-8", errors="strict"),
            )
            transaction.stage_write(
                assessment_directory / SCAN_ASSESSMENT_MANIFEST_NAME,
                manifest_payload,
            )
            transaction.stage_write(
                self._operation_receipt_relative(operation_sha256), receipt_payload
            )
            transaction.stage_write(self._binding_relative(ref), receipt_payload)
            transaction.commit(
                receipt={
                    "kind": "book_review_import",
                    "operation_sha256": operation_sha256,
                    "source_locator": scan_assessment_locator_digest(
                        ScanAssessmentKey(ref.namespace, ref.source_id)
                    ),
                    "assessment_revision": assessment.revision,
                }
            )
            return DestinationReviewState.create(
                metadata=request.unit.metadata_dict,
                record_revision=target.record_revision_after,
                assessment_sha256=assessment.manifest.content_sha256,
                assessment_revision=assessment.revision,
                import_receipt=receipt.binding,
                authority_sha256=before.authority_sha256,
            )

    # -- source and destination snapshots -----------------------------

    def _read_state_unlocked(
        self, ref: ReviewSourceRef
    ) -> DestinationReviewState | None:
        try:
            source, raw_document = self._current_source(ref)
        except BookReviewImportError as exc:
            if exc.code == "book_review_destination_missing":
                return None
            raise
        return self._read_state_from_current(ref, source, raw_document)

    def _read_state_from_current(
        self,
        ref: ReviewSourceRef,
        source: SourceRecord,
        raw_document: Any,
    ) -> DestinationReviewState:
        source_hash = catalog_source_record_sha256(source)
        assessment = self._assessment(ref)
        if ref.namespace == "manual_entries":
            authority = self._manual_authority(ref, raw_document)
            metadata = self._review_fields(authority.row)
            record_revision = authority.record_revision
            authority_sha256 = authority.identity_sha256
            if assessment is not None and (
                assessment.manifest.canonical_item_id != authority.canonical_item_id
                or assessment.manifest.capture_id != authority.capture_id
            ):
                raise _failure(
                    "scan assessment aliases no longer match active holding authority",
                    code="assessment_authority_conflict",
                    source_ref=ref,
                )
        else:
            authority_sha256 = None
            annotation = self._ch_annotation(ref, source_hash)
            if annotation is None:
                metadata = {}
                record_revision = ch_absent_revision(source_hash)
            else:
                metadata = self._review_fields(annotation.get("fields", {}))
                record_revision = annotation["revision"]

        receipt = self._read_binding(ref)
        binding = None
        if receipt is not None and self._receipt_matches_state(
            receipt,
            source_hash=source_hash,
            record_revision=record_revision,
            metadata=metadata,
            assessment=assessment,
        ):
            binding = receipt.binding
        return DestinationReviewState.create(
            metadata=metadata,
            record_revision=record_revision,
            assessment_sha256=(
                assessment.manifest.content_sha256 if assessment is not None else None
            ),
            assessment_revision=(
                assessment.revision if assessment is not None else None
            ),
            import_receipt=binding,
            authority_sha256=authority_sha256,
        )

    def _current_source(self, ref: ReviewSourceRef) -> tuple[SourceRecord, Any]:
        if ref.namespace == "manual_entries":
            maximum = MAX_CATALOG_DOCUMENT_BYTES
            expected = dict
        else:
            maximum = MAX_CATALOG_DOCUMENT_BYTES
            expected = list
        path = (
            _safe_absolute(self._write_set.root, self._manual_relative)
            if ref.namespace == "manual_entries"
            else self._ch_path()
        )
        before = _stable_bytes(path, maximum=maximum)
        assert before is not None
        raw_document = _decode_json(
            before,
            label=ref.namespace,
            expected=expected,
        )
        if ref.namespace == "manual_entries":
            raw_row = raw_document.get(ref.source_id)
            if not isinstance(raw_row, Mapping):
                raise _failure(
                    "selected manual source row does not exist",
                    code="book_review_destination_missing",
                    source_ref=ref,
                )
            iterator = iter_manual_records(
                path,
                captures_dir=_safe_absolute(
                    self._write_set.root, self._captures_relative
                ),
            )
        else:
            index = int(ref.source_id)
            if index >= len(raw_document) or not isinstance(
                raw_document[index], Mapping
            ):
                raise _failure(
                    "selected CH source row does not exist",
                    code="book_review_destination_missing",
                    source_ref=ref,
                )
            iterator = iter_ch_records(path)
        source = next(
            (
                value
                for value in iterator
                if value.namespace == ref.namespace and value.source_id == ref.source_id
            ),
            None,
        )
        if source is None:
            raise _failure(
                "selected source projection does not exist",
                code="book_review_destination_missing",
                source_ref=ref,
            )
        after = _stable_bytes(path, maximum=maximum)
        if after != before:
            raise _failure(
                "source document changed while its projection was built",
                code="book_review_destination_changed",
                source_ref=ref,
            )
        return source, raw_document

    def _ch_path(self) -> Path:
        return self._ch_read_only_path or _safe_absolute(
            self._write_set.root,
            self._ch_relative,
        )

    def _builds_document(self) -> dict[str, Any]:
        path = _safe_absolute(self._write_set.root, self._builds_relative)
        payload = _stable_bytes(
            path,
            maximum=MAX_BUILDS_DOCUMENT_BYTES,
            missing_ok=True,
        )
        if payload is None:
            return {}
        value = _decode_json(payload, label="WHL builds", expected=dict)
        if any(
            not isinstance(build_id, str)
            or not build_id
            or not isinstance(row, Mapping)
            for build_id, row in value.items()
        ):
            raise _failure(
                "WHL builds contains an invalid active record",
                code="invalid_book_review_build_store",
            )
        return value

    @staticmethod
    def _validate_build_record(build_id: str, row: Mapping[str, Any]) -> None:
        codec = WhlCatalogueItemCodec(
            advance_revision=lambda previous: previous or "unused",
            category_ids_for=tuple,
            # This importer never changes representation state. The host's
            # representation service remains its validation authority.
            validate_representation_manifest=lambda _raw: None,
        )
        try:
            codec.validate_managed_record(build_id, row)
            metadata = {
                key: value
                for key, value in row.items()
                if key not in codec.managed_fields
            }
            codec.validate_catalogue_metadata(metadata)
        except (TypeError, ValueError) as exc:
            raise _failure(
                "promoted build failed its storage codec",
                code="invalid_book_review_build_store",
                build_id=build_id,
                cause_type=type(exc).__name__,
            ) from exc

    def _build_identity_document(self, build_id: str) -> Mapping[str, Any] | None:
        relative = self._entries_relative / build_id / "ocr" / "lib-id.json"
        path = _safe_absolute(self._write_set.root, relative)
        payload = _stable_bytes(
            path,
            maximum=MAX_BUILD_IDENTITY_BYTES,
            missing_ok=True,
        )
        if payload is None:
            return None
        return _decode_json(
            payload,
            label="promoted build identity",
            expected=dict,
        )

    def _capture_association(self, capture_id: str):
        return FilesystemCaptureArchiveRepository.inspect_association_identity(
            _safe_absolute(self._write_set.root, self._builds_relative.parent),
            capture_id,
        )

    def _manual_authority(
        self,
        ref: ReviewSourceRef,
        manual_document: Mapping[str, Any],
    ) -> _ManualAuthority:
        row = manual_document.get(ref.source_id)
        if not isinstance(row, Mapping):
            raise _failure(
                "selected manual source row does not exist",
                code="book_review_destination_missing",
                source_ref=ref,
            )
        try:
            ManualEntryItemCodec.validate_record(ref.source_id, row)
        except (TypeError, ValueError) as exc:
            raise _failure(
                "selected manual source row failed its storage codec",
                code="invalid_book_review_manual_store",
                source_ref=ref,
                cause_type=type(exc).__name__,
            ) from exc
        builds = self._builds_document()
        try:
            resolved = resolve_manual_source_authority(
                manual_document,
                builds,
                ref.source_id,
                association_for=self._capture_association,
                build_identity_for=self._build_identity_document,
            )
        except ManualSourceAuthorityError as exc:
            raise _failure(
                str(exc),
                code=exc.code,
                source_ref=ref,
                **exc.details,
            ) from exc
        if resolved.storage_kind == "build":
            self._validate_build_record(resolved.storage_id, resolved.storage_row)
            return _ManualAuthority(
                storage_kind="build",
                storage_id=resolved.storage_id,
                relative=self._builds_relative,
                document=builds,
                row=resolved.storage_row,
                record_revision=WhlCatalogueItemCodec.record_revision(
                    resolved.storage_id,
                    resolved.storage_row,
                ),
                canonical_item_id=resolved.canonical_item_id,
                capture_id=resolved.capture_id,
                association_state=resolved.association_state,
                association_book_id=resolved.association_book_id,
            )
        return _ManualAuthority(
            storage_kind="manual",
            storage_id=ref.source_id,
            relative=self._manual_relative,
            document=dict(manual_document),
            row=resolved.storage_row,
            record_revision=ManualEntryItemCodec.record_revision(
                ref.source_id,
                resolved.storage_row,
            ),
            canonical_item_id=resolved.canonical_item_id,
            capture_id=resolved.capture_id,
            association_state=resolved.association_state,
            association_book_id=resolved.association_book_id,
        )

    def _assessment(self, ref: ReviewSourceRef) -> ScanAssessmentView | None:
        return self._assessments.read(ScanAssessmentKey(ref.namespace, ref.source_id))

    # -- target mutation rendering -----------------------------------

    def _target_mutation(
        self,
        request: AtomicBookReviewImportRequest,
        *,
        source: SourceRecord,
        raw_document: Any,
        before: DestinationReviewState,
        now: str,
    ) -> _TargetMutation:
        ref = request.unit.source_ref
        desired = request.unit.metadata_dict
        if ref.namespace == "manual_entries":
            authority = self._manual_authority(ref, raw_document)
            if authority.record_revision != before.record_revision:
                raise _failure(
                    "destination authority changed after the dry-run plan",
                    code="destination_record_revision_conflict",
                    source_ref=ref,
                    expected=before.record_revision,
                    actual=authority.record_revision,
                )
            document = copy.deepcopy(authority.document)
            row = copy.deepcopy(dict(authority.row))
            self._patch_review_fields(row, desired)
            target_updated_at = _advance_timestamp(
                now, str(row.get("updated_at") or "")
            )
            row["updated_at"] = target_updated_at
            document[authority.storage_id] = row
            if authority.storage_kind == "build":
                self._validate_build_record(authority.storage_id, row)
                source_hash_after = catalog_source_record_sha256(source)
                record_revision_after = WhlCatalogueItemCodec.record_revision(
                    authority.storage_id,
                    row,
                )
            else:
                ManualEntryItemCodec.validate_record(ref.source_id, row)
                projected = dict(source.data)
                self._patch_review_fields(projected, desired)
                projected["updated_at"] = target_updated_at
                after_source = SourceRecord(
                    namespace=ref.namespace,
                    source_id=ref.source_id,
                    data=projected,
                )
                source_hash_after = catalog_source_record_sha256(after_source)
                record_revision_after = ManualEntryItemCodec.record_revision(
                    ref.source_id,
                    row,
                )
            return _TargetMutation(
                relative=authority.relative,
                payload=_document_bytes(document),
                source_hash_after=source_hash_after,
                record_revision_after=record_revision_after,
                canonical_item_id=authority.canonical_item_id,
                capture_id=authority.capture_id,
            )

        source_hash = catalog_source_record_sha256(source)
        document = self._ch_annotations_document()
        annotations = document["annotations"]
        current = annotations.get(ref.source_id)
        current_mapping = (
            copy.deepcopy(dict(current)) if isinstance(current, Mapping) else {}
        )
        created_at = str(current_mapping.get("created_at") or now)
        target_updated_at = _advance_timestamp(
            now, str(current_mapping.get("updated_at") or "")
        )
        revision_material = {
            "namespace": "ch_library",
            "source_id": ref.source_id,
            "source_sha256": source_hash,
            "fields": desired,
            "operation_id": request.operation_id,
            "updated_at": target_updated_at,
            "nonce": self._nonce(ref),
        }
        revision = "cha-" + _digest_json(revision_material)
        current_mapping.update(
            {
                "namespace": "ch_library",
                "source_id": ref.source_id,
                "source_sha256": source_hash,
                "fields": copy.deepcopy(desired),
                "revision": revision,
                "created_at": created_at,
                "updated_at": target_updated_at,
            }
        )
        annotations[ref.source_id] = current_mapping
        return _TargetMutation(
            relative=self._ch_annotations_relative,
            payload=_document_bytes(document),
            source_hash_after=source_hash,
            record_revision_after=revision,
        )

    def _assessment_view(
        self,
        request: AtomicBookReviewImportRequest,
        *,
        current: ScanAssessmentView | None,
        active_source_hash: str,
        canonical_item_id: str,
        capture_id: str,
        now: str,
        request_sha256: str,
    ) -> ScanAssessmentView:
        ref = request.unit.source_ref
        if request.create_assessment:
            if current is not None:
                raise _failure(
                    "scan assessment already exists",
                    code="assessment_revision_conflict",
                    source_ref=ref,
                )
            created_at = now
            prior_revision = ""
        else:
            if (
                current is None
                or current.revision != request.expected_assessment_revision
            ):
                raise _failure(
                    "scan assessment changed after the dry-run plan",
                    code="assessment_revision_conflict",
                    source_ref=ref,
                )
            created_at = current.manifest.created_at
            prior_revision = current.revision
            now = _later_timestamp(self._now_value(ref), current.manifest.updated_at)
        draft = ScanAssessmentDraft(
            text=request.unit.reasoning.text,
            provenance=ScanAssessmentProvenance(
                review_record_uuid=request.unit.review_record_id,
                source_database="catalog-enrichment-review",
                source_snapshot="reviewed-export-" + request.export_sha256[:24],
                # Active binding: manual imports change the source projection.
                # The original export hash remains in the audit receipt.
                source_row_sha256=active_source_hash,
            ),
            canonical_item_id=canonical_item_id,
            capture_id=capture_id,
        )
        revision = "sa-" + _digest_json(
            {
                "namespace": ref.namespace,
                "source_id": ref.source_id,
                "operation_id": request.operation_id,
                "request_sha256": request_sha256,
                "previous_revision": prior_revision,
                "active_source_hash": active_source_hash,
                "updated_at": now,
                "nonce": self._nonce(ref),
            }
        )
        manifest = ScanAssessmentManifest(
            key=ScanAssessmentKey(ref.namespace, ref.source_id),
            content_sha256=hashlib.sha256(draft.utf8_bytes).hexdigest(),
            byte_size=len(draft.utf8_bytes),
            revision=revision,
            created_at=created_at,
            updated_at=now,
            provenance=draft.provenance,
            canonical_item_id=draft.canonical_item_id,
            capture_id=draft.capture_id,
        )
        return ScanAssessmentView(manifest=manifest, text=draft.text)

    # -- CH annotations ----------------------------------------------

    def _ch_annotations_document(self) -> dict[str, Any]:
        path = _safe_absolute(self._write_set.root, self._ch_annotations_relative)
        payload = _stable_bytes(
            path,
            maximum=MAX_CH_ANNOTATIONS_BYTES,
            missing_ok=True,
        )
        if payload is None:
            return {
                "schema": CH_ANNOTATIONS_SCHEMA,
                "annotations": {},
                "operations": {},
            }
        value = _decode_json(
            payload,
            label="CH annotations",
            expected=dict,
        )
        if (
            value.get("schema") != CH_ANNOTATIONS_SCHEMA
            or not isinstance(value.get("annotations"), dict)
            or not isinstance(value.get("operations", {}), dict)
        ):
            raise _failure(
                "CH annotation sidecar is invalid",
                code="invalid_ch_annotation_sidecar",
            )
        value.setdefault("operations", {})
        return value

    def _ch_annotation(
        self,
        ref: ReviewSourceRef,
        source_hash: str,
    ) -> Mapping[str, Any] | None:
        stored = self._ch_annotations_document()["annotations"].get(ref.source_id)
        if stored is None:
            return None
        if not isinstance(stored, Mapping):
            raise _failure(
                "CH annotation entry is invalid",
                code="invalid_ch_annotation_sidecar",
                source_ref=ref,
            )
        revision = stored.get("revision")
        if (
            stored.get("namespace") != "ch_library"
            or stored.get("source_id") != ref.source_id
            or stored.get("source_sha256") != source_hash
            or not isinstance(revision, str)
            or re.fullmatch(r"cha-[0-9a-f]{64}", revision) is None
            or not isinstance(stored.get("fields"), Mapping)
        ):
            raise _failure(
                "CH annotation no longer matches its shipped source row",
                code="ch_annotation_source_conflict",
                source_ref=ref,
            )
        return stored

    # -- receipts and replay -----------------------------------------

    def _read_operation_receipt(self, operation_sha256: str) -> _AuditReceipt | None:
        return self._read_receipt_relative(
            self._operation_receipt_relative(operation_sha256)
        )

    def _read_binding(self, ref: ReviewSourceRef) -> _AuditReceipt | None:
        return self._read_receipt_relative(self._binding_relative(ref))

    def _read_receipt_relative(self, relative: PurePosixPath) -> _AuditReceipt | None:
        path = _safe_absolute(self._write_set.root, relative)
        payload = _stable_bytes(path, maximum=MAX_IMPORT_RECEIPT_BYTES, missing_ok=True)
        if payload is None:
            return None
        value = _decode_json(payload, label="book-review import receipt", expected=dict)
        return _AuditReceipt.from_dict(value)

    def _verified_replay_state(self, receipt: _AuditReceipt) -> DestinationReviewState:
        state = self._read_state_unlocked(receipt.source_ref)
        if state is None or not self._receipt_matches_state(
            receipt,
            source_hash=receipt.source_hash_after_import,
            record_revision=state.record_revision,
            metadata=state.metadata_dict,
            assessment=self._assessment(receipt.source_ref),
        ):
            raise _failure(
                "idempotent import result has since been superseded",
                code="book_review_import_operation_superseded",
                source_ref=receipt.source_ref,
            )
        return DestinationReviewState.create(
            metadata=state.metadata_dict,
            record_revision=state.record_revision,
            assessment_sha256=state.assessment_sha256,
            assessment_revision=state.assessment_revision,
            import_receipt=receipt.binding,
            authority_sha256=state.authority_sha256,
        )

    @staticmethod
    def _receipt_matches_state(
        receipt: _AuditReceipt,
        *,
        source_hash: str,
        record_revision: str,
        metadata: Mapping[str, str],
        assessment: ScanAssessmentView | None,
    ) -> bool:
        return bool(
            assessment is not None
            and receipt.source_hash_after_import == source_hash
            and receipt.record_revision_after == record_revision
            and receipt.assessment_revision_after == assessment.revision
            and receipt.assessment_sha256 == assessment.manifest.content_sha256
            and receipt.metadata_sha256 == _digest_json(metadata)
            and assessment.manifest.provenance.source_row_sha256 == source_hash
        )

    # -- small helpers ------------------------------------------------

    @staticmethod
    def _review_fields(value: Mapping[str, Any]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for field in ("marked_price", "scan_priority", "scan_verdict"):
            current = value.get(field)
            if isinstance(current, str) and current:
                fields[field] = current
        return fields

    @staticmethod
    def _patch_review_fields(
        target: dict[str, Any], desired: Mapping[str, str]
    ) -> None:
        for field in ("marked_price", "scan_priority", "scan_verdict"):
            if field in desired:
                target[field] = desired[field]
            else:
                target.pop(field, None)

    def _assessment_directory(self, ref: ReviewSourceRef) -> PurePosixPath:
        key = ScanAssessmentKey(ref.namespace, ref.source_id)
        return self._scan_root / scan_assessment_locator_digest(key)

    def _operation_receipt_relative(self, operation_sha256: str) -> PurePosixPath:
        return self._import_root / "receipts" / (operation_sha256 + ".json")

    def _binding_relative(self, ref: ReviewSourceRef) -> PurePosixPath:
        key = ScanAssessmentKey(ref.namespace, ref.source_id)
        return (
            self._import_root
            / "bindings"
            / (scan_assessment_locator_digest(key) + ".json")
        )

    @staticmethod
    def _request_sha256(request: AtomicBookReviewImportRequest) -> str:
        return _digest_json(
            {
                "operation_id": request.operation_id,
                "export_sha256": request.export_sha256,
                "review_record_id": request.unit.review_record_id,
                "namespace": request.unit.source_ref.namespace,
                "source_id": request.unit.source_ref.source_id,
                "expected_source_hash": request.unit.expected_source_hash,
                "metadata_sha256": _digest_json(request.unit.metadata_dict),
                "reasoning_sha256": request.unit.reasoning.sha256,
                "expected_record_revision": request.expected_record_revision,
                "expected_assessment_revision": (
                    request.expected_assessment_revision or ""
                ),
                "expected_authority_sha256": (request.expected_authority_sha256 or ""),
                "create_assessment": request.create_assessment,
            }
        )

    def _now_value(self, ref: ReviewSourceRef) -> datetime:
        try:
            value = self._clock()
        except Exception as exc:
            raise _failure(
                "book-review import clock failed",
                code="book_review_import_clock_failed",
                source_ref=ref,
                cause_type=type(exc).__name__,
            ) from exc
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise _failure(
                "book-review import clock returned a naive timestamp",
                code="book_review_import_clock_failed",
                source_ref=ref,
            )
        return value

    def _now(self, ref: ReviewSourceRef) -> str:
        return _later_timestamp(self._now_value(ref))

    def _nonce(self, ref: ReviewSourceRef) -> str:
        try:
            value = self._revision_nonce()
        except Exception as exc:
            raise _failure(
                "book-review import revision source failed",
                code="book_review_import_revision_failed",
                source_ref=ref,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
        ):
            raise _failure(
                "book-review import revision source returned invalid data",
                code="book_review_import_revision_failed",
                source_ref=ref,
            )
        return value


__all__ = [
    "BOOK_REVIEW_PREIMPORT_BACKUP_SCHEMA",
    "BOOK_REVIEW_IMPORT_RECEIPT_SCHEMA",
    "BOOK_REVIEW_IMPORT_RELATIVE_ROOT",
    "CH_ANNOTATIONS_SCHEMA",
    "FilesystemBookReviewImportAdapter",
    "MAX_IMPORT_RECEIPT_BYTES",
    "ch_absent_revision",
]
