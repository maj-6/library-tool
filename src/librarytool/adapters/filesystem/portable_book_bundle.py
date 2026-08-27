"""ZIP backup/export and atomic import for Desktop book curation.

This adapter never opens the enrichment database.  It reads only the two
legacy catalogue sources, the mutable CH sidecar, and scan-assessment files.
Import validates the complete archive and every explicit target pin before a
single recoverable write-set transaction publishes metadata, Markdown, and an
audit receipt together.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import stat
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ...catalog_enrichment.importers import IMPORT_PROJECTION_VERSION
from ...engine.errors import RepositoryError
from ...engine.portable_book_bundle import (
    MAX_PORTABLE_BOOK_BUNDLE_BYTES,
    MAX_PORTABLE_BOOK_METADATA_BYTES,
    MAX_PORTABLE_BOOK_RECORDS,
    MAX_PORTABLE_BOOK_SOURCE_EVIDENCE_BYTES,
    PORTABLE_BOOK_BUNDLE_MANIFEST,
    PORTABLE_BOOK_BUNDLE_SCHEMA,
    PORTABLE_BOOK_COPY_FIELDS,
    PortableBookAuthority,
    PortableBookBundle,
    PortableBookBundleConflict,
    PortableBookBundleError,
    PortableBookImportPlan,
    PortableBookImportReceipt,
    PortableBookRecord,
    PortableImportAction,
    PortableImportPin,
    portable_book_canonical_json,
    portable_copy_curation,
)
from ...engine.scan_assessments import (
    MAX_SCAN_ASSESSMENT_BYTES,
    MAX_SCAN_ASSESSMENT_MANIFEST_BYTES,
    ScanAssessmentKey,
    ScanAssessmentManifest,
    ScanAssessmentView,
    canonical_scan_assessment_json,
    scan_assessment_locator_digest,
    validate_scan_assessment_operation_id,
)
from .manual_entry_item_codec import ManualEntryItemCodec
from .recoverable_write_set import RecoverableWriteSet
from .scan_assessment_repository import (
    SCAN_ASSESSMENT_MANIFEST_NAME,
    SCAN_ASSESSMENT_TEXT_NAME,
    FilesystemScanAssessmentRepository,
)
from .whl_catalogue_codec import WhlCatalogueItemCodec


_CH_ANNOTATIONS_SCHEMA = "librarytool.ch-annotations/1"
_IMPORT_RECEIPT_SCHEMA = "librarytool.portable-book-bundle-import/1"
_CH_BASE_VERSION_PREFIX = "chs-"
_CH_REVISION_RE = re.compile(r"^cha-[0-9a-f]{64}$", re.ASCII)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_AUTHORITY_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$", re.ASCII)
_CAPTURE_AUTHORITY_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9_-])?$", re.ASCII
)
_MAX_CATALOG_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_BUNDLE_MANIFEST_BYTES = 4 * 1024 * 1024
_MAX_IMPORT_RECEIPT_BYTES = 4 * 1024 * 1024
_MAX_ZIP_MEMBERS = 1 + MAX_PORTABLE_BOOK_RECORDS * 4
_CAPTURE_OCR_READ_CHUNK_BYTES = 1024 * 1024
_MAX_CAPTURE_OCR_SOURCE_BYTES = 64 * 1024 * 1024
_REGULAR_ZIP_MODE = stat.S_IFREG | 0o600
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))

_BUNDLE_FIELDS = frozenset({"schema", "created_at", "records"})
_RECORD_FIELDS = frozenset(
    {
        "source",
        "book_id",
        "record_version",
        "source_sha256",
        "source_evidence",
        "source_metadata",
        "authority",
        "copy_curation",
        "metadata",
        "assessment",
    }
)
_SOURCE_FIELDS = frozenset({"namespace", "source_id"})
_AUTHORITY_FIELDS = frozenset(
    {"storage_kind", "storage_id", "canonical_item_id", "capture_id"}
)
_SOURCE_EVIDENCE_FIELDS = frozenset({"import_projection_version", "capture_ocr"})
_CAPTURE_OCR_EVIDENCE_FIELDS = frozenset({"path", "sha256", "byte_length"})
_MEMBER_FIELDS = frozenset({"member", "sha256", "byte_size"})
_ASSESSMENT_FIELDS = frozenset(
    {
        "manifest_member",
        "manifest_sha256",
        "manifest_byte_size",
        "text_member",
        "text_sha256",
        "text_byte_size",
        "revision",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "request_sha256",
        "operation_sha256",
        "bundle_sha256",
        "state_sha256",
        "created_at",
        "actions",
    }
)
_RECEIPT_ACTION_FIELDS = frozenset(
    {
        "namespace",
        "source_id",
        "metadata",
        "assessment",
        "current_record_version",
        "current_assessment_revision",
        "result_record_version",
        "result_assessment_revision",
        "assessment_sha256",
        "conflicts",
    }
)


def _strict_json(payload: bytes, *, artifact: str) -> Any:
    def unique(pairs):
        result: dict[str, Any] = {}
        for name, value in pairs:
            if name in result:
                raise ValueError(f"duplicate member {name!r}")
            result[name] = value
        return result

    try:
        return json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite number {value}")
            ),
        )
    except (UnicodeError, ValueError) as exc:
        raise PortableBookBundleError(
            f"{artifact} is not strict UTF-8 JSON",
            code="invalid_portable_book_json",
            details={"artifact": artifact},
        ) from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validated_digest(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PortableBookBundleError(
            f"{field} must be a lower-case SHA-256 digest",
            details={"field": field},
        )
    return value


def _validated_size(value: Any, *, field: str, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > maximum
    ):
        raise PortableBookBundleError(
            f"{field} is outside its byte limit",
            details={"field": field},
        )
    return value


def _safe_member(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PortableBookBundleError(
            f"{field} must name a bundle member",
            details={"field": field},
        )
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise PortableBookBundleError(
            f"{field} is not a safe relative member",
            code="unsafe_portable_book_member",
            details={"field": field},
        )
    return value


def _safe_relative(value: str | PurePosixPath, *, field: str) -> PurePosixPath:
    raw = str(value)
    pure = PurePosixPath(raw)
    if (
        not raw
        or pure.is_absolute()
        or pure.as_posix() != raw
        or "\\" in raw
        or ":" in raw
        or any(part in {"", ".", ".."} for part in pure.parts)
        or pure.parts[0].casefold() == ".transactions"
    ):
        raise ValueError(f"{field} must be a normalized relative path")
    return pure


def _timestamp(clock: Callable[[], datetime]) -> str:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RepositoryError(
            "portable bundle clock returned a naive timestamp",
            code="portable_bundle_clock_failed",
        )
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _timestamp_after(value: str, prior: str) -> str:
    current = datetime.fromisoformat(value.replace("Z", "+00:00"))
    previous = datetime.fromisoformat(prior.replace("Z", "+00:00"))
    if current <= previous:
        current = previous + timedelta(microseconds=1)
    return (
        current.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _manual_capture_id(value: Any) -> str:
    """Normalize one capture identity exactly like the enrichment importer."""

    if value is None:
        return ""
    if isinstance(value, str):
        result = " ".join(value.split())
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result = str(value)
    else:
        return ""
    # Capture ids are directory identities, never paths.  Current Desktop
    # capture ids are considerably narrower than this guard, but retaining
    # spaces and case here matches the legacy importer's text projection.
    if (
        not result
        or len(result) > 255
        or result in {".", ".."}
        or "/" in result
        or "\\" in result
        or ":" in result
        or "\0" in result
    ):
        return ""
    return result


def _capture_ocr_snapshot(
    captures_path: str | Path | None,
    row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return only the external OCR integrity manifest, never its prose."""

    capture_id = _manual_capture_id(row.get("capture_id"))
    if captures_path is None or not capture_id:
        return None
    root = Path(os.path.abspath(captures_path))
    path = root / capture_id / "ocr.txt"
    try:
        digest = hashlib.sha256()
        byte_length = 0
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            while True:
                chunk = stream.read(_CAPTURE_OCR_READ_CHUNK_BYTES)
                if not chunk:
                    break
                byte_length += len(chunk)
                if byte_length > _MAX_CAPTURE_OCR_SOURCE_BYTES:
                    raise PortableBookBundleError(
                        "capture OCR exceeds its source-evidence byte limit",
                        code="portable_capture_ocr_too_large",
                    )
                digest.update(chunk)
    except OSError:
        return None
    return {
        "path": f"captures/{capture_id}/ocr.txt",
        "sha256": digest.hexdigest(),
        "byte_length": byte_length,
    }


def _validated_source_evidence(
    namespace: str,
    row: Mapping[str, Any],
    value: Any,
) -> dict[str, Any]:
    if namespace not in {"manual_entries", "ch_library"}:
        raise PortableBookBundleError(
            "portable bundle contains an unsupported catalogue namespace",
            code="unsupported_portable_book_namespace",
            details={"namespace": namespace},
        )
    if not isinstance(row, Mapping):
        raise PortableBookBundleError(
            "catalogue source row must be an object",
            details={"namespace": namespace},
        )
    if not isinstance(value, Mapping):
        raise PortableBookBundleError(
            "portable source evidence must be an object",
            code="invalid_portable_source_evidence",
        )
    fields = frozenset(value)
    if (
        "import_projection_version" not in fields
        or fields - _SOURCE_EVIDENCE_FIELDS
        or value["import_projection_version"] != IMPORT_PROJECTION_VERSION
        or isinstance(value["import_projection_version"], bool)
    ):
        raise PortableBookBundleError(
            "portable source evidence has an unsupported projection",
            code="invalid_portable_source_evidence",
        )
    result: dict[str, Any] = {
        "import_projection_version": IMPORT_PROJECTION_VERSION,
    }
    capture_raw = value.get("capture_ocr")
    if "capture_ocr" in fields:
        capture_id = _manual_capture_id(row.get("capture_id"))
        if (
            namespace != "manual_entries"
            or not capture_id
            or not isinstance(capture_raw, Mapping)
            or frozenset(capture_raw) != _CAPTURE_OCR_EVIDENCE_FIELDS
            or capture_raw.get("path") != f"captures/{capture_id}/ocr.txt"
        ):
            raise PortableBookBundleError(
                "portable capture OCR evidence does not match its source",
                code="invalid_portable_source_evidence",
            )
        result["capture_ocr"] = {
            "path": capture_raw["path"],
            "sha256": _validated_digest(
                capture_raw["sha256"],
                field="source_evidence.capture_ocr.sha256",
            ),
            "byte_length": _validated_size(
                capture_raw["byte_length"],
                field="source_evidence.capture_ocr.byte_length",
                maximum=_MAX_CAPTURE_OCR_SOURCE_BYTES,
            ),
        }
    if (
        len(portable_book_canonical_json(result))
        > MAX_PORTABLE_BOOK_SOURCE_EVIDENCE_BYTES
    ):
        raise PortableBookBundleError(
            "portable source evidence exceeds its byte limit",
            code="invalid_portable_source_evidence",
        )
    return result


def catalogue_source_evidence(
    namespace: str,
    row: Mapping[str, Any],
    *,
    captures_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build the bounded source-dependency manifest used by enrichment."""

    evidence = _validated_source_evidence(
        namespace,
        row,
        {"import_projection_version": IMPORT_PROJECTION_VERSION},
    )
    if namespace == "manual_entries":
        capture_ocr = _capture_ocr_snapshot(captures_path, row)
        if capture_ocr is not None:
            evidence["capture_ocr"] = capture_ocr
    return _validated_source_evidence(namespace, row, evidence)


def catalogue_source_sha256(
    namespace: str,
    row: Mapping[str, Any],
    *,
    captures_path: str | Path | None = None,
    source_evidence: Mapping[str, Any] | None = None,
) -> str:
    """Hash the source-data projection shared with catalog enrichment."""

    if captures_path is not None and source_evidence is not None:
        raise ValueError("captures_path and source_evidence are mutually exclusive")
    evidence = (
        catalogue_source_evidence(
            namespace,
            row,
            captures_path=captures_path,
        )
        if source_evidence is None
        else _validated_source_evidence(namespace, row, source_evidence)
    )
    source_data = copy.deepcopy(dict(row))
    source_data["_catalog_enrichment_source_evidence"] = evidence
    return _sha256(portable_book_canonical_json(source_data))


@dataclass(frozen=True, slots=True)
class ResolvedManualBookAuthority:
    """Original manual source plus its one active mutable catalogue row."""

    source_id: str
    source_row: Mapping[str, Any]
    source_exists: bool
    storage_kind: str
    storage_id: str
    active_row: Mapping[str, Any]
    active_exists: bool
    record_revision: str | None
    canonical_item_id: str
    capture_id: str

    @property
    def portable(self) -> PortableBookAuthority:
        return PortableBookAuthority(
            storage_kind=self.storage_kind,
            storage_id=self.storage_id,
            canonical_item_id=self.canonical_item_id,
            capture_id=self.capture_id,
        )


def _build_codec() -> WhlCatalogueItemCodec:
    return WhlCatalogueItemCodec(
        advance_revision=lambda previous: previous or "unused",
        category_ids_for=tuple,
        validate_representation_manifest=lambda _raw: None,
    )


def _validated_build(build_id: Any, row: Any) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(build_id, str) or not _AUTHORITY_ALIAS_RE.fullmatch(build_id):
        raise PortableBookBundleError(
            "WHL builds contains an invalid storage identity",
            code="invalid_portable_build_store",
        )
    if not isinstance(row, Mapping):
        raise PortableBookBundleError(
            "WHL builds contains a non-object record",
            code="invalid_portable_build_store",
        )
    codec = _build_codec()
    try:
        codec.validate_managed_record(build_id, row)
        codec.validate_catalogue_metadata(
            {
                name: value
                for name, value in row.items()
                if name not in codec.managed_fields
            }
        )
    except (TypeError, ValueError) as exc:
        raise PortableBookBundleError(
            "WHL build failed its storage codec",
            code="invalid_portable_build_store",
            details={"storage_id": build_id},
        ) from exc
    return build_id, row


def _canonical_aliases(row: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    containers: list[Mapping[str, Any]] = [row]
    extra = row.get("extra")
    if isinstance(extra, Mapping):
        containers.append(extra)
    for container in containers:
        for field in (
            "canonical_item_id",
            "canonical_book_id",
            "capture_book_id",
            "lib_book_id",
            "book_id",
        ):
            value = container.get(field)
            if value in (None, ""):
                continue
            if not isinstance(value, str) or not _AUTHORITY_ALIAS_RE.fullmatch(value):
                raise PortableBookBundleConflict(
                    "manual authority contains an invalid canonical alias",
                    code="invalid_portable_authority_alias",
                    details={"field": field},
                )
            values.append(value)
    return tuple(dict.fromkeys(values))


def resolve_manual_book_authority(
    source_id: str,
    manual_document: Mapping[str, Any],
    builds_document: Mapping[str, Any],
    source_fallback: Mapping[str, Any] | None = None,
) -> ResolvedManualBookAuthority:
    """Resolve a manual source to one active row without guessing aliases."""

    if not isinstance(manual_document, Mapping) or not isinstance(
        builds_document, Mapping
    ):
        raise PortableBookBundleError(
            "portable manual authority stores must be objects",
            code="invalid_portable_authority_store",
        )
    current = manual_document.get(source_id)
    source_exists = isinstance(current, Mapping)
    source_row = current if source_exists else source_fallback
    if not isinstance(source_row, Mapping):
        raise PortableBookBundleError(
            "selected manual source row does not exist",
            code="portable_book_source_not_found",
            details={"namespace": "manual_entries", "source_id": source_id},
        )
    try:
        ManualEntryItemCodec.validate_record(source_id, source_row)
    except (TypeError, ValueError) as exc:
        raise PortableBookBundleError(
            "manual source failed its storage codec",
            code="invalid_portable_manual_store",
        ) from exc
    raw_capture_id = source_row.get("capture_id")
    capture_id = raw_capture_id if isinstance(raw_capture_id, str) else ""
    if capture_id and not _CAPTURE_AUTHORITY_RE.fullmatch(capture_id):
        raise PortableBookBundleConflict(
            "manual source has an invalid capture identity",
            code="invalid_portable_authority_capture",
        )
    if capture_id:
        manual_claims = [
            entry_id
            for entry_id, row in manual_document.items()
            if isinstance(row, Mapping) and row.get("capture_id") == capture_id
        ]
        if not source_exists:
            manual_claims.append(source_id)
        if len(set(manual_claims)) > 1:
            raise PortableBookBundleConflict(
                "a capture identity has duplicate manual claims",
                code="duplicate_portable_authority_claim",
                details={"capture_id": capture_id},
            )
    build_claims: list[tuple[str, Mapping[str, Any]]] = []
    for raw_build_id, raw_build in builds_document.items():
        build_id, build = _validated_build(raw_build_id, raw_build)
        if capture_id and build.get("capture_id") == capture_id:
            build_claims.append((build_id, build))
    if len(build_claims) > 1:
        raise PortableBookBundleConflict(
            "a capture identity has duplicate active build claims",
            code="duplicate_portable_authority_claim",
            details={"capture_id": capture_id},
        )
    if build_claims:
        storage_id, active_row = build_claims[0]
        storage_kind = "whl_builds"
        active_exists = True
        record_revision = WhlCatalogueItemCodec.record_revision(storage_id, active_row)
    else:
        storage_id = source_id
        active_row = source_row
        storage_kind = "manual_entries"
        active_exists = source_exists
        record_revision = (
            ManualEntryItemCodec.record_revision(source_id, source_row)
            if source_exists
            else None
        )
    aliases = list(_canonical_aliases(source_row))
    if storage_kind == "whl_builds":
        aliases.extend(_canonical_aliases(active_row))
    distinct = tuple(dict.fromkeys(aliases))
    if len(distinct) > 1:
        raise PortableBookBundleConflict(
            "manual source and active build have conflicting canonical aliases",
            code="portable_authority_alias_conflict",
            details={"capture_id": capture_id},
        )
    return ResolvedManualBookAuthority(
        source_id=source_id,
        source_row=source_row,
        source_exists=source_exists,
        storage_kind=storage_kind,
        storage_id=storage_id,
        active_row=active_row,
        active_exists=active_exists,
        record_revision=record_revision,
        canonical_item_id=distinct[0] if distinct else "",
        capture_id=capture_id,
    )


def _detached_metadata(record: PortableBookRecord) -> dict[str, Any]:
    value = _strict_json(
        portable_book_canonical_json(record.metadata),
        artifact=f"metadata for {record.source.source_reference}",
    )
    assert isinstance(value, dict)
    return value


def _detached_source_metadata(record: PortableBookRecord) -> dict[str, Any]:
    value = _strict_json(
        portable_book_canonical_json(record.source_metadata),
        artifact=f"source metadata for {record.source.source_reference}",
    )
    assert isinstance(value, dict)
    return value


class PortableBookBundleZipCodec:
    """Encode/decode a bounded manifest-plus-file ZIP archive."""

    def encode(
        self,
        records: Iterable[PortableBookRecord],
        *,
        created_at: str,
    ) -> bytes:
        bundle = PortableBookBundle(
            schema=PORTABLE_BOOK_BUNDLE_SCHEMA,
            created_at=created_at,
            records=tuple(records),
        )
        members: dict[str, bytes] = {}
        manifest_records: list[dict[str, Any]] = []
        member_bytes = 0
        for record in bundle.records:
            locator = scan_assessment_locator_digest(record.source)
            metadata_member = f"records/{locator}.json"
            source_metadata_member = f"records/{locator}.source.json"
            metadata_payload = portable_book_canonical_json(record.metadata) + b"\n"
            source_metadata_payload = (
                portable_book_canonical_json(record.source_metadata) + b"\n"
            )
            members[metadata_member] = metadata_payload
            members[source_metadata_member] = source_metadata_payload
            member_bytes += len(metadata_payload) + len(source_metadata_payload)
            assessment_descriptor: dict[str, Any] | None = None
            if record.assessment is not None:
                manifest_member = f"scan_assessments/{locator}/manifest.json"
                text_member = f"scan_assessments/{locator}/assessment.md"
                assessment_manifest = (
                    canonical_scan_assessment_json(record.assessment.manifest.as_dict())
                    + b"\n"
                )
                text_payload = record.assessment.text.encode("utf-8", errors="strict")
                members[manifest_member] = assessment_manifest
                members[text_member] = text_payload
                member_bytes += len(assessment_manifest) + len(text_payload)
                assessment_descriptor = {
                    "manifest_member": manifest_member,
                    "manifest_sha256": _sha256(assessment_manifest),
                    "manifest_byte_size": len(assessment_manifest),
                    "text_member": text_member,
                    "text_sha256": _sha256(text_payload),
                    "text_byte_size": len(text_payload),
                    "revision": record.assessment.revision,
                }
            manifest_records.append(
                {
                    "source": record.source.as_dict(),
                    "book_id": record.book_id,
                    "record_version": record.record_version,
                    "source_sha256": record.source_sha256,
                    "source_evidence": dict(record.source_evidence),
                    "source_metadata": {
                        "member": source_metadata_member,
                        "sha256": _sha256(source_metadata_payload),
                        "byte_size": len(source_metadata_payload),
                    },
                    "authority": record.authority.as_dict(),
                    "copy_curation": dict(record.copy_curation),
                    "metadata": {
                        "member": metadata_member,
                        "sha256": _sha256(metadata_payload),
                        "byte_size": len(metadata_payload),
                    },
                    "assessment": assessment_descriptor,
                }
            )
            if member_bytes > MAX_PORTABLE_BOOK_BUNDLE_BYTES:
                raise PortableBookBundleError(
                    "portable bundle members exceed their aggregate byte limit",
                    code="portable_book_bundle_too_large",
                )
        manifest_payload = (
            portable_book_canonical_json(
                {
                    "schema": PORTABLE_BOOK_BUNDLE_SCHEMA,
                    "created_at": created_at,
                    "records": manifest_records,
                }
            )
            + b"\n"
        )
        if len(manifest_payload) > _MAX_BUNDLE_MANIFEST_BYTES:
            raise PortableBookBundleError(
                "portable bundle manifest exceeds its byte limit",
                code="portable_book_bundle_too_large",
            )
        if member_bytes + len(manifest_payload) > MAX_PORTABLE_BOOK_BUNDLE_BYTES:
            raise PortableBookBundleError(
                "portable bundle expands beyond its aggregate byte limit",
                code="portable_book_bundle_too_large",
            )
        members = {PORTABLE_BOOK_BUNDLE_MANIFEST: manifest_payload, **members}
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=False,
        ) as archive:
            for name, payload in members.items():
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = _REGULAR_ZIP_MODE << 16
                archive.writestr(info, payload)
        result = output.getvalue()
        if len(result) > MAX_PORTABLE_BOOK_BUNDLE_BYTES:
            raise PortableBookBundleError(
                "portable bundle exceeds its archive byte limit",
                code="portable_book_bundle_too_large",
            )
        return result

    def decode(self, data: bytes | bytearray | memoryview) -> PortableBookBundle:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("portable bundle archive must be bytes-like")
        payload = bytes(data)
        if not payload or len(payload) > MAX_PORTABLE_BOOK_BUNDLE_BYTES:
            raise PortableBookBundleError(
                "portable bundle archive size is outside its limit",
                code="portable_book_bundle_too_large",
            )
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload), mode="r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise PortableBookBundleError(
                "portable bundle is not a readable ZIP archive",
                code="invalid_portable_book_zip",
            ) from exc
        with archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_ZIP_MEMBERS:
                raise PortableBookBundleError(
                    "portable bundle member count is outside its limit",
                    code="portable_book_bundle_too_large",
                )
            by_name: dict[str, zipfile.ZipInfo] = {}
            total_uncompressed = 0
            for info in infos:
                name = _safe_member(info.filename, field="zip member")
                if name in by_name:
                    raise PortableBookBundleError(
                        "portable bundle contains a duplicate member",
                        code="duplicate_portable_book_member",
                        details={"member": name},
                    )
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                unix_kind = stat.S_IFMT(unix_mode)
                if info.is_dir() or (unix_kind not in {0, stat.S_IFREG}):
                    raise PortableBookBundleError(
                        "portable bundle member is not a regular file",
                        code="unsafe_portable_book_member",
                        details={"member": name},
                    )
                if info.file_size < 0:
                    raise PortableBookBundleError(
                        "portable bundle member size is invalid"
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_PORTABLE_BOOK_BUNDLE_BYTES:
                    raise PortableBookBundleError(
                        "portable bundle expands beyond its byte limit",
                        code="portable_book_bundle_too_large",
                    )
                by_name[name] = info
            manifest_info = by_name.get(PORTABLE_BOOK_BUNDLE_MANIFEST)
            if (
                manifest_info is None
                or manifest_info.file_size > _MAX_BUNDLE_MANIFEST_BYTES
            ):
                raise PortableBookBundleError(
                    "portable bundle manifest is missing or oversized",
                    code="invalid_portable_book_manifest",
                )
            try:
                manifest_payload = archive.read(manifest_info)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise PortableBookBundleError(
                    "portable bundle manifest failed ZIP integrity validation",
                    code="invalid_portable_book_zip",
                ) from exc
            raw = _strict_json(manifest_payload, artifact=PORTABLE_BOOK_BUNDLE_MANIFEST)
            if not isinstance(raw, Mapping) or frozenset(raw) != _BUNDLE_FIELDS:
                raise PortableBookBundleError(
                    "portable bundle manifest fields do not match its schema",
                    code="invalid_portable_book_manifest",
                )
            if raw["schema"] != PORTABLE_BOOK_BUNDLE_SCHEMA:
                raise PortableBookBundleError(
                    "portable book bundle schema is unsupported",
                    code="unsupported_portable_book_bundle_schema",
                )
            raw_records = raw["records"]
            if (
                not isinstance(raw_records, list)
                or not raw_records
                or len(raw_records) > MAX_PORTABLE_BOOK_RECORDS
            ):
                raise PortableBookBundleError(
                    "portable bundle record count is outside its limit",
                    code="invalid_portable_book_manifest",
                )
            expected_members = {PORTABLE_BOOK_BUNDLE_MANIFEST}
            records: list[PortableBookRecord] = []
            for index, raw_record in enumerate(raw_records):
                records.append(
                    self._decode_record(
                        archive,
                        by_name,
                        raw_record,
                        index=index,
                        expected_members=expected_members,
                    )
                )
            extras = set(by_name) - expected_members
            if extras:
                raise PortableBookBundleError(
                    "portable bundle contains undeclared members",
                    code="undeclared_portable_book_member",
                    details={"members": sorted(extras)[:10]},
                )
        return PortableBookBundle(
            schema=raw["schema"],
            created_at=raw["created_at"],
            records=tuple(records),
            archive_sha256=_sha256(payload),
        )

    def _decode_record(
        self,
        archive: zipfile.ZipFile,
        by_name: Mapping[str, zipfile.ZipInfo],
        raw: Any,
        *,
        index: int,
        expected_members: set[str],
    ) -> PortableBookRecord:
        if not isinstance(raw, Mapping) or frozenset(raw) != _RECORD_FIELDS:
            raise PortableBookBundleError(
                "portable record fields do not match the schema",
                details={"record": index},
            )
        source_raw = raw["source"]
        if (
            not isinstance(source_raw, Mapping)
            or frozenset(source_raw) != _SOURCE_FIELDS
        ):
            raise PortableBookBundleError(
                "portable record source fields are invalid",
                details={"record": index},
            )
        try:
            source = ScanAssessmentKey(source_raw["namespace"], source_raw["source_id"])
        except Exception as exc:
            raise PortableBookBundleError(
                "portable record source identity is invalid",
                details={"record": index},
            ) from exc
        metadata_descriptor = raw["metadata"]
        metadata_payload = self._declared_member(
            archive,
            by_name,
            metadata_descriptor,
            descriptor_fields=_MEMBER_FIELDS,
            maximum=MAX_PORTABLE_BOOK_METADATA_BYTES + 1,
            label=f"records[{index}].metadata",
            expected_members=expected_members,
        )
        metadata = _strict_json(
            metadata_payload, artifact=f"metadata for {source.source_reference}"
        )
        source_metadata_payload = self._declared_member(
            archive,
            by_name,
            raw["source_metadata"],
            descriptor_fields=_MEMBER_FIELDS,
            maximum=MAX_PORTABLE_BOOK_METADATA_BYTES + 1,
            label=f"records[{index}].source_metadata",
            expected_members=expected_members,
        )
        source_metadata = _strict_json(
            source_metadata_payload,
            artifact=f"source metadata for {source.source_reference}",
        )
        source_evidence = _validated_source_evidence(
            source.namespace,
            source_metadata,
            raw["source_evidence"],
        )
        authority_raw = raw["authority"]
        if (
            not isinstance(authority_raw, Mapping)
            or frozenset(authority_raw) != _AUTHORITY_FIELDS
        ):
            raise PortableBookBundleError(
                "portable authority descriptor fields are invalid",
                details={"record": index},
            )
        authority = PortableBookAuthority(**authority_raw)
        assessment: ScanAssessmentView | None = None
        assessment_raw = raw["assessment"]
        if assessment_raw is not None:
            if (
                not isinstance(assessment_raw, Mapping)
                or frozenset(assessment_raw) != _ASSESSMENT_FIELDS
            ):
                raise PortableBookBundleError(
                    "portable assessment descriptor fields are invalid",
                    details={"record": index},
                )
            manifest_payload = self._declared_named_member(
                archive,
                by_name,
                member=assessment_raw["manifest_member"],
                digest=assessment_raw["manifest_sha256"],
                byte_size=assessment_raw["manifest_byte_size"],
                maximum=MAX_SCAN_ASSESSMENT_MANIFEST_BYTES,
                label=f"records[{index}].assessment.manifest",
                expected_members=expected_members,
            )
            text_payload = self._declared_named_member(
                archive,
                by_name,
                member=assessment_raw["text_member"],
                digest=assessment_raw["text_sha256"],
                byte_size=assessment_raw["text_byte_size"],
                maximum=MAX_SCAN_ASSESSMENT_BYTES,
                label=f"records[{index}].assessment.text",
                expected_members=expected_members,
            )
            manifest_raw = _strict_json(
                manifest_payload,
                artifact=f"assessment manifest for {source.source_reference}",
            )
            try:
                scan_manifest = ScanAssessmentManifest.from_dict(manifest_raw)
                text = text_payload.decode("utf-8", errors="strict")
                assessment = ScanAssessmentView(scan_manifest, text)
            except Exception as exc:
                raise PortableBookBundleError(
                    "portable assessment manifest or Markdown failed integrity validation",
                    code="invalid_portable_scan_assessment",
                    details={"record": index},
                ) from exc
            if assessment_raw["revision"] != assessment.revision:
                raise PortableBookBundleError(
                    "portable assessment revision does not match its manifest",
                    code="invalid_portable_scan_assessment",
                    details={"record": index},
                )
        record = PortableBookRecord(
            source=source,
            record_version=raw["record_version"],
            source_sha256=raw["source_sha256"],
            source_evidence=source_evidence,
            source_metadata=source_metadata,
            authority=authority,
            metadata=metadata,
            assessment=assessment,
            book_id=raw["book_id"],
        )
        if raw["copy_curation"] != dict(record.copy_curation):
            raise PortableBookBundleError(
                "copy-curation fields do not match the metadata member",
                code="portable_copy_curation_mismatch",
                details={"record": index},
            )
        return record

    def _declared_member(
        self,
        archive: zipfile.ZipFile,
        by_name: Mapping[str, zipfile.ZipInfo],
        descriptor: Any,
        *,
        descriptor_fields: frozenset[str],
        maximum: int,
        label: str,
        expected_members: set[str],
    ) -> bytes:
        if (
            not isinstance(descriptor, Mapping)
            or frozenset(descriptor) != descriptor_fields
        ):
            raise PortableBookBundleError(f"{label} descriptor is invalid")
        return self._declared_named_member(
            archive,
            by_name,
            member=descriptor["member"],
            digest=descriptor["sha256"],
            byte_size=descriptor["byte_size"],
            maximum=maximum,
            label=label,
            expected_members=expected_members,
        )

    def _declared_named_member(
        self,
        archive: zipfile.ZipFile,
        by_name: Mapping[str, zipfile.ZipInfo],
        *,
        member: Any,
        digest: Any,
        byte_size: Any,
        maximum: int,
        label: str,
        expected_members: set[str],
    ) -> bytes:
        name = _safe_member(member, field=f"{label}.member")
        expected_digest = _validated_digest(digest, field=f"{label}.sha256")
        expected_size = _validated_size(
            byte_size, field=f"{label}.byte_size", maximum=maximum
        )
        info = by_name.get(name)
        if info is None or info.file_size != expected_size:
            raise PortableBookBundleError(
                f"{label} member is missing or has the wrong size",
                code="portable_book_member_size_mismatch",
                details={"member": name},
            )
        if name in expected_members:
            raise PortableBookBundleError(
                "two bundle records reference the same member",
                code="duplicate_portable_book_member_reference",
                details={"member": name},
            )
        try:
            member_payload = archive.read(info)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise PortableBookBundleError(
                f"{label} member failed ZIP integrity validation",
                code="invalid_portable_book_zip",
                details={"member": name},
            ) from exc
        if (
            len(member_payload) != expected_size
            or _sha256(member_payload) != expected_digest
        ):
            raise PortableBookBundleError(
                f"{label} member failed SHA-256 validation",
                code="portable_book_member_hash_mismatch",
                details={"member": name},
            )
        expected_members.add(name)
        return member_payload


class FilesystemPortableBookBundleService:
    """Export and atomically restore real Desktop catalogue stores.

    Relative mutable paths are resolved beneath ``write_set.root``.  A CH
    source path is separately supplied because packaged ``ch_library.json`` is
    read-only application data and may live outside that mutable authority.
    Host code should also hold its existing in-process manual/CH locks while it
    calls ``plan_import``/``commit_import``; the write set still detects an
    uncoordinated external rewrite and fails closed.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        ch_library_path: str | Path,
        manual_entries_relative: str | PurePosixPath = "manual_entries.json",
        builds_relative: str | PurePosixPath = "whl_builds.json",
        ch_annotations_relative: str | PurePosixPath = "ch_annotations.json",
        scan_assessments_relative: str | PurePosixPath = "scan_assessments",
        receipts_relative: str | PurePosixPath = "portable_bundle_imports",
        captures_path: str | Path | None = None,
        manual_authority_resolver: Callable[
            [str, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any] | None],
            ResolvedManualBookAuthority,
        ] = resolve_manual_book_authority,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        self._write_set = write_set
        self._manual_relative = _safe_relative(
            manual_entries_relative, field="manual_entries_relative"
        )
        self._builds_relative = _safe_relative(builds_relative, field="builds_relative")
        self._ch_annotations_relative = _safe_relative(
            ch_annotations_relative, field="ch_annotations_relative"
        )
        self._scan_relative = _safe_relative(
            scan_assessments_relative, field="scan_assessments_relative"
        )
        self._receipts_relative = _safe_relative(
            receipts_relative, field="receipts_relative"
        )
        roots = (
            self._manual_relative,
            self._builds_relative,
            self._ch_annotations_relative,
            self._scan_relative,
            self._receipts_relative,
        )
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise ValueError("portable bundle storage targets must not overlap")
        # ``resolve`` would hide a configured final-component symlink before
        # the safe reader can reject it.  ``abspath`` normalizes without
        # following the packaged source path.
        self._ch_library_path = Path(os.path.abspath(ch_library_path))
        self._captures_path = (
            Path(os.path.abspath(captures_path)) if captures_path is not None else None
        )
        if not callable(manual_authority_resolver):
            raise TypeError("manual_authority_resolver must be callable")
        self._manual_authority_resolver = manual_authority_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._codec = PortableBookBundleZipCodec()
        self._assessments = FilesystemScanAssessmentRepository(
            write_set,
            relative_root=self._scan_relative,
        )

    @property
    def codec(self) -> PortableBookBundleZipCodec:
        return self._codec

    def export_bundle(self, sources: Iterable[ScanAssessmentKey]) -> bytes:
        requested = tuple(sources)
        if not requested or len(set(requested)) != len(requested):
            raise PortableBookBundleError(
                "export requires a non-empty unique source selection",
                code="invalid_portable_book_selection",
            )
        with self._write_set.workspace_lease():
            manual = self._manual_document()
            builds = self._builds_document()
            ch_rows = self._ch_rows()
            ch_document = self._ch_annotations_document()
            records = tuple(
                self._export_record(key, manual, builds, ch_rows, ch_document)
                for key in requested
            )
        return self._codec.encode(records, created_at=_timestamp(self._clock))

    def decode_bundle(
        self, archive: bytes | bytearray | memoryview
    ) -> PortableBookBundle:
        return self._codec.decode(archive)

    def current_pins(
        self, sources: Iterable[ScanAssessmentKey]
    ) -> Mapping[ScanAssessmentKey, PortableImportPin]:
        """Return exact current CAS tokens for an explicit source selection.

        This is a read helper, not implicit approval: callers still pass the
        returned (or previously displayed) pins into ``plan_import``.
        """

        requested = tuple(sources)
        if not requested or len(set(requested)) != len(requested):
            raise PortableBookBundleError(
                "CAS inspection requires a non-empty unique source selection",
                code="invalid_portable_book_selection",
            )
        with self._write_set.workspace_lease():
            manual = self._manual_document()
            builds = self._builds_document()
            ch_rows = self._ch_rows()
            ch_document = self._ch_annotations_document()
            result: dict[ScanAssessmentKey, PortableImportPin] = {}
            for key in requested:
                self._supported_key(key)
                if key.namespace == "manual_entries":
                    raw = manual.get(key.source_id)
                    if raw is None:
                        record_version = None
                        source_record_version = None
                    elif isinstance(raw, Mapping):
                        resolved = self._manual_authority_resolver(
                            key.source_id,
                            manual,
                            builds,
                            None,
                        )
                        record_version = resolved.record_revision
                        source_record_version = ManualEntryItemCodec.record_revision(
                            key.source_id,
                            raw,
                        )
                    else:
                        raise RepositoryError(
                            "current manual entry is not an object",
                            code="invalid_manual_entries_store",
                            details=key.as_dict(),
                        )
                else:
                    source_record_version = None
                    index = self._ch_index(key, ch_rows)
                    source_hash = catalogue_source_sha256(key.namespace, ch_rows[index])
                    annotation = self._verified_annotation(
                        key,
                        source_hash,
                        ch_document["annotations"].get(key.source_id),
                    )
                    record_version = (
                        annotation["revision"]
                        if annotation is not None
                        else _CH_BASE_VERSION_PREFIX + source_hash
                    )
                assessment = self._assessments._read_unlocked(key)
                result[key] = PortableImportPin(
                    record_version=record_version,
                    assessment_revision=(
                        assessment.revision if assessment is not None else None
                    ),
                    source_record_version=source_record_version,
                )
        return result

    def plan_import(
        self,
        bundle: PortableBookBundle,
        pins: Mapping[ScanAssessmentKey, PortableImportPin],
    ) -> PortableBookImportPlan:
        self._require_bundle_and_pins(bundle, pins)
        with self._write_set.workspace_lease():
            return self._plan_locked(bundle, pins)

    def archive_pins_for_import(
        self,
        bundle: PortableBookBundle,
    ) -> Mapping[ScanAssessmentKey, PortableImportPin]:
        """Bind a dry-run to archive CAS, allowing only absent-target creates."""

        if not isinstance(bundle, PortableBookBundle):
            raise TypeError("bundle must be a PortableBookBundle")
        with self._write_set.workspace_lease():
            manual = self._manual_document()
            builds = self._builds_document()
            ch_rows = self._ch_rows()
            ch_document = self._ch_annotations_document()
            result: dict[ScanAssessmentKey, PortableImportPin] = {}
            for record in bundle.records:
                key = record.source
                assessment = self._assessments._read_unlocked(key)
                assessment_pin = (
                    None
                    if assessment is None
                    else record.assessment.revision
                    if record.assessment is not None
                    else None
                )
                if key.namespace == "manual_entries":
                    source_exists = isinstance(manual.get(key.source_id), Mapping)
                    resolved = self._manual_authority_resolver(
                        key.source_id,
                        manual,
                        builds,
                        record.source_metadata,
                    )
                    archived_source_revision = ManualEntryItemCodec.record_revision(
                        key.source_id,
                        record.source_metadata,
                    )
                    if record.authority.storage_kind == "whl_builds":
                        record_pin = (
                            record.record_version
                            if resolved.storage_kind == "whl_builds"
                            and resolved.active_exists
                            else None
                        )
                    else:
                        record_pin = record.record_version if source_exists else None
                    result[key] = PortableImportPin(
                        record_version=record_pin,
                        assessment_revision=assessment_pin,
                        source_record_version=(
                            archived_source_revision if source_exists else None
                        ),
                    )
                else:
                    index = self._ch_index(key, ch_rows)
                    source_hash = catalogue_source_sha256(key.namespace, ch_rows[index])
                    annotation = self._verified_annotation(
                        key,
                        source_hash,
                        ch_document["annotations"].get(key.source_id),
                    )
                    current_version = (
                        annotation["revision"]
                        if annotation is not None
                        else _CH_BASE_VERSION_PREFIX + source_hash
                    )
                    record_pin = (
                        current_version
                        if annotation is None
                        and not record.record_version.startswith(
                            _CH_BASE_VERSION_PREFIX
                        )
                        else record.record_version
                    )
                    result[key] = PortableImportPin(
                        record_version=record_pin,
                        assessment_revision=assessment_pin,
                    )
            return result

    def commit_import(
        self,
        plan: PortableBookImportPlan,
        *,
        operation_id: str,
    ) -> PortableBookImportReceipt:
        if not isinstance(plan, PortableBookImportPlan):
            raise TypeError("plan must be a PortableBookImportPlan")
        operation_id = validate_scan_assessment_operation_id(operation_id)
        if not plan.bundle.archive_sha256:
            raise PortableBookBundleError(
                "commit requires a bundle decoded from exact archive bytes",
                code="unbound_portable_book_bundle",
            )
        request_sha256 = self._request_sha256(plan)
        operation_sha256 = _sha256(operation_id.encode("utf-8"))
        with self._write_set.workspace_lease():
            replay = self._read_import_receipt(operation_sha256)
            if replay is not None:
                stored_request, receipt = replay
                if stored_request != request_sha256:
                    raise PortableBookBundleConflict(
                        "operation_id was already used for another bundle import",
                        code="portable_bundle_operation_id_conflict",
                        details={"operation_sha256": operation_sha256},
                    )
                return replace(receipt, replayed=True)
            current_plan = self._plan_locked(plan.bundle, plan.pins)
            if current_plan.state_sha256 != plan.state_sha256:
                raise PortableBookBundleConflict(
                    "Desktop stores changed after the portable import was planned",
                    code="portable_bundle_plan_stale",
                    details={
                        "planned_state_sha256": plan.state_sha256,
                        "current_state_sha256": current_plan.state_sha256,
                    },
                )
            if not current_plan.committable:
                raise PortableBookBundleConflict(
                    "portable bundle import has unresolved conflicts",
                    code="portable_bundle_import_conflicts",
                    details={
                        "conflicts": [
                            {
                                **action.source.as_dict(),
                                "reasons": list(action.conflicts),
                            }
                            for action in current_plan.actions
                            if not action.committable
                        ]
                    },
                )
            receipt = self._commit_locked(
                current_plan,
                operation_id=operation_id,
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
            )
            return receipt

    def _export_record(
        self,
        key: ScanAssessmentKey,
        manual: Mapping[str, Any],
        builds: Mapping[str, Any],
        ch_rows: list[Any],
        ch_document: Mapping[str, Any],
    ) -> PortableBookRecord:
        self._supported_key(key)
        if key.namespace == "manual_entries":
            resolved = self._manual_authority_resolver(
                key.source_id,
                manual,
                builds,
                None,
            )
            source_metadata = copy.deepcopy(dict(resolved.source_row))
            metadata = copy.deepcopy(dict(resolved.active_row))
            if resolved.record_revision is None:
                raise PortableBookBundleError(
                    "selected manual authority does not exist",
                    code="portable_book_source_not_found",
                    details=key.as_dict(),
                )
            version = resolved.record_revision
            source_evidence = catalogue_source_evidence(
                key.namespace,
                source_metadata,
                captures_path=self._captures_path,
            )
            source_hash = catalogue_source_sha256(
                key.namespace,
                source_metadata,
                source_evidence=source_evidence,
            )
            authority = resolved.portable
            book_id = authority.canonical_item_id or key.source_reference
        else:
            index = self._ch_index(key, ch_rows)
            row = ch_rows[index]
            source_metadata = copy.deepcopy(dict(row))
            source_evidence = catalogue_source_evidence(key.namespace, row)
            source_hash = catalogue_source_sha256(
                key.namespace,
                row,
                source_evidence=source_evidence,
            )
            annotation = self._verified_annotation(
                key,
                source_hash,
                ch_document["annotations"].get(key.source_id),
            )
            metadata = copy.deepcopy(dict(row))
            if annotation is not None:
                metadata.update(copy.deepcopy(annotation["fields"]))
                version = annotation["revision"]
            else:
                version = _CH_BASE_VERSION_PREFIX + source_hash
            aliases = _canonical_aliases(row)
            if len(aliases) > 1:
                raise PortableBookBundleConflict(
                    "CH source has conflicting canonical aliases",
                    code="portable_authority_alias_conflict",
                    details=key.as_dict(),
                )
            authority = PortableBookAuthority(
                storage_kind="ch_library",
                storage_id=key.source_id,
                canonical_item_id=aliases[0] if aliases else "",
                capture_id="",
            )
            book_id = authority.canonical_item_id or key.source_reference
        assessment = self._assessments._read_unlocked(key)
        return PortableBookRecord(
            source=key,
            record_version=version,
            source_sha256=source_hash,
            source_evidence=source_evidence,
            source_metadata=source_metadata,
            authority=authority,
            metadata=metadata,
            assessment=assessment,
            book_id=book_id,
        )

    def _plan_locked(
        self,
        bundle: PortableBookBundle,
        pins: Mapping[ScanAssessmentKey, PortableImportPin],
    ) -> PortableBookImportPlan:
        manual = self._manual_document()
        builds = self._builds_document()
        ch_rows = self._ch_rows()
        ch_document = self._ch_annotations_document()
        actions: list[PortableImportAction] = []
        state_records: list[dict[str, Any]] = []
        for record in bundle.records:
            key = record.source
            self._supported_key(key)
            pin = pins[key]
            conflicts: list[str] = []
            if key.namespace == "manual_entries":
                desired = _detached_metadata(record)
                desired_source = _detached_source_metadata(record)
                try:
                    ManualEntryItemCodec.validate_record(key.source_id, desired_source)
                except (TypeError, ValueError) as exc:
                    raise PortableBookBundleError(
                        "portable manual source metadata failed validation",
                        code="invalid_portable_manual_entry",
                        details=key.as_dict(),
                    ) from exc
                if record.authority.storage_kind == "whl_builds":
                    _validated_build(record.authority.storage_id, desired)
                    if desired.get("capture_id") != record.authority.capture_id:
                        raise PortableBookBundleError(
                            "promoted build capture does not match its authority",
                            code="portable_authority_mismatch",
                            details=key.as_dict(),
                        )
                    desired_version = WhlCatalogueItemCodec.record_revision(
                        record.authority.storage_id, desired
                    )
                else:
                    if record.authority.storage_id != key.source_id:
                        raise PortableBookBundleError(
                            "manual authority storage id does not match its source",
                            code="portable_authority_mismatch",
                            details=key.as_dict(),
                        )
                    try:
                        ManualEntryItemCodec.validate_record(key.source_id, desired)
                    except (TypeError, ValueError) as exc:
                        raise PortableBookBundleError(
                            "portable manual authority failed validation",
                            code="invalid_portable_manual_entry",
                            details=key.as_dict(),
                        ) from exc
                    desired_version = ManualEntryItemCodec.record_revision(
                        key.source_id, desired
                    )
                    if desired != desired_source:
                        raise PortableBookBundleError(
                            "unpromoted authority must equal its manual source",
                            code="portable_authority_mismatch",
                            details=key.as_dict(),
                        )
                if desired_version != record.record_version:
                    raise PortableBookBundleError(
                        "authority record version does not match its metadata",
                        code="portable_record_version_mismatch",
                        details=key.as_dict(),
                    )
                desired_source_hash = catalogue_source_sha256(
                    key.namespace,
                    desired_source,
                    source_evidence=record.source_evidence,
                )
                if desired_source_hash != record.source_sha256:
                    raise PortableBookBundleError(
                        "manual-entry source hash does not match its metadata",
                        code="portable_source_hash_mismatch",
                        details=key.as_dict(),
                    )
                target_desired_source_hash = catalogue_source_sha256(
                    key.namespace,
                    desired_source,
                    captures_path=self._captures_path,
                )
                if target_desired_source_hash != record.source_sha256:
                    conflicts.append("source_hash_changed")
                current_raw = manual.get(key.source_id)
                current = current_raw if isinstance(current_raw, Mapping) else None
                current_source_version = (
                    ManualEntryItemCodec.record_revision(key.source_id, current)
                    if current is not None
                    else None
                )
                if current_raw is not None and current is None:
                    conflicts.append("invalid_current_manual_entry")
                current_source_hash = (
                    catalogue_source_sha256(
                        key.namespace,
                        current,
                        captures_path=self._captures_path,
                    )
                    if current is not None
                    else target_desired_source_hash
                    if current_raw is None
                    else None
                )
                if (
                    current_source_hash is not None
                    and current_source_hash != record.source_sha256
                ):
                    conflicts.append("source_hash_changed")
                expected_source_version = pin.source_record_version
                if (
                    expected_source_version is None
                    and record.authority.storage_kind == "manual_entries"
                ):
                    expected_source_version = pin.record_version
                if current_source_version != expected_source_version:
                    conflicts.append("source_record_version_changed")
                resolved = self._manual_authority_resolver(
                    key.source_id,
                    manual,
                    builds,
                    desired_source,
                )
                archived_aliases = tuple(
                    dict.fromkeys(
                        (
                            *_canonical_aliases(desired_source),
                            *_canonical_aliases(desired),
                        )
                    )
                )
                if len(archived_aliases) > 1 or (
                    archived_aliases
                    and archived_aliases[0] != record.authority.canonical_item_id
                ):
                    raise PortableBookBundleError(
                        "archived authority aliases are inconsistent",
                        code="portable_authority_alias_conflict",
                        details=key.as_dict(),
                    )
                if record.authority.capture_id != (
                    desired_source.get("capture_id")
                    if isinstance(desired_source.get("capture_id"), str)
                    else ""
                ):
                    raise PortableBookBundleError(
                        "archived authority capture does not match its source",
                        code="portable_authority_mismatch",
                        details=key.as_dict(),
                    )
                if current is not None and (
                    resolved.capture_id != record.authority.capture_id
                    or resolved.canonical_item_id != record.authority.canonical_item_id
                ):
                    conflicts.append("authority_alias_changed")
                if record.authority.storage_kind == "whl_builds":
                    if resolved.storage_kind == "whl_builds":
                        current_version = resolved.record_revision
                        current_active = resolved.active_row
                        if resolved.storage_id != record.authority.storage_id:
                            conflicts.append("authority_changed")
                    else:
                        current_version = None
                        current_active = None
                        existing_at_id = builds.get(record.authority.storage_id)
                        if existing_at_id is not None:
                            conflicts.append("authority_storage_id_conflict")
                else:
                    current_version = current_source_version
                    current_active = current
                    if resolved.storage_kind != "manual_entries":
                        conflicts.append("authority_changed")
                if current_version != pin.record_version:
                    conflicts.append("record_version_changed")
                metadata_action = (
                    "conflict"
                    if conflicts
                    else "create"
                    if current is None or current_active is None
                    else "unchanged"
                    if dict(current_active) == desired
                    and dict(current) == desired_source
                    else "update"
                )
            else:
                index = self._ch_index(key, ch_rows)
                row = ch_rows[index]
                current_source_version = None
                archived_source = _detached_source_metadata(record)
                archived_source_hash = catalogue_source_sha256(
                    key.namespace,
                    archived_source,
                    source_evidence=record.source_evidence,
                )
                if archived_source_hash != record.source_sha256:
                    raise PortableBookBundleError(
                        "CH source hash does not match its archived evidence",
                        code="portable_source_hash_mismatch",
                        details=key.as_dict(),
                    )
                live_source_hash = catalogue_source_sha256(key.namespace, row)
                current_source_hash = live_source_hash
                if live_source_hash != record.source_sha256:
                    conflicts.append("source_hash_changed")
                if record.authority != PortableBookAuthority(
                    storage_kind="ch_library",
                    storage_id=key.source_id,
                    canonical_item_id=record.authority.canonical_item_id,
                    capture_id="",
                ):
                    raise PortableBookBundleError(
                        "CH authority descriptor does not match its source",
                        code="portable_authority_mismatch",
                        details=key.as_dict(),
                    )
                archived_aliases = _canonical_aliases(archived_source)
                if len(archived_aliases) > 1 or (
                    (archived_aliases[0] if archived_aliases else "")
                    != record.authority.canonical_item_id
                ):
                    raise PortableBookBundleError(
                        "CH authority alias does not match its source",
                        code="portable_authority_alias_conflict",
                        details=key.as_dict(),
                    )
                expected_metadata = copy.deepcopy(archived_source)
                expected_metadata.update(dict(record.copy_curation))
                if expected_metadata != _detached_metadata(record):
                    conflicts.append("shipped_ch_metadata_changed_in_bundle")
                try:
                    current_annotation = self._verified_annotation(
                        key,
                        live_source_hash,
                        ch_document["annotations"].get(key.source_id),
                    )
                except PortableBookBundleConflict:
                    current_annotation = None
                    conflicts.append("invalid_current_ch_annotation")
                current_version = (
                    current_annotation["revision"]
                    if current_annotation is not None
                    else _CH_BASE_VERSION_PREFIX + live_source_hash
                )
                if current_version != pin.record_version:
                    conflicts.append("record_version_changed")
                current_fields = (
                    {
                        name: value
                        for name, value in current_annotation["fields"].items()
                        if value != ""
                    }
                    if current_annotation is not None
                    else {}
                )
                desired_fields = {
                    key_name: value
                    for key_name, value in record.copy_curation.items()
                    if value != ""
                }
                if record.record_version.startswith(_CH_BASE_VERSION_PREFIX):
                    if (
                        record.record_version
                        != _CH_BASE_VERSION_PREFIX + record.source_sha256
                        or desired_fields
                    ):
                        raise PortableBookBundleError(
                            "CH base record version does not match an unannotated source",
                            code="portable_record_version_mismatch",
                            details=key.as_dict(),
                        )
                elif not _CH_REVISION_RE.fullmatch(record.record_version):
                    raise PortableBookBundleError(
                        "CH annotation record version is invalid",
                        code="portable_record_version_mismatch",
                        details=key.as_dict(),
                    )
                metadata_action = (
                    "conflict"
                    if conflicts
                    else "unchanged"
                    if current_fields == desired_fields
                    else "create"
                    if current_annotation is None
                    else "update"
                )
            current_assessment = self._assessments._read_unlocked(key)
            current_assessment_revision = (
                current_assessment.revision if current_assessment is not None else None
            )
            if current_assessment_revision != pin.assessment_revision:
                conflicts.append("assessment_revision_changed")
            if conflicts:
                assessment_action = "conflict"
                metadata_action = "conflict"
            elif record.assessment is None:
                assessment_action = (
                    "delete" if current_assessment is not None else "unchanged"
                )
            elif current_assessment is None:
                assessment_action = "create"
            elif current_assessment.as_dict() == record.assessment.as_dict():
                assessment_action = "unchanged"
            else:
                assessment_action = "update"
            actions.append(
                PortableImportAction(
                    source=key,
                    metadata=metadata_action,
                    assessment=assessment_action,
                    current_record_version=current_version,
                    current_assessment_revision=current_assessment_revision,
                    result_record_version=(
                        None
                        if conflicts
                        else None
                        if key.namespace == "ch_library"
                        and metadata_action in {"create", "update"}
                        else record.record_version
                        if metadata_action in {"create", "update"}
                        else current_version
                    ),
                    result_assessment_revision=(
                        None
                        if conflicts
                        or record.assessment is None
                        or assessment_action in {"create", "update"}
                        else current_assessment_revision
                    ),
                    assessment_sha256=(
                        None
                        if conflicts or record.assessment is None
                        else record.assessment.manifest.content_sha256
                    ),
                    conflicts=tuple(dict.fromkeys(conflicts)),
                )
            )
            state_records.append(
                {
                    **key.as_dict(),
                    "source_sha256": current_source_hash,
                    "source_record_version": current_source_version,
                    "record_version": current_version,
                    "assessment_revision": current_assessment_revision,
                    "assessment_sha256": (
                        current_assessment.manifest.content_sha256
                        if current_assessment is not None
                        else None
                    ),
                }
            )
        state = {
            "manual_sha256": _sha256(portable_book_canonical_json(manual)),
            "builds_sha256": _sha256(portable_book_canonical_json(builds)),
            "ch_annotations_sha256": _sha256(portable_book_canonical_json(ch_document)),
            "records": state_records,
        }
        return PortableBookImportPlan(
            bundle=bundle,
            pins=pins,
            actions=tuple(actions),
            state_sha256=_sha256(portable_book_canonical_json(state)),
        )

    def _commit_locked(
        self,
        plan: PortableBookImportPlan,
        *,
        operation_id: str,
        operation_sha256: str,
        request_sha256: str,
    ) -> PortableBookImportReceipt:
        manual = self._manual_document()
        builds = self._builds_document()
        ch_document = self._ch_annotations_document()
        changed_manual = False
        changed_builds = False
        changed_ch = False
        created_at = _timestamp(self._clock)
        restored_assessments: dict[ScanAssessmentKey, ScanAssessmentView] = {}
        result_actions: list[PortableImportAction] = []
        for record, action in zip(plan.bundle.records, plan.actions, strict=True):
            key = record.source
            result_record_version = action.result_record_version
            if action.metadata in {"create", "update"}:
                if key.namespace == "manual_entries":
                    manual[key.source_id] = _detached_source_metadata(record)
                    changed_manual = True
                    if record.authority.storage_kind == "whl_builds":
                        builds[record.authority.storage_id] = _detached_metadata(record)
                        changed_builds = True
                else:
                    current_raw = ch_document["annotations"].get(key.source_id)
                    stored = (
                        copy.deepcopy(dict(current_raw))
                        if isinstance(current_raw, Mapping)
                        else {}
                    )
                    desired_fields = {
                        name: value
                        for name, value in record.copy_curation.items()
                        if value != ""
                    }
                    revision = "cha-" + _sha256(
                        portable_book_canonical_json(
                            {
                                "operation_sha256": operation_sha256,
                                "bundle_sha256": plan.bundle.archive_sha256,
                                "source": key.as_dict(),
                                "previous_revision": action.current_record_version,
                                "fields": desired_fields,
                                "updated_at": created_at,
                            }
                        )
                    )
                    stored.update(
                        {
                            "namespace": key.namespace,
                            "source_id": key.source_id,
                            "source_sha256": record.source_sha256,
                            "fields": desired_fields,
                            "revision": revision,
                            "created_at": str(stored.get("created_at") or created_at),
                            "updated_at": created_at,
                        }
                    )
                    ch_document["annotations"][key.source_id] = stored
                    changed_ch = True
                    result_record_version = revision
            if action.assessment in {"create", "update"}:
                assert record.assessment is not None
                current_assessment = self._assessments._read_unlocked(key)
                prior_timestamp = (
                    current_assessment.manifest.updated_at
                    if current_assessment is not None
                    else record.assessment.manifest.created_at
                )
                assessment_updated_at = _timestamp_after(created_at, prior_timestamp)
                assessment_revision = "sa-" + _sha256(
                    portable_book_canonical_json(
                        {
                            "operation_sha256": operation_sha256,
                            "bundle_sha256": plan.bundle.archive_sha256,
                            "source": key.as_dict(),
                            "previous_revision": action.current_assessment_revision,
                            "content_sha256": (
                                record.assessment.manifest.content_sha256
                            ),
                            "updated_at": assessment_updated_at,
                        }
                    )
                )
                restored_manifest = ScanAssessmentManifest(
                    key=key,
                    content_sha256=record.assessment.manifest.content_sha256,
                    byte_size=record.assessment.manifest.byte_size,
                    revision=assessment_revision,
                    created_at=(
                        current_assessment.manifest.created_at
                        if current_assessment is not None
                        else record.assessment.manifest.created_at
                    ),
                    updated_at=assessment_updated_at,
                    provenance=record.assessment.manifest.provenance,
                    canonical_item_id=record.assessment.manifest.canonical_item_id,
                    capture_id=record.assessment.manifest.capture_id,
                )
                restored_assessments[key] = ScanAssessmentView(
                    restored_manifest, record.assessment.text
                )
            result_actions.append(
                replace(
                    action,
                    result_record_version=result_record_version,
                    result_assessment_revision=(
                        restored_assessments[key].revision
                        if key in restored_assessments
                        else action.result_assessment_revision
                    ),
                )
            )
        receipt = PortableBookImportReceipt(
            operation_sha256=operation_sha256,
            bundle_sha256=plan.bundle.archive_sha256,
            state_sha256=plan.state_sha256,
            created_at=created_at,
            actions=tuple(result_actions),
        )
        transaction = self._write_set.begin(
            operation_id=operation_id,
            scope="portable_book_bundle_import",
            metadata={
                "bundle_sha256": plan.bundle.archive_sha256,
                "record_count": len(plan.actions),
            },
        )
        if changed_manual:
            transaction.stage_write(
                self._manual_relative,
                self._pretty_json(manual),
            )
        if changed_builds:
            transaction.stage_write(
                self._builds_relative,
                self._pretty_json(builds),
            )
        if changed_ch:
            transaction.stage_write(
                self._ch_annotations_relative,
                self._pretty_json(ch_document),
            )
        for record, action in zip(plan.bundle.records, plan.actions, strict=True):
            directory = self._scan_relative / scan_assessment_locator_digest(
                record.source
            )
            if action.assessment in {"create", "update"}:
                restored = restored_assessments[record.source]
                transaction.stage_write(
                    directory / SCAN_ASSESSMENT_TEXT_NAME,
                    restored.text.encode("utf-8", errors="strict"),
                )
                transaction.stage_write(
                    directory / SCAN_ASSESSMENT_MANIFEST_NAME,
                    canonical_scan_assessment_json(restored.manifest.as_dict()) + b"\n",
                )
            elif action.assessment == "delete":
                transaction.stage_delete(directory / SCAN_ASSESSMENT_TEXT_NAME)
                transaction.stage_delete(directory / SCAN_ASSESSMENT_MANIFEST_NAME)
        transaction.stage_write(
            self._receipt_relative(operation_sha256),
            self._receipt_bytes(receipt, request_sha256=request_sha256),
        )
        transaction.commit(
            receipt={
                "kind": "portable_book_bundle_import",
                "bundle_sha256": plan.bundle.archive_sha256,
                "record_count": len(plan.actions),
            }
        )
        return receipt

    def _require_bundle_and_pins(
        self,
        bundle: PortableBookBundle,
        pins: Mapping[ScanAssessmentKey, PortableImportPin],
    ) -> None:
        if not isinstance(bundle, PortableBookBundle):
            raise TypeError("bundle must be a PortableBookBundle")
        if not isinstance(pins, Mapping):
            raise TypeError("pins must be a mapping")
        expected = {record.source for record in bundle.records}
        if set(pins) != expected or any(
            not isinstance(value, PortableImportPin) for value in pins.values()
        ):
            raise PortableBookBundleError(
                "import pins must cover exactly every bundle source",
                code="invalid_portable_import_pins",
            )

    @staticmethod
    def _supported_key(key: ScanAssessmentKey) -> None:
        if not isinstance(key, ScanAssessmentKey):
            raise TypeError("source selection must contain ScanAssessmentKey values")
        if key.namespace not in {"manual_entries", "ch_library"}:
            raise PortableBookBundleError(
                "portable bundle contains an unsupported catalogue namespace",
                code="unsupported_portable_book_namespace",
                details=key.as_dict(),
            )

    @staticmethod
    def _ch_index(key: ScanAssessmentKey, rows: list[Any]) -> int:
        if not key.source_id.isdigit():
            raise PortableBookBundleError(
                "CH source id must be its exact zero-based index",
                code="invalid_portable_ch_source",
                details=key.as_dict(),
            )
        index = int(key.source_id)
        if (
            str(index) != key.source_id
            or index >= len(rows)
            or not isinstance(rows[index], Mapping)
        ):
            raise PortableBookBundleError(
                "CH source row does not exist",
                code="portable_book_source_not_found",
                details=key.as_dict(),
            )
        return index

    @staticmethod
    def _verified_annotation(
        key: ScanAssessmentKey,
        source_sha256: str,
        raw: Any,
    ) -> dict[str, Any] | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise PortableBookBundleConflict(
                "current CH annotation is not an object",
                code="invalid_current_ch_annotation",
                details=key.as_dict(),
            )
        fields = raw.get("fields")
        if (
            raw.get("namespace") != key.namespace
            or raw.get("source_id") != key.source_id
            or raw.get("source_sha256") != source_sha256
            or not isinstance(raw.get("revision"), str)
            or not _CH_REVISION_RE.fullmatch(raw["revision"])
            or not isinstance(fields, Mapping)
            or set(fields) - set(PORTABLE_BOOK_COPY_FIELDS)
        ):
            raise PortableBookBundleConflict(
                "current CH annotation identity or schema is invalid",
                code="invalid_current_ch_annotation",
                details=key.as_dict(),
            )
        validated = portable_copy_curation(fields)
        return {**copy.deepcopy(dict(raw)), "fields": validated}

    def _manual_document(self) -> dict[str, Any]:
        raw = self._read_mutable_json(self._manual_relative, missing={})
        if not isinstance(raw, dict) or any(
            not isinstance(name, str) or not isinstance(value, Mapping)
            for name, value in raw.items()
        ):
            raise RepositoryError(
                "manual_entries.json is not a valid record mapping",
                code="invalid_manual_entries_store",
            )
        return copy.deepcopy(raw)

    def _builds_document(self) -> dict[str, Any]:
        raw = self._read_mutable_json(self._builds_relative, missing={})
        if not isinstance(raw, dict):
            raise RepositoryError(
                "whl_builds.json is not a valid record mapping",
                code="invalid_portable_build_store",
            )
        for build_id, row in raw.items():
            _validated_build(build_id, row)
        return copy.deepcopy(raw)

    def _ch_annotations_document(self) -> dict[str, Any]:
        raw = self._read_mutable_json(
            self._ch_annotations_relative,
            missing={
                "schema": _CH_ANNOTATIONS_SCHEMA,
                "annotations": {},
                "operations": {},
            },
        )
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != _CH_ANNOTATIONS_SCHEMA
            or not isinstance(raw.get("annotations"), dict)
            or not isinstance(raw.get("operations", {}), dict)
        ):
            raise RepositoryError(
                "ch_annotations.json does not match its schema",
                code="invalid_ch_annotations_store",
            )
        raw.setdefault("operations", {})
        return copy.deepcopy(raw)

    def _ch_rows(self) -> list[Any]:
        raw = self._read_absolute_json(self._ch_library_path, label="ch_library.json")
        if not isinstance(raw, list):
            raise RepositoryError(
                "ch_library.json is not an array",
                code="invalid_ch_library_store",
            )
        return raw

    def _read_mutable_json(self, relative: PurePosixPath, *, missing: Any) -> Any:
        # Reuse the write authority's containment and redirect checks for the
        # read side as well.  This is intentionally a repository-private seam:
        # both objects are one filesystem adapter and share the same authority.
        path = self._write_set._target(relative)
        if not os.path.lexists(path):
            return copy.deepcopy(missing)
        return self._read_absolute_json(path, label=relative.as_posix())

    @staticmethod
    def _read_absolute_json(path: Path, *, label: str) -> Any:
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOINHERIT", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RepositoryError(
                f"{label} could not be opened safely",
                code="portable_bundle_store_read_failed",
                details={"store": label},
            ) from exc
        try:
            opened_before = os.fstat(descriptor)
            named_before = os.lstat(path)
            if (
                stat.S_ISLNK(named_before.st_mode)
                or bool(
                    int(getattr(named_before, "st_file_attributes", 0))
                    & _REPARSE_POINT_ATTRIBUTE
                )
                or not stat.S_ISREG(opened_before.st_mode)
                or not stat.S_ISREG(named_before.st_mode)
                or opened_before.st_nlink != 1
                or named_before.st_nlink != 1
                or not os.path.samestat(opened_before, named_before)
                or opened_before.st_size > _MAX_CATALOG_DOCUMENT_BYTES
            ):
                raise OSError("not one bounded private regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(_MAX_CATALOG_DOCUMENT_BYTES + 1)
            opened_after = os.fstat(descriptor)
            named_after = os.lstat(path)
            if (
                len(payload) > _MAX_CATALOG_DOCUMENT_BYTES
                or not os.path.samestat(opened_before, opened_after)
                or not os.path.samestat(opened_after, named_after)
                or len(payload) != opened_after.st_size
            ):
                raise OSError("file changed during read")
        except OSError as exc:
            raise RepositoryError(
                f"{label} could not be read safely",
                code="portable_bundle_store_read_failed",
                details={"store": label},
            ) from exc
        finally:
            os.close(descriptor)
        return _strict_json(payload, artifact=label)

    @staticmethod
    def _pretty_json(value: Any) -> bytes:
        try:
            return (
                json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8", errors="strict")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RepositoryError(
                "a Desktop catalogue document could not be encoded safely",
                code="portable_bundle_store_write_failed",
            ) from exc

    def _receipt_relative(self, operation_sha256: str) -> PurePosixPath:
        return self._receipts_relative / f"{operation_sha256}.json"

    @staticmethod
    def _request_sha256(plan: PortableBookImportPlan) -> str:
        pins = [
            {
                **key.as_dict(),
                "record_version": pin.record_version,
                "assessment_revision": pin.assessment_revision,
            }
            for key, pin in sorted(
                plan.pins.items(), key=lambda item: item[0].source_reference
            )
        ]
        return _sha256(
            portable_book_canonical_json(
                {
                    "bundle_sha256": plan.bundle.archive_sha256,
                    "state_sha256": plan.state_sha256,
                    "pins": pins,
                }
            )
        )

    @staticmethod
    def _receipt_bytes(
        receipt: PortableBookImportReceipt,
        *,
        request_sha256: str,
    ) -> bytes:
        value = {
            "schema": _IMPORT_RECEIPT_SCHEMA,
            "request_sha256": request_sha256,
            "operation_sha256": receipt.operation_sha256,
            "bundle_sha256": receipt.bundle_sha256,
            "state_sha256": receipt.state_sha256,
            "created_at": receipt.created_at,
            "actions": [
                {
                    **action.source.as_dict(),
                    "metadata": action.metadata,
                    "assessment": action.assessment,
                    "current_record_version": action.current_record_version,
                    "current_assessment_revision": action.current_assessment_revision,
                    "result_record_version": action.result_record_version,
                    "result_assessment_revision": action.result_assessment_revision,
                    "assessment_sha256": action.assessment_sha256,
                    "conflicts": list(action.conflicts),
                }
                for action in receipt.actions
            ],
        }
        payload = portable_book_canonical_json(value) + b"\n"
        if len(payload) > _MAX_IMPORT_RECEIPT_BYTES:
            raise RepositoryError(
                "portable bundle import receipt exceeds its byte limit",
                code="portable_bundle_receipt_too_large",
            )
        return payload

    def _read_import_receipt(
        self, operation_sha256: str
    ) -> tuple[str, PortableBookImportReceipt] | None:
        relative = self._receipt_relative(operation_sha256)
        path = self._write_set.root.joinpath(*relative.parts)
        if not os.path.lexists(path):
            return None
        raw = self._read_absolute_json(path, label=relative.as_posix())
        try:
            if (
                not isinstance(raw, Mapping)
                or frozenset(raw) != _RECEIPT_FIELDS
                or raw.get("schema") != _IMPORT_RECEIPT_SCHEMA
                or raw.get("operation_sha256") != operation_sha256
                or not isinstance(raw.get("actions"), list)
                or any(
                    not isinstance(item, Mapping)
                    or frozenset(item) != _RECEIPT_ACTION_FIELDS
                    for item in raw.get("actions", [])
                )
            ):
                raise ValueError("receipt schema")
            actions = tuple(
                PortableImportAction(
                    source=ScanAssessmentKey(item["namespace"], item["source_id"]),
                    metadata=item["metadata"],
                    assessment=item["assessment"],
                    current_record_version=item["current_record_version"],
                    current_assessment_revision=item["current_assessment_revision"],
                    result_record_version=item["result_record_version"],
                    result_assessment_revision=item["result_assessment_revision"],
                    assessment_sha256=item["assessment_sha256"],
                    conflicts=tuple(item["conflicts"]),
                )
                for item in raw["actions"]
            )
            if len({action.source for action in actions}) != len(actions):
                raise ValueError("duplicate receipt source")
            receipt = PortableBookImportReceipt(
                operation_sha256=raw["operation_sha256"],
                bundle_sha256=raw["bundle_sha256"],
                state_sha256=raw["state_sha256"],
                created_at=raw["created_at"],
                actions=actions,
            )
            request_sha256 = _validated_digest(
                raw["request_sha256"], field="request_sha256"
            )
        except (KeyError, TypeError, ValueError, PortableBookBundleError) as exc:
            raise RepositoryError(
                "portable bundle import receipt failed integrity validation",
                code="invalid_portable_bundle_receipt",
                details={"operation_sha256": operation_sha256},
            ) from exc
        return request_sha256, receipt


__all__ = [
    "FilesystemPortableBookBundleService",
    "PortableBookBundleZipCodec",
    "ResolvedManualBookAuthority",
    "catalogue_source_evidence",
    "catalogue_source_sha256",
    "resolve_manual_book_authority",
]
