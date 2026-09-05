#!/usr/bin/env python3
"""Reconcile catalog-review scan priorities into Desktop book metadata.

The catalog review application owns the decisions, while Desktop owns the
canonical book rows that project into ``capture_book_metadata``.  This tool
joins a complete ``/api/scan-priorities`` export to its exact seed source and
writes the first-class ``scan_priority`` field to those Desktop rows.  It never
edits the immutable phone-ingest ``captures/*/meta.json`` envelope.

The command is a dry run unless both ``--apply`` and an unused ``--backup``
path are supplied.  Apply uses the Desktop recoverable write set, so a manual
source and an already-promoted build change as one recoverable transaction.

Example::

    python tools/reconcile_capture_scan_priorities.py \
      --seed scan-priority-seed.json \
      --priorities scan-priorities.json \
      --data-root "%APPDATA%/Library Tool" \
      --capture-manifest capture-media-manifest.json

    python tools/reconcile_capture_scan_priorities.py ... --apply \
      --backup D:/backups/capture-scan-priorities-before.zip

``scan-priorities.json`` may be the full version-2 response or the lightweight
``?view=effective`` version-1 response.  Version 1 must be accompanied by the
live D1 ``scan_triage_meta.candidate_seed_sha256`` value through
``--expected-seed-sha256`` or ``--expected-seed-sha256-file``.  Exact record
coverage, the seed bindings, and every current Desktop source hash are still
validated before either form can produce a plan.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from librarytool.adapters.filesystem.manual_entry_item_codec import (  # noqa: E402
    ManualEntryItemCodec,
)
from librarytool.adapters.filesystem.portable_book_bundle import (  # noqa: E402
    catalogue_source_sha256,
    resolve_manual_book_authority,
)
from librarytool.adapters.filesystem.recoverable_write_set import (  # noqa: E402
    RecoverableWriteSet,
)
from librarytool.adapters.filesystem.whl_catalogue_codec import (  # noqa: E402
    WhlCatalogueItemCodec,
)
from librarytool.catalog_enrichment.importers import (  # noqa: E402
    SourceRecord,
    resolve_source_paths,
)


SEED_SCHEMA = "catalog-scan-priority-seed/v2"
PRIORITY_EXPORT_SCHEMA = "catalog-scan-priorities/v2"
EFFECTIVE_PRIORITY_EXPORT_SCHEMA = "catalog-effective-scan-priorities/v1"
BACKUP_SCHEMA = "librarytool.capture-scan-priority-reconciliation-backup/1"
SCAN_PRIORITIES = frozenset({"n/s (no scan)", "Low", "Medium", "High"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReconciliationError(RuntimeError):
    """One deterministic validation or compare-and-swap check failed."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SeedBinding:
    record_id: str
    source_id: str
    source_hash: str
    title: str


@dataclass(frozen=True, slots=True)
class TargetChange:
    record_id: str
    source_id: str
    capture_id: str
    target_kind: Literal["manual_entries", "whl_builds"]
    target_id: str
    before_present: bool
    before: str | None
    after: str


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    seed_sha256: str
    priority_export_sha256: str
    manual_entries_sha256: str
    builds_sha256: str | None
    manual_entries_payload: bytes
    builds_payload: bytes | None
    assignments: tuple[tuple[str, str | None], ...]
    changes: tuple[TargetChange, ...]
    converged_stale_sources: tuple[str, ...]
    captured_records: int
    uncaptured_records: int
    unseeded_manual_rows: int
    manifest_records: int | None
    manifest_missing_records: tuple[str, ...]
    manifest_missing_captures: tuple[str, ...]
    manual_document: dict[str, Any]
    builds_document: dict[str, Any]

    @property
    def manual_changes(self) -> int:
        return sum(change.target_kind == "manual_entries" for change in self.changes)

    @property
    def build_changes(self) -> int:
        return sum(change.target_kind == "whl_builds" for change in self.changes)

    @property
    def assignment_counts(self) -> Counter[str]:
        return Counter(
            priority or "Unassessed" for _record_id, priority in self.assignments
        )


@dataclass(frozen=True, slots=True)
class ReconciliationPaths:
    output_dir: Path
    manual_entries: Path
    builds: Path
    captures_dir: Path | None


def _error(message: str, code: str) -> ReconciliationError:
    return ReconciliationError(message, code=code)


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error(f"duplicate JSON key: {key}", "invalid_json")
        result[key] = value
    return result


def _decode_json(payload: bytes, *, label: str) -> Any:
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except ReconciliationError:
        raise
    except (UnicodeError, ValueError) as exc:
        raise _error(
            f"{label} is not strict UTF-8 JSON: {exc}", "invalid_json"
        ) from exc


def _read_json(path: Path, *, label: str) -> tuple[Any, bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise _error(f"could not read {label}: {path}", "source_unavailable") from exc
    return _decode_json(payload, label=label), payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _record_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(row, Mapping)
        for key, row in value.items()
    ):
        raise _error(f"{label} must be a JSON object of row objects", "invalid_store")
    return value


def _seed_bindings(document: Any) -> tuple[str, dict[str, SeedBinding]]:
    if not isinstance(document, Mapping) or document.get("schema") != SEED_SCHEMA:
        raise _error(f"seed must use {SEED_SCHEMA}", "invalid_seed")
    seed_sha256 = document.get("seedSha256")
    if not isinstance(seed_sha256, str) or not _SHA256_RE.fullmatch(seed_sha256):
        raise _error("seedSha256 must be a lowercase SHA-256", "invalid_seed")
    records = document.get("records")
    if not isinstance(records, list):
        raise _error("seed.records must be an array", "invalid_seed")
    bindings: dict[str, SeedBinding] = {}
    source_ids: set[str] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise _error(f"seed.records[{index}] is not an object", "invalid_seed")
        record_id = raw.get("recordId")
        namespace = raw.get("sourceNamespace")
        source_id = raw.get("sourceId")
        source_hash = raw.get("sourceHash")
        title = raw.get("title", "")
        if namespace != "manual_entries":
            raise _error(
                f"{record_id or index}: unsupported source namespace {namespace!r}",
                "unsupported_source_namespace",
            )
        if not isinstance(source_id, str) or not source_id:
            raise _error(f"seed.records[{index}] has no sourceId", "invalid_seed")
        if source_id in source_ids:
            raise _error(
                f"duplicate seed sourceId: {source_id}", "duplicate_seed_source"
            )
        if not isinstance(record_id, str) or not record_id:
            raise _error(f"seed.records[{index}] has no recordId", "invalid_seed")
        expected_record_id = SourceRecord(
            namespace=namespace,
            source_id=source_id,
            data={},
        ).record_id
        if record_id != expected_record_id:
            raise _error(
                f"{record_id}: recordId does not match {namespace}:{source_id}",
                "seed_record_identity_mismatch",
            )
        if record_id in bindings:
            raise _error(
                f"duplicate seed recordId: {record_id}", "duplicate_seed_record"
            )
        if not isinstance(source_hash, str) or not _SHA256_RE.fullmatch(source_hash):
            raise _error(f"{record_id}: invalid sourceHash", "invalid_seed")
        if not isinstance(title, str):
            raise _error(f"{record_id}: title is not a string", "invalid_seed")
        source_ids.add(source_id)
        bindings[record_id] = SeedBinding(
            record_id=record_id,
            source_id=source_id,
            source_hash=source_hash,
            title=title,
        )
    metrics = document.get("metrics")
    if isinstance(metrics, Mapping) and "records" in metrics:
        if metrics["records"] != len(bindings):
            raise _error("seed metrics.records disagrees with records", "invalid_seed")
    return seed_sha256, bindings


def _priority_assignments(
    document: Any,
    *,
    seed_sha256: str,
    bindings: Mapping[str, SeedBinding],
    expected_live_seed_sha256: str | None,
) -> dict[str, str | None]:
    if not isinstance(document, Mapping):
        raise _error("priority export must be an object", "invalid_priority_export")
    schema = document.get("schema")
    if schema not in {PRIORITY_EXPORT_SCHEMA, EFFECTIVE_PRIORITY_EXPORT_SCHEMA}:
        raise _error(
            f"priority export must use {PRIORITY_EXPORT_SCHEMA} or "
            f"{EFFECTIVE_PRIORITY_EXPORT_SCHEMA}",
            "invalid_priority_export",
        )
    if expected_live_seed_sha256 is not None and (
        not isinstance(expected_live_seed_sha256, str)
        or not _SHA256_RE.fullmatch(expected_live_seed_sha256)
    ):
        raise _error(
            "expected live seed SHA must be a lowercase SHA-256",
            "invalid_expected_seed_sha256",
        )
    if schema == EFFECTIVE_PRIORITY_EXPORT_SCHEMA:
        if expected_live_seed_sha256 is None:
            raise _error(
                "effective-only priority export requires the live D1 "
                "candidate_seed_sha256",
                "expected_seed_sha256_required",
            )
        export_seed_sha256 = expected_live_seed_sha256
    else:
        summary = document.get("seed")
        export_seed_sha256 = (
            summary.get("seedSha256") if isinstance(summary, Mapping) else None
        )
        if (
            expected_live_seed_sha256 is not None
            and export_seed_sha256 != expected_live_seed_sha256
        ):
            raise _error(
                "full priority export and expected live seed SHA disagree",
                "priority_seed_mismatch",
            )
    if export_seed_sha256 != seed_sha256:
        raise _error(
            "priority export and seed have different seedSha256 values",
            "priority_seed_mismatch",
        )
    items = document.get("items")
    if not isinstance(items, list):
        raise _error(
            "priority export items must be an array", "invalid_priority_export"
        )
    assignments: dict[str, str | None] = {}
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise _error(
                f"priority export items[{index}] is not an object",
                "invalid_priority_export",
            )
        record_id = raw.get("recordId")
        if not isinstance(record_id, str) or record_id not in bindings:
            raise _error(
                f"priority export contains unknown recordId {record_id!r}",
                "unknown_priority_record",
            )
        if record_id in assignments:
            raise _error(
                f"priority export contains duplicate recordId {record_id}",
                "duplicate_priority_record",
            )
        priority = raw.get("effectivePriority")
        if priority is not None and priority not in SCAN_PRIORITIES:
            raise _error(
                f"{record_id}: invalid effectivePriority {priority!r}",
                "invalid_scan_priority",
            )
        if schema == PRIORITY_EXPORT_SCHEMA:
            source_ref = raw.get("sourceRef")
            binding = bindings[record_id]
            expected_ref = {
                "namespace": "manual_entries",
                "sourceId": binding.source_id,
                "sourceHash": binding.source_hash,
            }
            if not isinstance(source_ref, Mapping) or any(
                source_ref.get(key) != value for key, value in expected_ref.items()
            ):
                raise _error(
                    f"{record_id}: priority sourceRef disagrees with the seed",
                    "priority_source_mismatch",
                )
        assignments[record_id] = priority
    missing = sorted(set(bindings) - set(assignments))
    if missing:
        raise _error(
            f"priority export is incomplete ({len(missing)} seed records missing; "
            f"first: {missing[0]})",
            "incomplete_priority_export",
        )
    return assignments


def _stored_priority(
    row: Mapping[str, Any],
    *,
    label: str,
) -> tuple[bool, str | None]:
    if "scan_priority" not in row:
        return False, None
    value = row.get("scan_priority")
    if not isinstance(value, str) or (value and value not in SCAN_PRIORITIES):
        raise _error(
            f"{label} has invalid scan_priority {value!r}",
            "invalid_desktop_scan_priority",
        )
    return True, value


def _target_change(
    *,
    binding: SeedBinding,
    capture_id: str,
    target_kind: Literal["manual_entries", "whl_builds"],
    target_id: str,
    row: Mapping[str, Any],
    desired: str,
) -> TargetChange | None:
    present, before = _stored_priority(
        row,
        label=f"{target_kind}:{target_id}",
    )
    if present and before == desired:
        return None
    return TargetChange(
        record_id=binding.record_id,
        source_id=binding.source_id,
        capture_id=capture_id,
        target_kind=target_kind,
        target_id=target_id,
        before_present=present,
        before=before,
        after=desired,
    )


def _audit_manifest(
    document: Any | None,
    *,
    bindings: Mapping[str, SeedBinding],
    capture_ids: Mapping[str, str],
) -> tuple[int | None, tuple[str, ...], tuple[str, ...]]:
    if document is None:
        return None, (), ()
    records = document.get("records") if isinstance(document, Mapping) else None
    if records is None and isinstance(document, Mapping):
        records = document
    if not isinstance(records, Mapping):
        raise _error(
            "capture manifest must contain a records object",
            "invalid_capture_manifest",
        )
    for raw_record_id, raw in records.items():
        record_id = str(raw_record_id)
        if record_id not in bindings or not isinstance(raw, Mapping):
            raise _error(
                f"capture manifest contains unknown or invalid record {record_id}",
                "invalid_capture_manifest",
            )
        manifest_capture = raw.get("captureId")
        if manifest_capture is None:
            normalized_capture = ""
        elif isinstance(manifest_capture, str):
            normalized_capture = manifest_capture.strip()
        else:
            raise _error(
                f"{record_id}: capture manifest captureId is not a string or null",
                "invalid_capture_manifest",
            )
        if normalized_capture != capture_ids[record_id]:
            raise _error(
                f"{record_id}: manifest captureId disagrees with its desktop source",
                "capture_manifest_mismatch",
            )
    missing = tuple(sorted(set(bindings) - set(map(str, records))))
    missing_captures = tuple(
        record_id for record_id in missing if capture_ids[record_id]
    )
    return len(records), missing, missing_captures


def build_plan(
    *,
    seed_document: Any,
    priority_document: Any,
    priority_export_sha256: str,
    manual_document: Any,
    manual_entries_sha256: str,
    manual_entries_payload: bytes,
    builds_document: Any,
    builds_sha256: str | None,
    builds_payload: bytes | None,
    captures_dir: Path | None,
    capture_manifest: Any | None = None,
    expected_live_seed_sha256: str | None = None,
) -> ReconciliationPlan:
    """Validate all authorities and return an immutable reconciliation plan."""

    if _sha256(manual_entries_payload) != manual_entries_sha256:
        raise _error(
            "manual_entries payload and digest disagree",
            "invalid_plan_snapshot",
        )
    if (builds_payload is None) != (builds_sha256 is None) or (
        builds_payload is not None and _sha256(builds_payload) != builds_sha256
    ):
        raise _error(
            "whl_builds payload and digest disagree",
            "invalid_plan_snapshot",
        )
    seed_sha256, bindings = _seed_bindings(seed_document)
    assignments = _priority_assignments(
        priority_document,
        seed_sha256=seed_sha256,
        bindings=bindings,
        expected_live_seed_sha256=expected_live_seed_sha256,
    )
    manual = _record_mapping(manual_document, label="manual_entries.json")
    builds = _record_mapping(builds_document, label="whl_builds.json")
    changes: list[TargetChange] = []
    stale_converged: list[str] = []
    capture_ids: dict[str, str] = {}
    seen_targets: set[tuple[str, str]] = set()

    for record_id in sorted(bindings):
        binding = bindings[record_id]
        source = manual.get(binding.source_id)
        if not isinstance(source, Mapping):
            raise _error(
                f"{record_id}: manual source {binding.source_id} is missing",
                "desktop_source_not_found",
            )
        try:
            authority = resolve_manual_book_authority(
                binding.source_id,
                manual,
                builds,
            )
        except Exception as exc:
            code = getattr(exc, "code", "invalid_desktop_authority")
            raise _error(
                f"{record_id}: could not resolve desktop authority ({exc})",
                str(code),
            ) from exc
        capture_id = authority.capture_id.strip()
        capture_ids[record_id] = capture_id
        desired = assignments[record_id] or ""
        pending: list[TargetChange] = []
        manual_change = _target_change(
            binding=binding,
            capture_id=capture_id,
            target_kind="manual_entries",
            target_id=binding.source_id,
            row=source,
            desired=desired,
        )
        if manual_change is not None:
            pending.append(manual_change)
        if authority.storage_kind == "whl_builds":
            build_change = _target_change(
                binding=binding,
                capture_id=capture_id,
                target_kind="whl_builds",
                target_id=authority.storage_id,
                row=authority.active_row,
                desired=desired,
            )
            if build_change is not None:
                pending.append(build_change)

        try:
            current_hash = catalogue_source_sha256(
                "manual_entries",
                source,
                captures_path=captures_dir,
            )
        except Exception as exc:
            raise _error(
                f"{record_id}: could not hash the desktop source ({exc})",
                "desktop_source_hash_failed",
            ) from exc
        if current_hash != binding.source_hash:
            if pending:
                raise _error(
                    f"{record_id}: desktop source hash changed; refusing "
                    f"{len(pending)} pending update(s)",
                    "desktop_source_hash_mismatch",
                )
            stale_converged.append(record_id)
            continue
        for change in pending:
            target = (change.target_kind, change.target_id)
            if target in seen_targets:
                raise _error(
                    f"multiple assignments resolve to {change.target_kind}:{change.target_id}",
                    "duplicate_desktop_target",
                )
            seen_targets.add(target)
            changes.append(change)

    manifest_records, missing_manifest, missing_manifest_captures = _audit_manifest(
        capture_manifest,
        bindings=bindings,
        capture_ids=capture_ids,
    )
    return ReconciliationPlan(
        seed_sha256=seed_sha256,
        priority_export_sha256=priority_export_sha256,
        manual_entries_sha256=manual_entries_sha256,
        builds_sha256=builds_sha256,
        manual_entries_payload=bytes(manual_entries_payload),
        builds_payload=(bytes(builds_payload) if builds_payload is not None else None),
        assignments=tuple(sorted(assignments.items())),
        changes=tuple(changes),
        converged_stale_sources=tuple(stale_converged),
        captured_records=sum(bool(value) for value in capture_ids.values()),
        uncaptured_records=sum(not value for value in capture_ids.values()),
        unseeded_manual_rows=len(
            set(manual) - {row.source_id for row in bindings.values()}
        ),
        manifest_records=manifest_records,
        manifest_missing_records=missing_manifest,
        manifest_missing_captures=missing_manifest_captures,
        manual_document=copy.deepcopy(manual),
        builds_document=copy.deepcopy(builds),
    )


def resolve_paths(data_root: str | Path | None) -> ReconciliationPaths:
    try:
        source_paths = resolve_source_paths(data_root)
    except Exception as exc:
        raise _error(str(exc), "source_unavailable") from exc
    return ReconciliationPaths(
        output_dir=source_paths.output_dir,
        manual_entries=source_paths.manual_entries,
        builds=source_paths.output_dir / "whl_builds.json",
        captures_dir=source_paths.captures_dir,
    )


def _expected_seed_sha256_file(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise _error(
            f"could not read expected live seed SHA: {path}",
            "source_unavailable",
        ) from exc
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise _error(
            "expected seed SHA file is not UTF-8",
            "invalid_expected_seed_sha256",
        ) from exc
    if _SHA256_RE.fullmatch(text):
        return text
    document = _decode_json(payload, label="expected live seed SHA")

    values: set[str] = set()

    def visit(value: Any, *, key: str = "") -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(child, key=str(child_key))
        elif isinstance(value, list):
            for child in value:
                visit(child, key=key)
        elif (
            key in {"candidate_seed_sha256", "seedSha256", "value"}
            and isinstance(value, str)
            and _SHA256_RE.fullmatch(value)
        ):
            values.add(value)

    visit(document)
    if len(values) != 1:
        raise _error(
            "expected seed SHA file must contain one unambiguous lowercase SHA-256",
            "invalid_expected_seed_sha256",
        )
    return values.pop()


def plan_from_paths(
    *,
    seed_path: Path,
    priorities_path: Path,
    paths: ReconciliationPaths,
    capture_manifest_path: Path | None = None,
    expected_live_seed_sha256: str | None = None,
) -> ReconciliationPlan:
    seed, _seed_payload = _read_json(seed_path, label="scan-priority seed")
    priorities, priority_payload = _read_json(
        priorities_path,
        label="scan-priority export",
    )
    manual, manual_payload = _read_json(
        paths.manual_entries,
        label="manual_entries.json",
    )
    if paths.builds.is_file():
        builds, builds_payload = _read_json(paths.builds, label="whl_builds.json")
        builds_sha256 = _sha256(builds_payload)
    else:
        builds, builds_payload, builds_sha256 = {}, None, None
    manifest = None
    if capture_manifest_path is not None:
        manifest, _manifest_payload = _read_json(
            capture_manifest_path,
            label="capture media manifest",
        )
    return build_plan(
        seed_document=seed,
        priority_document=priorities,
        priority_export_sha256=_sha256(priority_payload),
        manual_document=manual,
        manual_entries_sha256=_sha256(manual_payload),
        manual_entries_payload=manual_payload,
        builds_document=builds,
        builds_sha256=builds_sha256,
        builds_payload=builds_payload,
        captures_dir=paths.captures_dir,
        capture_manifest=manifest,
        expected_live_seed_sha256=expected_live_seed_sha256,
    )


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reconciliation_timestamp(
    plan: ReconciliationPlan,
    *,
    now: datetime | None = None,
) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    for change in plan.changes:
        document = (
            plan.manual_document
            if change.target_kind == "manual_entries"
            else plan.builds_document
        )
        previous = _parse_timestamp(
            str(document[change.target_id].get("updated_at") or "")
        )
        if previous is not None and current <= previous:
            current = previous + timedelta(microseconds=1)
    return current.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _changed_documents(
    plan: ReconciliationPlan,
    *,
    timestamp: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manual = copy.deepcopy(plan.manual_document)
    builds = copy.deepcopy(plan.builds_document)
    for change in plan.changes:
        document = manual if change.target_kind == "manual_entries" else builds
        row = document[change.target_id]
        row["scan_priority"] = change.after
        row["updated_at"] = timestamp
        try:
            if change.target_kind == "manual_entries":
                ManualEntryItemCodec.validate_record(change.target_id, row)
            else:
                codec = WhlCatalogueItemCodec(
                    advance_revision=lambda previous: previous or "unused",
                    category_ids_for=tuple,
                    validate_representation_manifest=lambda _row: None,
                )
                codec.validate_managed_record(change.target_id, row)
                codec.validate_catalogue_metadata(
                    {
                        name: value
                        for name, value in row.items()
                        if name not in codec.managed_fields
                    }
                )
        except (TypeError, ValueError) as exc:
            raise _error(
                f"{change.target_kind}:{change.target_id} failed validation after update",
                "invalid_reconciled_row",
            ) from exc
    return manual, builds


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _assert_unused_backup(backup_path: Path, *, output_dir: Path) -> None:
    backup = backup_path.expanduser().resolve()
    output = output_dir.resolve()
    try:
        backup.relative_to(output)
    except ValueError:
        pass
    else:
        raise _error(
            "backup must be outside the mutable output directory",
            "unsafe_backup_path",
        )
    if os.path.lexists(backup):
        raise _error(f"backup already exists: {backup}", "backup_exists")


def _write_backup(
    backup_path: Path,
    *,
    paths: ReconciliationPaths,
    plan: ReconciliationPlan,
    timestamp: str,
) -> Path:
    backup = backup_path.expanduser().resolve()
    _assert_unused_backup(backup, output_dir=paths.output_dir)
    backup.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": BACKUP_SCHEMA,
        "createdAt": timestamp,
        "seedSha256": plan.seed_sha256,
        "priorityExportSha256": plan.priority_export_sha256,
        "files": {
            "manual_entries.json": plan.manual_entries_sha256,
            **(
                {"whl_builds.json": plan.builds_sha256}
                if plan.builds_sha256 is not None
                else {}
            ),
        },
        "changes": {
            "manualEntries": plan.manual_changes,
            "whlBuilds": plan.build_changes,
        },
    }
    try:
        with backup.open("xb") as stream:
            with zipfile.ZipFile(
                stream, mode="w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("manifest.json", _json_bytes(manifest))
                archive.writestr("manual_entries.json", plan.manual_entries_payload)
                if plan.builds_payload is not None:
                    archive.writestr("whl_builds.json", plan.builds_payload)
    except Exception:
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    return backup


def _current_file_sha256(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes()) if path.is_file() else None
    except OSError as exc:
        raise _error(f"could not re-read {path}", "source_unavailable") from exc


def apply_plan(
    plan: ReconciliationPlan,
    *,
    paths: ReconciliationPaths,
    backup_path: Path,
    now: datetime | None = None,
) -> tuple[Path | None, str | None]:
    """Persist a validated plan after backup and byte-level CAS verification."""

    if not plan.changes:
        return None, None
    _assert_unused_backup(backup_path, output_dir=paths.output_dir)
    timestamp = reconciliation_timestamp(plan, now=now)
    manual, builds = _changed_documents(plan, timestamp=timestamp)
    backup = _write_backup(
        backup_path,
        paths=paths,
        plan=plan,
        timestamp=timestamp,
    )
    write_set = RecoverableWriteSet(paths.output_dir)
    with write_set.workspace_lease():
        if _current_file_sha256(paths.manual_entries) != plan.manual_entries_sha256:
            raise _error(
                "manual_entries.json changed after planning",
                "desktop_compare_and_swap_failed",
            )
        if _current_file_sha256(paths.builds) != plan.builds_sha256:
            raise _error(
                "whl_builds.json changed after planning",
                "desktop_compare_and_swap_failed",
            )
        transaction = write_set.begin(
            operation_id=(
                "capture-scan-priority-reconcile:"
                + hashlib.sha256(
                    f"{plan.seed_sha256}\0{plan.priority_export_sha256}".encode()
                ).hexdigest()
            ),
            scope="capture_scan_priority_reconciliation",
            metadata={
                "seed_sha256": plan.seed_sha256,
                "priority_export_sha256": plan.priority_export_sha256,
            },
        )
        if plan.manual_changes:
            transaction.stage_write("manual_entries.json", _json_bytes(manual))
        if plan.build_changes:
            transaction.stage_write("whl_builds.json", _json_bytes(builds))
        transaction.commit(
            receipt={
                "kind": "capture_scan_priority_reconciliation",
                "updated_at": timestamp,
                "manual_changes": plan.manual_changes,
                "build_changes": plan.build_changes,
                "backup": str(backup),
            }
        )
    return backup, timestamp


def _print_plan(plan: ReconciliationPlan) -> None:
    print(f"seed records       : {len(plan.assignments)}")
    print(f"captured / no id   : {plan.captured_records} / {plan.uncaptured_records}")
    print(f"unseeded rows      : {plan.unseeded_manual_rows}")
    for label in ("High", "Medium", "Low", "n/s (no scan)", "Unassessed"):
        print(f"{label:<18} : {plan.assignment_counts[label]}")
    print(f"manual row changes : {plan.manual_changes}")
    print(f"active build changes: {plan.build_changes}")
    print(f"stale + converged  : {len(plan.converged_stale_sources)}")
    if plan.manifest_records is not None:
        print(f"manifest records   : {plan.manifest_records}")
        print(f"manifest omissions : {len(plan.manifest_missing_records)}")
        print(f"  carrying capture : {len(plan.manifest_missing_captures)}")
        for record_id in plan.manifest_missing_captures[:10]:
            print(f"    {record_id}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--seed", required=True, type=Path)
    parser.add_argument("--priorities", required=True, type=Path)
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--capture-manifest", type=Path)
    expected_seed = parser.add_mutually_exclusive_group()
    expected_seed.add_argument(
        "--expected-seed-sha256",
        help="live D1 scan_triage_meta candidate_seed_sha256",
    )
    expected_seed.add_argument(
        "--expected-seed-sha256-file",
        type=Path,
        help="text or D1 JSON file containing candidate_seed_sha256",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--backup",
        type=Path,
        help="new ZIP path outside Desktop output; required with --apply",
    )
    args = parser.parse_args(argv)
    try:
        if args.apply and args.backup is None:
            raise _error("--apply requires --backup", "backup_required")
        expected_live_seed_sha256 = args.expected_seed_sha256
        if args.expected_seed_sha256_file is not None:
            expected_live_seed_sha256 = _expected_seed_sha256_file(
                args.expected_seed_sha256_file
            )
        paths = resolve_paths(args.data_root)
        plan = plan_from_paths(
            seed_path=args.seed,
            priorities_path=args.priorities,
            paths=paths,
            capture_manifest_path=args.capture_manifest,
            expected_live_seed_sha256=expected_live_seed_sha256,
        )
        _print_plan(plan)
        if not args.apply:
            print("\nDRY RUN -- no files written.")
            return 0
        assert args.backup is not None
        backup, timestamp = apply_plan(
            plan,
            paths=paths,
            backup_path=args.backup,
        )
        if backup is None:
            print("\nAlready reconciled -- no files or backup written.")
        else:
            print(f"\nbackup            : {backup}")
            print(f"updated_at        : {timestamp}")
            print(
                "Desktop rows reconciled. Normal capture metadata sync publishes them."
            )
        return 0
    except ReconciliationError as exc:
        print(f"error [{exc.code}]: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
