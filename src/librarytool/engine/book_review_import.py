"""Validate and plan selective imports from the curated book-review export.

This module is deliberately a read-only bridge.  It validates the complete
review bundle, binds every selected row to the *current* legacy-source
projection, and returns a field-level dry-run plan.  It does not know how to
open or mutate Desktop storage.  A later composition layer may implement
``AtomicBookReviewImportPort``; that port is intentionally one atomic call per
holding so short metadata and its Markdown artifact cannot diverge.

Two record layouts are supported under the same versioned envelope:

``canonical``
    ``record_id`` plus review fields at the record's top level.

``catalog-custom``
    ``id`` plus review fields in the record's ``custom`` object, matching the
    catalog-enrichment API projection.

Both layouts require ``source_links`` at the record top level.  Schema-less
catalog exports are not accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from librarytool.catalog_enrichment.importers import (
    LegacySourceError,
    SourceRecord,
    iter_ch_records,
    iter_manual_records,
    resolve_source_paths,
)


REVIEWED_EXPORT_SCHEMA = "org.worldherblibrary.book-review-reviewed-export.v1"
BOOK_REVIEW_SCHEMA = "org.worldherblibrary.book-review.v1"
DESTINATION_SNAPSHOT_SCHEMA = "librarytool.book-review-destination-snapshot/v1"
BOOK_REVIEW_COMMIT_CONFIRMATION = "COMMIT-SELECTED-BOOK-REVIEWS"

SUPPORTED_NAMESPACES = frozenset({"manual_entries", "ch_library"})
SCAN_PRIORITIES = ("n/s (no scan)", "Low", "Medium", "High")
DESKTOP_REVIEW_FIELDS = ("marked_price", "scan_priority", "scan_verdict")

MAX_SCAN_VERDICT_CHARACTERS = 500
MAX_MARKED_PRICE_CHARACTERS = 500
MAX_REASONING_BYTES = 1024 * 1024
MAX_REVIEWED_EXPORT_BYTES = 64 * 1024 * 1024
MAX_REVIEWED_RECORDS = 100_000

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PORTABLE_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$", re.ASCII)
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


class BookReviewImportError(RuntimeError):
    """A reviewed export or dry-run precondition failed closed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "book_review_import_error",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _error(
    message: str,
    *,
    code: str,
    field: str | None = None,
    **details: Any,
) -> BookReviewImportError:
    if field is not None:
        details["field"] = field
    return BookReviewImportError(message, code=code, details=details)


def _has_unsafe_unicode(value: str) -> bool:
    return any(
        character == "\0" or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


def _source_id(namespace: Any, source_id: Any) -> tuple[str, str]:
    if not isinstance(namespace, str) or namespace not in SUPPORTED_NAMESPACES:
        raise _error(
            "source namespace is unsupported",
            code="unsupported_source_namespace",
            field="namespace",
            namespace=namespace,
        )
    if not isinstance(source_id, str) or not _PORTABLE_SOURCE_ID_RE.fullmatch(
        source_id
    ):
        raise _error(
            "source_id must be a portable, non-empty string",
            code="invalid_source_reference",
            field="source_id",
        )
    if namespace == "ch_library" and (
        not source_id.isascii()
        or not source_id.isdigit()
        or (len(source_id) > 1 and source_id.startswith("0"))
    ):
        raise _error(
            "ch_library source_id must be a canonical zero-based array index",
            code="invalid_source_reference",
            field="source_id",
        )
    return namespace, source_id


@dataclass(frozen=True, slots=True, order=True)
class ReviewSourceRef:
    namespace: str
    source_id: str

    def __post_init__(self) -> None:
        namespace, source_id = _source_id(self.namespace, self.source_id)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "source_id", source_id)

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.source_id}"

    def as_dict(self) -> dict[str, str]:
        return {"namespace": self.namespace, "source_id": self.source_id}

    @classmethod
    def parse(cls, value: str) -> "ReviewSourceRef":
        if not isinstance(value, str) or ":" not in value:
            raise _error(
                "source selection must use namespace:source_id",
                code="invalid_source_reference",
                field="source_ref",
            )
        namespace, source_id = value.split(":", 1)
        return cls(namespace, source_id)


def _canonical_uuid(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise _error(
            f"{field} must be a UUID string",
            code="invalid_review_record_id",
            field=field,
        )
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise _error(
            f"{field} must be a UUID string",
            code="invalid_review_record_id",
            field=field,
        ) from exc


@dataclass(frozen=True, slots=True)
class ReviewSelection:
    """A non-empty, explicit set of review UUID and/or source-ref selectors."""

    review_record_ids: frozenset[str]
    source_refs: frozenset[ReviewSourceRef]

    def __post_init__(self) -> None:
        if not isinstance(self.review_record_ids, frozenset) or not isinstance(
            self.source_refs, frozenset
        ):
            raise TypeError("selection members must be frozensets")
        if not self.review_record_ids and not self.source_refs:
            raise _error(
                "at least one review UUID or source reference must be selected",
                code="explicit_selection_required",
            )
        for value in self.review_record_ids:
            if _canonical_uuid(value, field="selected_review_record_id") != value:
                raise _error(
                    "selected review UUID must use canonical form",
                    code="invalid_review_record_id",
                    field="selected_review_record_id",
                )
        if any(not isinstance(value, ReviewSourceRef) for value in self.source_refs):
            raise TypeError("source_refs must contain ReviewSourceRef values")

    @classmethod
    def explicit(
        cls,
        *,
        review_record_ids: Iterable[str] = (),
        source_refs: Iterable[str | ReviewSourceRef] = (),
    ) -> "ReviewSelection":
        raw_ids = list(review_record_ids)
        normalized_ids = [
            _canonical_uuid(value, field="selected_review_record_id")
            for value in raw_ids
        ]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise _error(
                "review UUID selection contains a duplicate",
                code="duplicate_selection",
            )

        raw_refs = list(source_refs)
        normalized_refs = [
            value
            if isinstance(value, ReviewSourceRef)
            else ReviewSourceRef.parse(value)
            for value in raw_refs
        ]
        if len(set(normalized_refs)) != len(normalized_refs):
            raise _error(
                "source-reference selection contains a duplicate",
                code="duplicate_selection",
            )
        if not normalized_ids and not normalized_refs:
            raise _error(
                "at least one review UUID or source reference must be selected",
                code="explicit_selection_required",
            )
        return cls(frozenset(normalized_ids), frozenset(normalized_refs))


@dataclass(frozen=True, slots=True)
class ExportSourceLink:
    ref: ReviewSourceRef
    source_hash: str


@dataclass(frozen=True, slots=True)
class ValidatedReasoning:
    member: str
    sha256: str
    byte_size: int
    text: str


@dataclass(frozen=True, slots=True)
class ReviewedExportRecord:
    record_id: str
    layout: str
    source_links: tuple[ExportSourceLink, ...]
    metadata: tuple[tuple[str, str], ...]
    reasoning: ValidatedReasoning

    @property
    def metadata_dict(self) -> dict[str, str]:
        return dict(self.metadata)


@dataclass(frozen=True, slots=True)
class ReviewedExportBundle:
    path: Path
    review_root: Path
    sha256: str
    records: tuple[ReviewedExportRecord, ...]


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error(
                "JSON object contains a duplicate key",
                code="duplicate_json_key",
                field=key,
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise _error(
        "reviewed export contains a non-finite number",
        code="invalid_reviewed_export_json",
        value=value,
    )


def _stable_read(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        before = path.stat()
        if before.st_size > maximum:
            raise _error(
                f"{label} exceeds its byte limit",
                code="review_input_too_large",
                maximum=maximum,
                actual=before.st_size,
            )
        payload = path.read_bytes()
        after = path.stat()
    except BookReviewImportError:
        raise
    except OSError as exc:
        raise _error(
            f"{label} is unavailable",
            code="review_input_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    signature_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    signature_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if signature_before != signature_after or len(payload) != after.st_size:
        raise _error(
            f"{label} changed while it was read",
            code="review_input_changed",
        )
    return payload


def _load_strict_json(path: Path, *, maximum: int, label: str) -> tuple[Any, bytes]:
    payload = _stable_read(path, maximum=maximum, label=label)
    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_json_constant,
        )
    except BookReviewImportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(
            f"{label} is not strict UTF-8 JSON",
            code="invalid_reviewed_export_json",
            cause_type=type(exc).__name__,
        ) from exc
    return value, payload


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _error(
            f"{field} must be a lower-case SHA-256 digest",
            code="invalid_sha256",
            field=field,
        )
    return value


def _portable_reasoning_member(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise _error(
            "reasoning member must be a non-empty relative POSIX path",
            code="invalid_reasoning_member",
            field="review_analysis_member",
        )
    if (
        "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or _has_unsafe_unicode(value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _error(
            "reasoning member is not a relative POSIX path",
            code="invalid_reasoning_member",
            field="review_analysis_member",
        )
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise _error(
            "reasoning member contains an empty or traversal segment",
            code="invalid_reasoning_member",
            field="review_analysis_member",
        )
    if PurePosixPath(value).suffix.casefold() != ".md":
        raise _error(
            "reasoning member must identify a Markdown file",
            code="invalid_reasoning_member",
            field="review_analysis_member",
        )
    return value


def _read_reasoning(
    review_root: Path,
    member: Any,
    expected_sha256: Any,
) -> ValidatedReasoning:
    portable = _portable_reasoning_member(member)
    digest = _sha256(expected_sha256, field="review_analysis_sha256")
    root = review_root.expanduser().resolve(strict=True)
    candidate = root.joinpath(*portable.split("/"))
    try:
        resolved_before = candidate.resolve(strict=True)
        resolved_before.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _error(
            "reasoning member does not resolve beneath the review root",
            code="reasoning_member_escape",
            member=portable,
        ) from exc
    if not resolved_before.is_file():
        raise _error(
            "reasoning member is not a regular file",
            code="reasoning_member_unavailable",
            member=portable,
        )
    payload = _stable_read(
        resolved_before,
        maximum=MAX_REASONING_BYTES,
        label="reasoning member",
    )
    try:
        resolved_after = candidate.resolve(strict=True)
    except OSError as exc:
        raise _error(
            "reasoning member changed while it was read",
            code="review_input_changed",
            member=portable,
        ) from exc
    if resolved_after != resolved_before:
        raise _error(
            "reasoning member changed while it was read",
            code="review_input_changed",
            member=portable,
        )
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise _error(
            "reasoning member SHA-256 does not match the export",
            code="reasoning_hash_mismatch",
            member=portable,
            expected=digest,
            actual=actual,
        )
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error(
            "reasoning member is not valid UTF-8",
            code="invalid_reasoning_utf8",
            member=portable,
        ) from exc
    if _has_unsafe_unicode(text):
        raise _error(
            "reasoning member contains an unsafe Unicode value",
            code="invalid_reasoning_utf8",
            member=portable,
        )
    return ValidatedReasoning(portable, digest, len(payload), text)


def _priority(value: Any) -> str:
    if not isinstance(value, str) or value not in SCAN_PRIORITIES:
        raise _error(
            "scan_priority must be one of the four exact review values",
            code="invalid_scan_priority",
            field="scan_priority",
            allowed=list(SCAN_PRIORITIES),
        )
    return value


def _verdict(value: Any) -> str:
    if not isinstance(value, str):
        raise _error(
            "scan_verdict must be a string",
            code="invalid_scan_verdict",
            field="scan_verdict",
        )
    result = value.strip()
    if (
        not result
        or len(result) > MAX_SCAN_VERDICT_CHARACTERS
        or any(character in _LINE_BREAKS for character in result)
        or _has_unsafe_unicode(result)
    ):
        raise _error(
            "scan_verdict must be a non-empty, bounded single line",
            code="invalid_scan_verdict",
            field="scan_verdict",
            maximum=MAX_SCAN_VERDICT_CHARACTERS,
        )
    return result


def _marked_price(value: Any) -> str:
    if not isinstance(value, str):
        raise _error(
            "marked_price must be a string when present",
            code="invalid_marked_price",
            field="marked_price",
        )
    result = value.strip()
    if (
        not result
        or len(result) > MAX_MARKED_PRICE_CHARACTERS
        or any(character in _LINE_BREAKS for character in result)
        or _has_unsafe_unicode(result)
    ):
        raise _error(
            "marked_price must be a non-empty, bounded single line when present",
            code="invalid_marked_price",
            field="marked_price",
            maximum=MAX_MARKED_PRICE_CHARACTERS,
        )
    return result


def _record_container(raw: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    has_record_id = "record_id" in raw
    has_catalog_id = "id" in raw
    if has_record_id == has_catalog_id:
        raise _error(
            "review row must use exactly one supported record layout",
            code="unsupported_review_record_layout",
        )
    if has_record_id:
        return (
            "canonical",
            _canonical_uuid(raw["record_id"], field="record_id"),
            raw,
        )
    custom = raw.get("custom")
    if not isinstance(custom, Mapping):
        raise _error(
            "catalog-custom review row requires a custom object",
            code="unsupported_review_record_layout",
            field="custom",
        )
    return (
        "catalog-custom",
        _canonical_uuid(raw["id"], field="id"),
        custom,
    )


def _source_links(value: Any, *, review_record_id: str) -> tuple[ExportSourceLink, ...]:
    if not isinstance(value, list) or not value:
        raise _error(
            "source_links must be a non-empty array",
            code="invalid_source_links",
            field="source_links",
        )
    links: list[ExportSourceLink] = []
    seen: set[ReviewSourceRef] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise _error(
                "source link must be an object",
                code="invalid_source_links",
                field=f"source_links[{index}]",
            )
        try:
            ref = ReviewSourceRef(raw.get("namespace"), raw.get("source_id"))
            source_hash = _sha256(
                raw.get("source_hash"), field=f"source_links[{index}].source_hash"
            )
        except BookReviewImportError as exc:
            exc.details.setdefault("source_link_index", index)
            raise
        linked_record_id = raw.get("record_id")
        if linked_record_id is not None and (
            _canonical_uuid(
                linked_record_id,
                field=f"source_links[{index}].record_id",
            )
            != review_record_id
        ):
            raise _error(
                "source link record_id does not match its review row",
                code="invalid_source_links",
                field=f"source_links[{index}].record_id",
            )
        if ref in seen:
            raise _error(
                "source reference occurs more than once in one review row",
                code="duplicate_source_reference",
                source_ref=ref.key,
            )
        seen.add(ref)
        links.append(ExportSourceLink(ref, source_hash))
    return tuple(links)


def _validate_book_review_manifest(
    value: Any,
    *,
    record_id: str,
    priority: str,
    verdict: str,
    source_refs: frozenset[ReviewSourceRef],
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise _error(
            "book_review must be an object when present",
            code="invalid_book_review_manifest",
            field="book_review",
        )
    if value.get("schema") != BOOK_REVIEW_SCHEMA or value.get("state") != "completed":
        raise _error(
            "book_review manifest schema/state is unsupported",
            code="invalid_book_review_manifest",
            field="book_review",
        )
    if (
        _canonical_uuid(value.get("record_id"), field="book_review.record_id")
        != record_id
    ):
        raise _error(
            "book_review record_id does not match its export row",
            code="invalid_book_review_manifest",
            field="book_review.record_id",
        )
    source_key = value.get("source_key")
    if source_key is not None and ReviewSourceRef.parse(source_key) not in source_refs:
        raise _error(
            "book_review source_key does not match a preserved source link",
            code="invalid_book_review_manifest",
            field="book_review.source_key",
        )
    decision = value.get("decision")
    if not isinstance(decision, Mapping):
        raise _error(
            "book_review decision is missing",
            code="invalid_book_review_manifest",
            field="book_review.decision",
        )
    if decision.get("priority") != priority or decision.get("verdict") != verdict:
        raise _error(
            "book_review decision does not match the Desktop-facing fields",
            code="book_review_decision_mismatch",
            field="book_review.decision",
        )


def load_reviewed_export(
    export_path: str | Path,
    review_root: str | Path,
) -> ReviewedExportBundle:
    """Load and fully validate one curated reviewed-export bundle.

    Validation is intentionally independent of selection: an invalid
    unselected row still rejects the bundle before any commit could begin.
    """

    try:
        path = Path(export_path).expanduser().resolve(strict=True)
        root = Path(review_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise _error(
            "reviewed export or review root is unavailable",
            code="review_input_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    if not root.is_dir():
        raise _error(
            "review root must be a directory",
            code="review_input_unavailable",
        )
    value, payload = _load_strict_json(
        path,
        maximum=MAX_REVIEWED_EXPORT_BYTES,
        label="reviewed export",
    )
    if not isinstance(value, Mapping):
        raise _error(
            "reviewed export must be an object",
            code="invalid_reviewed_export",
        )
    if value.get("schema") != REVIEWED_EXPORT_SCHEMA:
        raise _error(
            "reviewed export schema is unsupported",
            code="unsupported_reviewed_export_schema",
            field="schema",
            supported=[REVIEWED_EXPORT_SCHEMA],
        )
    if value.get("merge_performed") is not False:
        raise _error(
            "reviewed export must state merge_performed: false",
            code="reviewed_export_already_merged",
            field="merge_performed",
        )
    raw_records = value.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > MAX_REVIEWED_RECORDS:
        raise _error(
            "records must be a bounded array",
            code="invalid_reviewed_export",
            field="records",
            maximum=MAX_REVIEWED_RECORDS,
        )

    records: list[ReviewedExportRecord] = []
    record_ids: set[str] = set()
    all_refs: dict[ReviewSourceRef, str] = {}
    members: dict[str, str] = {}
    folded_members: dict[str, str] = {}
    reasoning_cache: dict[tuple[str, str], ValidatedReasoning] = {}
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, Mapping):
            raise _error(
                "review row must be an object",
                code="invalid_review_record",
                field=f"records[{index}]",
            )
        layout, record_id, container = _record_container(raw)
        if record_id in record_ids:
            raise _error(
                "reviewed export contains a duplicate record UUID",
                code="duplicate_review_record_id",
                record_id=record_id,
            )
        record_ids.add(record_id)
        links = _source_links(raw.get("source_links"), review_record_id=record_id)
        for link in links:
            prior = all_refs.get(link.ref)
            if prior is not None:
                raise _error(
                    "reviewed export contains a duplicate source reference",
                    code="duplicate_source_reference",
                    source_ref=link.ref.key,
                    record_ids=[prior, record_id],
                )
            all_refs[link.ref] = record_id

        state = container.get("book_review_state")
        if state is not None and state != "completed":
            raise _error(
                "only completed review rows can be imported",
                code="incomplete_review_record",
                record_id=record_id,
            )
        priority = _priority(container.get("scan_priority"))
        verdict = _verdict(container.get("scan_verdict"))
        metadata: dict[str, str] = {
            "scan_priority": priority,
            "scan_verdict": verdict,
        }
        if "marked_price" in container:
            metadata["marked_price"] = _marked_price(container["marked_price"])
        _validate_book_review_manifest(
            container.get("book_review"),
            record_id=record_id,
            priority=priority,
            verdict=verdict,
            source_refs=frozenset(link.ref for link in links),
        )
        member = _portable_reasoning_member(container.get("review_analysis_member"))
        member_sha = _sha256(
            container.get("review_analysis_sha256"),
            field="review_analysis_sha256",
        )
        prior_member = members.get(member)
        if prior_member is not None and prior_member != record_id:
            raise _error(
                "two review records use the same reasoning member",
                code="duplicate_reasoning_member",
                member=member,
            )
        folded = member.casefold()
        prior_folded = folded_members.get(folded)
        if prior_folded is not None and prior_folded != member:
            raise _error(
                "reasoning member names collide case-insensitively",
                code="duplicate_reasoning_member",
                members=[prior_folded, member],
            )
        members[member] = record_id
        folded_members[folded] = member
        cache_key = (member, member_sha)
        reasoning = reasoning_cache.get(cache_key)
        if reasoning is None:
            reasoning = _read_reasoning(root, member, member_sha)
            reasoning_cache[cache_key] = reasoning
        records.append(
            ReviewedExportRecord(
                record_id=record_id,
                layout=layout,
                source_links=links,
                metadata=tuple(
                    (field, metadata[field])
                    for field in DESKTOP_REVIEW_FIELDS
                    if field in metadata
                ),
                reasoning=reasoning,
            )
        )
    return ReviewedExportBundle(
        path=path,
        review_root=root,
        sha256=hashlib.sha256(payload).hexdigest(),
        records=tuple(records),
    )


def catalog_source_record_sha256(source: SourceRecord) -> str:
    """Hash the existing catalog-enrichment ``SourceRecord.data`` projection.

    This is byte-for-byte the canonical JSON/hash operation used by
    ``CatalogStore.import_source_records``.  The projection itself remains
    owned by ``catalog_enrichment.importers`` and therefore includes its
    current projection version and capture-OCR evidence dependency.
    """

    if not isinstance(source, SourceRecord):
        raise TypeError("source must be a SourceRecord")
    serialized = json.dumps(
        dict(source.data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CurrentSourceRecord:
    ref: ReviewSourceRef
    source_hash: str


class CurrentSourceIndex:
    """Exact current source identities, built from existing SourceRecord ports."""

    def __init__(self, records: Iterable[CurrentSourceRecord]) -> None:
        values: dict[ReviewSourceRef, CurrentSourceRecord] = {}
        for record in records:
            if not isinstance(record, CurrentSourceRecord):
                raise TypeError("records must contain CurrentSourceRecord values")
            _sha256(record.source_hash, field="source_hash")
            if record.ref in values:
                raise _error(
                    "current source index contains a duplicate source reference",
                    code="duplicate_current_source_reference",
                    source_ref=record.ref.key,
                )
            values[record.ref] = record
        self._records = values

    @classmethod
    def from_source_records(
        cls, records: Iterable[SourceRecord]
    ) -> "CurrentSourceIndex":
        return cls(
            CurrentSourceRecord(
                ref=ReviewSourceRef(source.namespace, source.source_id),
                source_hash=catalog_source_record_sha256(source),
            )
            for source in records
        )

    @classmethod
    def from_paths(
        cls,
        manual_entries_path: str | Path,
        ch_library_path: str | Path,
        captures_dir: str | Path | None = None,
    ) -> "CurrentSourceIndex":
        """Build an exact index from independently located legacy stores.

        Desktop packages keep mutable manual entries under ``WHL_DATA_ROOT``
        while the immutable CH catalogue may live with packaged application
        data.  Requiring a synthetic common root would either copy that
        catalogue or hash the wrong file, so the two authorities are accepted
        explicitly and opened read-only.
        """

        try:
            manual_path = Path(manual_entries_path).expanduser().resolve(strict=True)
            ch_path = Path(ch_library_path).expanduser().resolve(strict=True)
            capture_path = (
                Path(captures_dir).expanduser().resolve(strict=True)
                if captures_dir is not None
                else None
            )
            manual = iter_manual_records(
                manual_path,
                captures_dir=capture_path,
            )
            checked = iter_ch_records(ch_path)
            return cls.from_source_records((*manual, *checked))
        except (LegacySourceError, OSError) as exc:
            raise _error(
                "current Library Tool source projection is unavailable",
                code="current_source_unavailable",
                cause_type=type(exc).__name__,
            ) from exc

    @classmethod
    def from_library_tool(cls, source_root: str | Path) -> "CurrentSourceIndex":
        """Read both legacy sources without writing them or an enrichment DB."""

        try:
            paths = resolve_source_paths(source_root)
            return cls.from_paths(
                paths.manual_entries,
                paths.ch_library,
                paths.captures_dir,
            )
        except LegacySourceError as exc:
            raise _error(
                "current Library Tool source projection is unavailable",
                code="current_source_unavailable",
                cause_type=type(exc).__name__,
            ) from exc

    def get(self, ref: ReviewSourceRef) -> CurrentSourceRecord | None:
        return self._records.get(ref)


@dataclass(frozen=True, slots=True)
class DestinationReviewState:
    """Read-only target state returned by a Desktop composition adapter."""

    metadata: tuple[tuple[str, str], ...]
    record_revision: str
    assessment_sha256: str | None = None
    assessment_revision: str | None = None
    import_receipt: "ReviewImportReceiptBinding | None" = None
    authority_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, tuple):
            raise TypeError("metadata must be a tuple of field/value pairs")
        raw: dict[str, str] = {}
        for pair in self.metadata:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("metadata must contain field/value pairs")
            field, value = pair
            if field in raw:
                raise _error(
                    "destination state contains a duplicate review field",
                    code="invalid_destination_state",
                    field=field,
                )
            if field not in DESKTOP_REVIEW_FIELDS or not isinstance(value, str):
                raise _error(
                    "destination state contains invalid review metadata",
                    code="invalid_destination_state",
                    field=str(field),
                )
            raw[field] = value
        if (
            not isinstance(self.record_revision, str)
            or not self.record_revision.strip()
        ):
            raise _error(
                "destination record revision must be non-empty",
                code="invalid_destination_state",
                field="record_revision",
            )
        if self.assessment_sha256 is not None:
            _sha256(self.assessment_sha256, field="assessment_sha256")
        if self.assessment_revision is not None and (
            not isinstance(self.assessment_revision, str)
            or not self.assessment_revision.strip()
        ):
            raise _error(
                "assessment revision must be non-empty when present",
                code="invalid_destination_state",
                field="assessment_revision",
            )
        if self.import_receipt is not None and not isinstance(
            self.import_receipt, ReviewImportReceiptBinding
        ):
            raise TypeError("import_receipt must be ReviewImportReceiptBinding")
        if self.authority_sha256 is not None:
            _sha256(self.authority_sha256, field="authority_sha256")

    @classmethod
    def create(
        cls,
        *,
        metadata: Mapping[str, str] | None = None,
        record_revision: str,
        assessment_sha256: str | None = None,
        assessment_revision: str | None = None,
        import_receipt: "ReviewImportReceiptBinding | None" = None,
        authority_sha256: str | None = None,
    ) -> "DestinationReviewState":
        if not isinstance(record_revision, str) or not record_revision.strip():
            raise _error(
                "destination record revision must be non-empty",
                code="invalid_destination_state",
                field="record_revision",
            )
        raw = dict(metadata or {})
        unsupported = set(raw) - set(DESKTOP_REVIEW_FIELDS)
        if unsupported:
            raise _error(
                "destination state contains unsupported review fields",
                code="invalid_destination_state",
                fields=sorted(str(field) for field in unsupported),
            )
        for field, value in raw.items():
            if not isinstance(value, str):
                raise _error(
                    "destination review metadata values must be strings",
                    code="invalid_destination_state",
                    field=field,
                )
        digest = (
            _sha256(assessment_sha256, field="assessment_sha256")
            if assessment_sha256 is not None
            else None
        )
        if assessment_revision is not None and (
            not isinstance(assessment_revision, str) or not assessment_revision.strip()
        ):
            raise _error(
                "assessment revision must be non-empty when present",
                code="invalid_destination_state",
                field="assessment_revision",
            )
        return cls(
            metadata=tuple(
                (field, raw[field]) for field in DESKTOP_REVIEW_FIELDS if field in raw
            ),
            record_revision=record_revision,
            assessment_sha256=digest,
            assessment_revision=assessment_revision,
            import_receipt=import_receipt,
            authority_sha256=(
                _sha256(authority_sha256, field="authority_sha256")
                if authority_sha256 is not None
                else None
            ),
        )

    @property
    def metadata_dict(self) -> dict[str, str]:
        return dict(self.metadata)


@dataclass(frozen=True, slots=True)
class ReviewImportReceiptBinding:
    """Minimal receipt pins needed to prove an idempotent replay is unchanged."""

    export_sha256: str
    source_hash_before_import: str
    source_hash_after_import: str

    def __post_init__(self) -> None:
        _sha256(self.export_sha256, field="receipt.export_sha256")
        _sha256(
            self.source_hash_before_import,
            field="receipt.source_hash_before_import",
        )
        _sha256(
            self.source_hash_after_import,
            field="receipt.source_hash_after_import",
        )


@runtime_checkable
class DestinationReviewReader(Protocol):
    """Read current combined metadata/artifact state by exact source ref."""

    def read(self, ref: ReviewSourceRef) -> DestinationReviewState | None: ...


class MappingDestinationReviewReader:
    """Small read adapter useful for tests and serialized preflight snapshots."""

    def __init__(
        self, values: Mapping[ReviewSourceRef, DestinationReviewState]
    ) -> None:
        self._values = dict(values)

    def read(self, ref: ReviewSourceRef) -> DestinationReviewState | None:
        return self._values.get(ref)


@dataclass(frozen=True, slots=True)
class ReviewImportUnit:
    review_record_id: str
    source_ref: ReviewSourceRef
    expected_source_hash: str
    metadata: tuple[tuple[str, str], ...]
    reasoning: ValidatedReasoning

    @property
    def metadata_dict(self) -> dict[str, str]:
        return dict(self.metadata)


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    before: str | None
    after: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"field": self.field, "before": self.before, "after": self.after}


@dataclass(frozen=True, slots=True)
class ReviewImportAction:
    status: str
    unit: ReviewImportUnit
    changes: tuple[FieldChange, ...]
    conflict: str | None
    expected_record_revision: str | None
    expected_assessment_revision: str | None
    expected_authority_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "review_record_id": self.unit.review_record_id,
            "source_ref": self.unit.source_ref.key,
            "expected_source_hash": self.unit.expected_source_hash,
            "conflict": self.conflict,
            "expected_record_revision": self.expected_record_revision,
            "expected_assessment_revision": self.expected_assessment_revision,
            "expected_authority_sha256": self.expected_authority_sha256,
            "reasoning_member": self.unit.reasoning.member,
            "reasoning_sha256": self.unit.reasoning.sha256,
            "reasoning_bytes": self.unit.reasoning.byte_size,
            "changes": [change.as_dict() for change in self.changes],
        }


@dataclass(frozen=True, slots=True)
class AtomicBookReviewImportRequest:
    """One holding's all-or-nothing commit request for a future adapter."""

    unit: ReviewImportUnit
    operation_id: str
    export_sha256: str
    expected_record_revision: str
    expected_assessment_revision: str | None
    expected_authority_sha256: str | None
    create_assessment: bool


@runtime_checkable
class AtomicBookReviewImportPort(Protocol):
    """Commit metadata + reasoning atomically with CAS and a durable receipt.

    Implementations must preserve unknown metadata, verify the current source
    hash again inside the write boundary, use recoverable writes/workspace
    leasing, and make ``operation_id`` idempotent.  This engine module never
    invokes the port implicitly.
    """

    def apply_atomically(
        self, request: AtomicBookReviewImportRequest
    ) -> DestinationReviewState: ...


@dataclass(frozen=True, slots=True)
class CommittedBookReview:
    source_ref: ReviewSourceRef
    operation_id: str
    record_revision: str
    assessment_revision: str
    assessment_sha256: str
    source_hash_after_import: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source_ref": self.source_ref.key,
            "operation_id": self.operation_id,
            "record_revision": self.record_revision,
            "assessment_revision": self.assessment_revision,
            "assessment_sha256": self.assessment_sha256,
            "source_hash_after_import": self.source_hash_after_import,
        }


@dataclass(frozen=True, slots=True)
class BookReviewCommitResult:
    export_sha256: str
    committed: tuple[CommittedBookReview, ...]
    skipped: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "librarytool.book-review-import-commit-result/v1",
            "export_sha256": self.export_sha256,
            "committed_count": len(self.committed),
            "skipped_count": self.skipped,
            "records": [value.as_dict() for value in self.committed],
        }


@dataclass(frozen=True, slots=True)
class ReviewImportPlan:
    export_schema: str
    export_sha256: str
    validated_records: int
    selected_review_records: int
    selected_source_refs: int
    actions: tuple[ReviewImportAction, ...]
    dry_run: bool = True

    @property
    def counts(self) -> dict[str, int]:
        counts = {name: 0 for name in ("create", "update", "conflict", "skip")}
        for action in self.actions:
            counts[action.status] += 1
        return counts

    @property
    def ready(self) -> bool:
        return self.counts["conflict"] == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "librarytool.book-review-import-plan/v1",
            "dry_run": True,
            "ready": self.ready,
            "export_schema": self.export_schema,
            "export_sha256": self.export_sha256,
            "validated_records": self.validated_records,
            "selected_review_records": self.selected_review_records,
            "selected_source_refs": self.selected_source_refs,
            "counts": self.counts,
            "actions": [action.as_dict() for action in self.actions],
        }

    def atomic_requests(self) -> tuple[AtomicBookReviewImportRequest, ...]:
        """Materialize explicit commit requests; never perform writes.

        A plan containing any conflict cannot be promoted to commit requests.
        Skips are idempotent and therefore produce no request.
        """

        if not self.ready:
            raise _error(
                "a plan with conflicts cannot be promoted to commit requests",
                code="import_plan_has_conflicts",
                conflicts=self.counts["conflict"],
            )
        requests: list[AtomicBookReviewImportRequest] = []
        for action in self.actions:
            if action.status not in {"create", "update"}:
                continue
            if action.expected_record_revision is None:
                raise _error(
                    "commit request is missing a record CAS revision",
                    code="invalid_destination_state",
                    source_ref=action.unit.source_ref.key,
                )
            operation_material = (
                self.export_sha256 + "\0" + action.unit.source_ref.key
            ).encode("utf-8")
            operation_id = "bri-" + hashlib.sha256(operation_material).hexdigest()
            requests.append(
                AtomicBookReviewImportRequest(
                    unit=action.unit,
                    operation_id=operation_id,
                    export_sha256=self.export_sha256,
                    expected_record_revision=action.expected_record_revision,
                    expected_assessment_revision=action.expected_assessment_revision,
                    expected_authority_sha256=action.expected_authority_sha256,
                    create_assessment=action.status == "create",
                )
            )
        return tuple(requests)


def commit_review_import_plan(
    plan: ReviewImportPlan,
    port: AtomicBookReviewImportPort,
    *,
    confirmation: str,
) -> BookReviewCommitResult:
    """Explicitly commit a ready, selected dry-run plan through an atomic port.

    There is intentionally no default confirmation and no API accepting a
    bundle without its already-resolved selection.  Each port call is one
    independently recoverable holding; a failure cannot split that holding's
    short metadata, reasoning artifact, or receipt.
    """

    if not isinstance(plan, ReviewImportPlan):
        raise TypeError("plan must be a ReviewImportPlan")
    if not isinstance(port, AtomicBookReviewImportPort):
        raise TypeError("port must implement AtomicBookReviewImportPort")
    if confirmation != BOOK_REVIEW_COMMIT_CONFIRMATION:
        raise _error(
            "book-review import commit requires its exact confirmation token",
            code="book_review_commit_confirmation_required",
        )
    if plan.selected_source_refs < 1:
        raise _error(
            "book-review commit requires an explicit non-empty selection",
            code="explicit_selection_required",
        )

    requests = plan.atomic_requests()
    results: list[CommittedBookReview] = []
    for request in requests:
        state = port.apply_atomically(request)
        if not isinstance(state, DestinationReviewState):
            raise TypeError("atomic import port returned an invalid state")
        receipt = state.import_receipt
        if (
            state.metadata_dict != request.unit.metadata_dict
            or state.assessment_sha256 != request.unit.reasoning.sha256
            or not state.assessment_revision
            or receipt is None
            or receipt.export_sha256 != plan.export_sha256
            or receipt.source_hash_before_import != request.unit.expected_source_hash
            or state.authority_sha256 != request.expected_authority_sha256
        ):
            raise _error(
                "atomic import result does not match its selected plan",
                code="book_review_commit_result_mismatch",
                source_ref=request.unit.source_ref.key,
            )
        results.append(
            CommittedBookReview(
                source_ref=request.unit.source_ref,
                operation_id=request.operation_id,
                record_revision=state.record_revision,
                assessment_revision=state.assessment_revision,
                assessment_sha256=state.assessment_sha256,
                source_hash_after_import=receipt.source_hash_after_import,
            )
        )
    return BookReviewCommitResult(
        export_sha256=plan.export_sha256,
        committed=tuple(results),
        skipped=plan.counts["skip"],
    )


def _field_changes(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    before_reasoning: str | None,
    after_reasoning: str,
) -> tuple[FieldChange, ...]:
    changes = [
        FieldChange(field, before.get(field), after.get(field))
        for field in DESKTOP_REVIEW_FIELDS
        if before.get(field) != after.get(field)
    ]
    if before_reasoning != after_reasoning:
        changes.append(
            FieldChange("assessment_sha256", before_reasoning, after_reasoning)
        )
    return tuple(changes)


def _destination_conflict(state: DestinationReviewState) -> str | None:
    metadata = state.metadata_dict
    has_metadata = bool(metadata)
    has_assessment = state.assessment_sha256 is not None
    has_priority = bool(metadata.get("scan_priority"))
    has_verdict = bool(metadata.get("scan_verdict"))
    if (has_metadata or has_assessment) and not (
        has_assessment and has_priority and has_verdict
    ):
        return "incomplete_existing_review"
    if has_assessment and not state.assessment_revision:
        return "missing_assessment_revision"
    if has_priority and metadata["scan_priority"] not in SCAN_PRIORITIES:
        return "invalid_existing_scan_priority"
    if has_verdict:
        try:
            _verdict(metadata["scan_verdict"])
        except BookReviewImportError:
            return "invalid_existing_scan_verdict"
    return None


def build_review_import_plan(
    bundle: ReviewedExportBundle,
    *,
    selection: ReviewSelection,
    current_sources: CurrentSourceIndex,
    destination: DestinationReviewReader,
) -> ReviewImportPlan:
    """Build a selected-only create/update/conflict/skip plan without writes."""

    if not isinstance(bundle, ReviewedExportBundle):
        raise TypeError("bundle must be ReviewedExportBundle")
    if not isinstance(selection, ReviewSelection):
        raise TypeError("selection must be ReviewSelection")
    if not isinstance(current_sources, CurrentSourceIndex):
        raise TypeError("current_sources must be CurrentSourceIndex")
    if not isinstance(destination, DestinationReviewReader):
        raise TypeError("destination must implement DestinationReviewReader")

    by_id = {record.record_id: record for record in bundle.records}
    by_ref = {
        link.ref: record for record in bundle.records for link in record.source_links
    }
    missing_ids = sorted(selection.review_record_ids - set(by_id))
    missing_refs = sorted(
        (ref.key for ref in selection.source_refs if ref not in by_ref)
    )
    if missing_ids or missing_refs:
        raise _error(
            "one or more explicit selectors do not exist in the reviewed export",
            code="selection_not_found",
            review_record_ids=missing_ids,
            source_refs=missing_refs,
        )

    selected_links: dict[
        ReviewSourceRef, tuple[ReviewedExportRecord, ExportSourceLink]
    ] = {}
    selected_record_ids: set[str] = set()
    for record_id in selection.review_record_ids:
        record = by_id[record_id]
        selected_record_ids.add(record.record_id)
        for link in record.source_links:
            selected_links[link.ref] = (record, link)
    for ref in selection.source_refs:
        record = by_ref[ref]
        selected_record_ids.add(record.record_id)
        link = next(link for link in record.source_links if link.ref == ref)
        selected_links[ref] = (record, link)

    actions: list[ReviewImportAction] = []
    for ref in sorted(selected_links):
        record, link = selected_links[ref]
        desired = record.metadata_dict
        unit = ReviewImportUnit(
            review_record_id=record.record_id,
            source_ref=ref,
            expected_source_hash=link.source_hash,
            metadata=record.metadata,
            reasoning=record.reasoning,
        )
        current = current_sources.get(ref)
        if current is None:
            actions.append(
                ReviewImportAction(
                    "conflict",
                    unit,
                    _field_changes(
                        {},
                        desired,
                        before_reasoning=None,
                        after_reasoning=record.reasoning.sha256,
                    ),
                    "current_source_missing",
                    None,
                    None,
                    None,
                )
            )
            continue
        state = destination.read(ref)
        if state is None:
            actions.append(
                ReviewImportAction(
                    "conflict",
                    unit,
                    (),
                    (
                        "current_source_hash_mismatch"
                        if current.source_hash != link.source_hash
                        else "destination_state_missing"
                    ),
                    None,
                    None,
                    None,
                )
            )
            continue
        if not isinstance(state, DestinationReviewState):
            raise TypeError("destination reader returned an invalid state")
        before = state.metadata_dict
        changes = _field_changes(
            before,
            desired,
            before_reasoning=state.assessment_sha256,
            after_reasoning=record.reasoning.sha256,
        )
        conflict = _destination_conflict(state)
        if current.source_hash != link.source_hash:
            receipt = state.import_receipt
            is_proven_idempotent_replay = bool(
                conflict is None
                and not changes
                and receipt is not None
                and receipt.export_sha256 == bundle.sha256
                and receipt.source_hash_before_import == link.source_hash
                and receipt.source_hash_after_import == current.source_hash
            )
            actions.append(
                ReviewImportAction(
                    "skip" if is_proven_idempotent_replay else "conflict",
                    unit,
                    changes,
                    None
                    if is_proven_idempotent_replay
                    else "current_source_hash_mismatch",
                    state.record_revision,
                    state.assessment_revision,
                    state.authority_sha256,
                )
            )
            continue
        if conflict:
            status = "conflict"
        elif not before and state.assessment_sha256 is None:
            status = "create"
        elif changes:
            status = "update"
        else:
            status = "skip"
        actions.append(
            ReviewImportAction(
                status,
                unit,
                changes,
                conflict,
                state.record_revision,
                state.assessment_revision,
                state.authority_sha256,
            )
        )
    return ReviewImportPlan(
        export_schema=REVIEWED_EXPORT_SCHEMA,
        export_sha256=bundle.sha256,
        validated_records=len(bundle.records),
        selected_review_records=len(selected_record_ids),
        selected_source_refs=len(selected_links),
        actions=tuple(actions),
    )


def load_destination_snapshot(
    path: str | Path,
) -> MappingDestinationReviewReader:
    """Load the small read-only destination-state interchange used by the CLI."""

    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise _error(
            "destination snapshot is unavailable",
            code="review_input_unavailable",
            cause_type=type(exc).__name__,
        ) from exc
    value, _payload = _load_strict_json(
        resolved,
        maximum=MAX_REVIEWED_EXPORT_BYTES,
        label="destination snapshot",
    )
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != DESTINATION_SNAPSHOT_SCHEMA
    ):
        raise _error(
            "destination snapshot schema is unsupported",
            code="unsupported_destination_snapshot_schema",
            field="schema",
        )
    records = value.get("records")
    if not isinstance(records, list) or len(records) > MAX_REVIEWED_RECORDS:
        raise _error(
            "destination snapshot records must be a bounded array",
            code="invalid_destination_snapshot",
            field="records",
        )
    states: dict[ReviewSourceRef, DestinationReviewState] = {}
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise _error(
                "destination snapshot row must be an object",
                code="invalid_destination_snapshot",
                field=f"records[{index}]",
            )
        ref = ReviewSourceRef(raw.get("namespace"), raw.get("source_id"))
        if ref in states:
            raise _error(
                "destination snapshot contains a duplicate source reference",
                code="duplicate_current_source_reference",
                source_ref=ref.key,
            )
        metadata = raw.get("metadata")
        if not isinstance(metadata, Mapping):
            raise _error(
                "destination metadata must be an object",
                code="invalid_destination_snapshot",
                field=f"records[{index}].metadata",
            )
        raw_receipt = raw.get("import_receipt")
        if raw_receipt is not None:
            if not isinstance(raw_receipt, Mapping):
                raise _error(
                    "destination import_receipt must be an object when present",
                    code="invalid_destination_snapshot",
                    field=f"records[{index}].import_receipt",
                )
            if set(raw_receipt) != {
                "export_sha256",
                "source_hash_before_import",
                "source_hash_after_import",
            }:
                raise _error(
                    "destination import_receipt fields are invalid",
                    code="invalid_destination_snapshot",
                    field=f"records[{index}].import_receipt",
                )
            receipt = ReviewImportReceiptBinding(
                export_sha256=raw_receipt["export_sha256"],
                source_hash_before_import=raw_receipt["source_hash_before_import"],
                source_hash_after_import=raw_receipt["source_hash_after_import"],
            )
        else:
            receipt = None
        states[ref] = DestinationReviewState.create(
            metadata=metadata,
            record_revision=raw.get("record_revision"),
            assessment_sha256=raw.get("assessment_sha256"),
            assessment_revision=raw.get("assessment_revision"),
            import_receipt=receipt,
            authority_sha256=raw.get("authority_sha256"),
        )
    return MappingDestinationReviewReader(states)


__all__ = [
    "AtomicBookReviewImportPort",
    "AtomicBookReviewImportRequest",
    "BOOK_REVIEW_SCHEMA",
    "BOOK_REVIEW_COMMIT_CONFIRMATION",
    "BookReviewCommitResult",
    "BookReviewImportError",
    "CommittedBookReview",
    "CurrentSourceIndex",
    "CurrentSourceRecord",
    "DESKTOP_REVIEW_FIELDS",
    "DESTINATION_SNAPSHOT_SCHEMA",
    "DestinationReviewReader",
    "DestinationReviewState",
    "ExportSourceLink",
    "FieldChange",
    "MAX_REASONING_BYTES",
    "MAX_SCAN_VERDICT_CHARACTERS",
    "MappingDestinationReviewReader",
    "REVIEWED_EXPORT_SCHEMA",
    "ReviewImportAction",
    "ReviewImportPlan",
    "ReviewImportReceiptBinding",
    "ReviewImportUnit",
    "ReviewSelection",
    "ReviewSourceRef",
    "ReviewedExportBundle",
    "ReviewedExportRecord",
    "SCAN_PRIORITIES",
    "ValidatedReasoning",
    "build_review_import_plan",
    "catalog_source_record_sha256",
    "commit_review_import_plan",
    "load_destination_snapshot",
    "load_reviewed_export",
]
