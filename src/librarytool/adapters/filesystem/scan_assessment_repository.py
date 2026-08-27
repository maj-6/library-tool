"""Recoverable filesystem storage for source-keyed scan assessments.

The public key never becomes a path component.  Its UTF-8 namespace and
source id are separated by NUL and hashed exactly as specified by the storage
contract.  Every read treats the resulting directory as an untrusted locator:
the manifest identity, file kinds, authority chain, byte count, UTF-8, and
digest all have to agree before text is returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ...engine.errors import (
    ConflictError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from ...engine.scan_assessments import (
    MAX_SCAN_ASSESSMENT_BYTES,
    MAX_SCAN_ASSESSMENT_MANIFEST_BYTES,
    ScanAssessmentDraft,
    ScanAssessmentIntegrityError,
    ScanAssessmentKey,
    ScanAssessmentManifest,
    ScanAssessmentView,
    canonical_scan_assessment_json,
    scan_assessment_locator_digest,
    scan_assessment_not_found,
    validate_scan_assessment_operation_id,
    validate_scan_assessment_revision,
)
from .recoverable_write_set import RecoverableWriteSet


SCAN_ASSESSMENT_RELATIVE_ROOT = PurePosixPath("output/scan_assessments")
SCAN_ASSESSMENT_MANIFEST_NAME = "manifest.json"
SCAN_ASSESSMENT_TEXT_NAME = "assessment.md"

_OPERATION_SCHEMA = "librarytool.scan-assessment-operation/1"
_OPERATION_DIRECTORY = ".operations"
_MAX_OPERATION_RECEIPT_BYTES = 16 * 1024
_OPERATION_FIELDS = frozenset(
    {
        "schema",
        "operation_sha256",
        "request_sha256",
        "action",
        "namespace",
        "source_id",
        "result_revision",
        "deleted_revision",
    }
)
_HEX_SHA256 = frozenset("0123456789abcdef")
_KNOWN_ACTIONS = frozenset({"create", "update", "delete"})
_REPARSE_POINT_ATTRIBUTE = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


@dataclass(frozen=True, slots=True)
class _AuthoritySnapshot:
    states: tuple[os.stat_result, ...]


@dataclass(frozen=True, slots=True)
class _OperationReceipt:
    operation_sha256: str
    request_sha256: str
    action: str
    key: ScanAssessmentKey
    result_revision: str
    deleted_revision: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema": _OPERATION_SCHEMA,
            "operation_sha256": self.operation_sha256,
            "request_sha256": self.request_sha256,
            "action": self.action,
            "namespace": self.key.namespace,
            "source_id": self.key.source_id,
            "result_revision": self.result_revision,
            "deleted_revision": self.deleted_revision,
        }


class FilesystemScanAssessmentRepository:
    """Store Markdown and its identity-checked manifest as one write set.

    ``write_set`` should normally be the application's mutable-data-root write
    coordinator.  ``relative_root`` is intentionally normalized and relative
    to that authority, so callers cannot configure an assessment path outside
    the recoverable workspace.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        relative_root: str | PurePosixPath = SCAN_ASSESSMENT_RELATIVE_ROOT,
        clock: Callable[[], datetime] | None = None,
        revision_nonce: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        self._write_set = write_set
        self._relative_root = self._safe_relative_root(relative_root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._revision_nonce = revision_nonce or (lambda: secrets.token_hex(32))

    # -- public repository port ----------------------------------------

    def read(self, key: ScanAssessmentKey) -> ScanAssessmentView | None:
        self._require_key(key)
        try:
            with self._write_set.workspace_lease():
                return self._read_unlocked(key)
        except ScanAssessmentIntegrityError:
            raise
        except RepositoryError as exc:
            raise self._storage_failure(
                key,
                "the scan assessment could not be read safely",
                code="scan_assessment_read_failed",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise self._storage_failure(
                key,
                "the scan assessment could not be read",
                code="scan_assessment_read_failed",
                cause=exc,
            ) from exc

    def create(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        operation_id: str,
    ) -> ScanAssessmentView:
        self._require_key(key)
        self._require_draft(draft)
        operation_id = validate_scan_assessment_operation_id(operation_id)
        return self._guard_mutation(
            key,
            lambda: self._create_locked(key, draft, operation_id),
        )

    def update(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        expected_revision: str,
        operation_id: str,
    ) -> ScanAssessmentView:
        self._require_key(key)
        self._require_draft(draft)
        expected_revision = validate_scan_assessment_revision(expected_revision)
        operation_id = validate_scan_assessment_operation_id(operation_id)
        return self._guard_mutation(
            key,
            lambda: self._update_locked(
                key,
                draft,
                expected_revision,
                operation_id,
            ),
        )

    def delete(
        self,
        key: ScanAssessmentKey,
        expected_revision: str,
        operation_id: str,
    ) -> str:
        self._require_key(key)
        expected_revision = validate_scan_assessment_revision(expected_revision)
        operation_id = validate_scan_assessment_operation_id(operation_id)
        return self._guard_mutation(
            key,
            lambda: self._delete_locked(
                key,
                expected_revision,
                operation_id,
            ),
        )

    # -- mutation critical sections -----------------------------------

    def _create_locked(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        operation_id: str,
    ) -> ScanAssessmentView:
        request_sha256 = self._request_sha256("create", key, draft=draft)
        operation_sha256 = self._operation_sha256(operation_id)
        with self._write_set.workspace_lease():
            replay = self._receipt_replay(
                key,
                operation_sha256,
                request_sha256,
                "create",
            )
            if replay is not None:
                if not isinstance(replay, ScanAssessmentView):
                    raise self._integrity(key, "an operation receipt has a bad result")
                return replay
            if self._read_unlocked(key) is not None:
                raise ConflictError(
                    "the scan assessment already exists",
                    code="scan_assessment_exists",
                    details=key.as_dict(),
                )
            timestamp = self._now(key)
            view = self._new_view(
                key,
                draft,
                created_at=timestamp,
                updated_at=timestamp,
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
            )
            receipt = _OperationReceipt(
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
                action="create",
                key=key,
                result_revision=view.revision,
                deleted_revision="",
            )
            self._commit_write(key, view, receipt)
            return view

    def _update_locked(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        expected_revision: str,
        operation_id: str,
    ) -> ScanAssessmentView:
        request_sha256 = self._request_sha256(
            "update",
            key,
            draft=draft,
            expected_revision=expected_revision,
        )
        operation_sha256 = self._operation_sha256(operation_id)
        with self._write_set.workspace_lease():
            replay = self._receipt_replay(
                key,
                operation_sha256,
                request_sha256,
                "update",
            )
            if replay is not None:
                if not isinstance(replay, ScanAssessmentView):
                    raise self._integrity(key, "an operation receipt has a bad result")
                return replay
            current = self._read_unlocked(key)
            if current is None:
                raise scan_assessment_not_found(key)
            self._match_revision(key, current.revision, expected_revision)
            timestamp = self._now(key, not_before=current.manifest.updated_at)
            view = self._new_view(
                key,
                draft,
                created_at=current.manifest.created_at,
                updated_at=timestamp,
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
                previous_revision=current.revision,
            )
            receipt = _OperationReceipt(
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
                action="update",
                key=key,
                result_revision=view.revision,
                deleted_revision="",
            )
            self._commit_write(key, view, receipt)
            return view

    def _delete_locked(
        self,
        key: ScanAssessmentKey,
        expected_revision: str,
        operation_id: str,
    ) -> str:
        request_sha256 = self._request_sha256(
            "delete",
            key,
            expected_revision=expected_revision,
        )
        operation_sha256 = self._operation_sha256(operation_id)
        with self._write_set.workspace_lease():
            replay = self._receipt_replay(
                key,
                operation_sha256,
                request_sha256,
                "delete",
            )
            if replay is not None:
                if not isinstance(replay, str):
                    raise self._integrity(key, "an operation receipt has a bad result")
                return replay
            current = self._read_unlocked(key)
            if current is None:
                raise scan_assessment_not_found(key)
            self._match_revision(key, current.revision, expected_revision)
            receipt = _OperationReceipt(
                operation_sha256=operation_sha256,
                request_sha256=request_sha256,
                action="delete",
                key=key,
                result_revision="",
                deleted_revision=current.revision,
            )
            transaction = self._write_set.begin(
                operation_id=operation_sha256,
                scope="scan_assessment_delete",
                metadata={"locator": scan_assessment_locator_digest(key)},
            )
            transaction.stage_delete(
                self._artifact_relative(key, SCAN_ASSESSMENT_TEXT_NAME)
            )
            transaction.stage_delete(
                self._artifact_relative(key, SCAN_ASSESSMENT_MANIFEST_NAME)
            )
            transaction.stage_write(
                self._receipt_relative(operation_sha256),
                self._receipt_bytes(key, receipt),
            )
            transaction.commit(
                receipt={
                    "kind": "scan_assessment_delete",
                    "revision": current.revision,
                }
            )
            return current.revision

    def _guard_mutation(self, key: ScanAssessmentKey, callback: Callable[[], Any]):
        try:
            return callback()
        except (ConflictError, NotFoundError, ScanAssessmentIntegrityError):
            raise
        except ValidationError:
            raise
        except RepositoryError as exc:
            raise self._storage_failure(
                key,
                "the scan-assessment mutation could not be published safely",
                code="scan_assessment_write_failed",
                cause=exc,
            ) from exc
        except Exception as exc:
            raise self._storage_failure(
                key,
                "the scan-assessment mutation could not be published",
                code="scan_assessment_write_failed",
                cause=exc,
            ) from exc

    # -- reads and integrity -------------------------------------------

    def _read_unlocked(self, key: ScanAssessmentKey) -> ScanAssessmentView | None:
        directory_relative = self._artifact_directory_relative(key)
        before = self._directory_snapshot(key, directory_relative)
        if before is None:
            return None
        directory = self._absolute(directory_relative)
        try:
            names_seen: set[str] = set()
            with os.scandir(directory) as entries:
                for entry in entries:
                    names_seen.add(entry.name)
                    if len(names_seen) > 2:
                        break
        except OSError as exc:
            raise self._integrity(
                key,
                "the assessment directory cannot be inspected",
                cause=exc,
            ) from exc
        names = frozenset(names_seen)
        expected = frozenset({SCAN_ASSESSMENT_MANIFEST_NAME, SCAN_ASSESSMENT_TEXT_NAME})
        if not names:
            self._require_stable_authority(key, directory_relative, before)
            return None
        if names != expected:
            raise self._integrity(
                key,
                "the assessment directory has incomplete or unexpected members",
            )
        manifest_payload = self._read_regular_file(
            key,
            self._artifact_relative(key, SCAN_ASSESSMENT_MANIFEST_NAME),
            maximum=MAX_SCAN_ASSESSMENT_MANIFEST_BYTES,
            artifact="manifest",
        )
        text_payload = self._read_regular_file(
            key,
            self._artifact_relative(key, SCAN_ASSESSMENT_TEXT_NAME),
            maximum=MAX_SCAN_ASSESSMENT_BYTES,
            artifact="assessment",
        )
        self._require_stable_authority(key, directory_relative, before)
        manifest = self._decode_manifest(key, manifest_payload)
        if manifest.key != key:
            raise ScanAssessmentIntegrityError(
                "the scan-assessment manifest identity does not match its locator",
                code="scan_assessment_identity_mismatch",
                details={"expected": key.as_dict(), "actual": manifest.key.as_dict()},
            )
        if len(text_payload) != manifest.byte_size:
            raise ScanAssessmentIntegrityError(
                "the scan-assessment byte length does not match its manifest",
                code="scan_assessment_size_mismatch",
                details={**key.as_dict(), "artifact": "assessment"},
            )
        if hashlib.sha256(text_payload).hexdigest() != manifest.content_sha256:
            raise ScanAssessmentIntegrityError(
                "the scan-assessment digest does not match its manifest",
                code="scan_assessment_hash_mismatch",
                details={**key.as_dict(), "artifact": "assessment"},
            )
        try:
            text = text_payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ScanAssessmentIntegrityError(
                "the scan assessment is not valid UTF-8",
                code="invalid_scan_assessment_utf8",
                details={**key.as_dict(), "artifact": "assessment"},
            ) from exc
        try:
            return ScanAssessmentView(manifest=manifest, text=text)
        except (TypeError, ValidationError) as exc:
            raise self._integrity(
                key,
                "the assessment and manifest cannot be represented safely",
                cause=exc,
            ) from exc

    def _decode_manifest(
        self,
        key: ScanAssessmentKey,
        payload: bytes,
    ) -> ScanAssessmentManifest:
        raw = self._strict_json(key, payload, artifact="manifest")
        try:
            return ScanAssessmentManifest.from_dict(raw)
        except (TypeError, ValidationError) as exc:
            raise self._integrity(
                key,
                "the scan-assessment manifest is invalid",
                cause=exc,
            ) from exc

    def _strict_json(
        self,
        key: ScanAssessmentKey,
        payload: bytes,
        *,
        artifact: str,
    ) -> Any:
        def unique_object(pairs):
            value: dict[str, Any] = {}
            for name, item in pairs:
                if name in value:
                    raise ValueError("duplicate JSON member")
                value[name] = item
            return value

        try:
            return json.loads(
                payload.decode("utf-8", errors="strict"),
                object_pairs_hook=unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
        except (UnicodeError, ValueError) as exc:
            raise self._integrity(
                key,
                f"the {artifact} descriptor is not strict UTF-8 JSON",
                cause=exc,
            ) from exc

    def _read_regular_file(
        self,
        key: ScanAssessmentKey,
        relative: PurePosixPath,
        *,
        maximum: int,
        artifact: str,
        missing_ok: bool = False,
    ) -> bytes | None:
        path = self._absolute(relative)
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOINHERIT", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise self._integrity(key, f"the {artifact} file is missing")
        except OSError as exc:
            raise self._integrity(
                key,
                f"the {artifact} file cannot be opened safely",
                cause=exc,
            ) from exc
        try:
            try:
                opened_before = os.fstat(descriptor)
                named_before = os.lstat(path)
            except OSError as exc:
                raise self._integrity(
                    key,
                    f"the {artifact} file identity cannot be inspected",
                    cause=exc,
                ) from exc
            self._require_private_regular_file(
                key,
                artifact,
                opened_before,
                named_before,
            )
            if opened_before.st_size > maximum:
                raise ScanAssessmentIntegrityError(
                    "a stored scan-assessment artifact exceeds its byte limit",
                    code="scan_assessment_too_large",
                    details={**key.as_dict(), "artifact": artifact},
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read(maximum + 1)
            if len(payload) > maximum:
                raise ScanAssessmentIntegrityError(
                    "a stored scan-assessment artifact exceeds its byte limit",
                    code="scan_assessment_too_large",
                    details={**key.as_dict(), "artifact": artifact},
                )
            try:
                opened_after = os.fstat(descriptor)
                named_after = os.lstat(path)
            except OSError as exc:
                raise self._integrity(
                    key,
                    f"the {artifact} file changed while it was read",
                    cause=exc,
                ) from exc
            self._require_private_regular_file(
                key,
                artifact,
                opened_after,
                named_after,
            )
            if not os.path.samestat(opened_before, opened_after):
                raise self._integrity(
                    key,
                    f"the {artifact} file changed while it was read",
                )
            return payload
        finally:
            os.close(descriptor)

    def _require_private_regular_file(
        self,
        key: ScanAssessmentKey,
        artifact: str,
        opened: os.stat_result,
        named: os.stat_result,
    ) -> None:
        if (
            self._is_redirecting(named)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or opened.st_nlink != 1
            or named.st_nlink != 1
            or not os.path.samestat(opened, named)
        ):
            raise self._integrity(
                key,
                f"the {artifact} file is not one private regular file",
            )

    def _directory_snapshot(
        self,
        key: ScanAssessmentKey,
        relative: PurePosixPath,
    ) -> _AuthoritySnapshot | None:
        current = self._write_set.root
        try:
            root_info = os.lstat(current)
        except OSError as exc:
            raise self._integrity(
                key,
                "the scan-assessment storage authority is unavailable",
                cause=exc,
            ) from exc
        if self._is_redirecting(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise self._integrity(
                key,
                "the scan-assessment storage authority is unsafe",
            )
        states: list[os.stat_result] = [root_info]
        for part in relative.parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise self._integrity(
                    key,
                    "the scan-assessment storage authority cannot be inspected",
                    cause=exc,
                ) from exc
            if self._is_redirecting(info) or not stat.S_ISDIR(info.st_mode):
                raise self._integrity(
                    key,
                    "the scan-assessment storage authority crosses an unsafe node",
                )
            states.append(info)
        return _AuthoritySnapshot(tuple(states))

    def _require_stable_authority(
        self,
        key: ScanAssessmentKey,
        relative: PurePosixPath,
        before: _AuthoritySnapshot,
    ) -> None:
        after = self._directory_snapshot(key, relative)
        if (
            after is None
            or len(after.states) != len(before.states)
            or any(
                not os.path.samestat(left, right)
                for left, right in zip(before.states, after.states, strict=True)
            )
        ):
            raise self._integrity(
                key,
                "the scan-assessment storage authority changed during the read",
            )

    # -- operation receipts and replay --------------------------------

    def _receipt_replay(
        self,
        key: ScanAssessmentKey,
        operation_sha256: str,
        request_sha256: str,
        action: str,
    ) -> ScanAssessmentView | str | None:
        receipt = self._read_receipt(key, operation_sha256)
        if receipt is None:
            return None
        if (
            receipt.key != key
            or receipt.action != action
            or receipt.request_sha256 != request_sha256
        ):
            raise ConflictError(
                "operation_id was already used for another scan-assessment request",
                code="scan_assessment_operation_id_conflict",
                details={"operation_sha256": operation_sha256},
            )
        current = self._read_unlocked(key)
        if action in {"create", "update"}:
            if current is None or current.revision != receipt.result_revision:
                details: dict[str, Any] = {
                    **key.as_dict(),
                    "result_revision": receipt.result_revision,
                }
                if current is not None:
                    details["current_revision"] = current.revision
                raise ConflictError(
                    "the idempotent operation result has since been superseded",
                    code="scan_assessment_operation_superseded",
                    details=details,
                )
            return current
        if current is not None:
            raise ConflictError(
                "the deleted scan assessment has since been recreated",
                code="scan_assessment_operation_superseded",
                details={**key.as_dict(), "current_revision": current.revision},
            )
        return receipt.deleted_revision

    def _read_receipt(
        self,
        key: ScanAssessmentKey,
        operation_sha256: str,
    ) -> _OperationReceipt | None:
        directory_relative = self._relative_root / _OPERATION_DIRECTORY
        before = self._directory_snapshot(key, directory_relative)
        if before is None:
            return None
        payload = self._read_regular_file(
            key,
            self._receipt_relative(operation_sha256),
            maximum=_MAX_OPERATION_RECEIPT_BYTES,
            artifact="operation receipt",
            missing_ok=True,
        )
        self._require_stable_authority(key, directory_relative, before)
        if payload is None:
            return None
        raw = self._strict_json(key, payload, artifact="operation receipt")
        if not isinstance(raw, Mapping) or frozenset(raw) != _OPERATION_FIELDS:
            raise self._integrity(key, "an operation receipt has invalid fields")
        if raw["schema"] != _OPERATION_SCHEMA:
            raise self._integrity(key, "an operation receipt version is unsupported")
        stored_operation = self._stored_sha256(
            key,
            raw["operation_sha256"],
            "operation receipt",
        )
        if stored_operation != operation_sha256:
            raise self._integrity(key, "an operation receipt identity is invalid")
        request_sha256 = self._stored_sha256(
            key,
            raw["request_sha256"],
            "operation receipt",
        )
        action = raw["action"]
        if not isinstance(action, str) or action not in _KNOWN_ACTIONS:
            raise self._integrity(key, "an operation receipt action is invalid")
        try:
            receipt_key = ScanAssessmentKey(raw["namespace"], raw["source_id"])
            result_revision = self._optional_revision(raw["result_revision"])
            deleted_revision = self._optional_revision(raw["deleted_revision"])
        except (TypeError, ValidationError) as exc:
            raise self._integrity(
                key,
                "an operation receipt contains invalid identity data",
                cause=exc,
            ) from exc
        if (action == "delete") != bool(deleted_revision) or (
            action != "delete"
        ) != bool(result_revision):
            raise self._integrity(key, "an operation receipt result is invalid")
        return _OperationReceipt(
            operation_sha256=stored_operation,
            request_sha256=request_sha256,
            action=action,
            key=receipt_key,
            result_revision=result_revision,
            deleted_revision=deleted_revision,
        )

    # -- rendering and publication ------------------------------------

    def _new_view(
        self,
        key: ScanAssessmentKey,
        draft: ScanAssessmentDraft,
        *,
        created_at: str,
        updated_at: str,
        operation_sha256: str,
        request_sha256: str,
        previous_revision: str = "",
    ) -> ScanAssessmentView:
        payload = draft.utf8_bytes
        revision = self._new_revision(
            key,
            operation_sha256,
            request_sha256,
            previous_revision=previous_revision,
        )
        manifest = ScanAssessmentManifest(
            key=key,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
            revision=revision,
            created_at=created_at,
            updated_at=updated_at,
            provenance=draft.provenance,
            canonical_item_id=draft.canonical_item_id,
            capture_id=draft.capture_id,
        )
        return ScanAssessmentView(manifest=manifest, text=draft.text)

    def _commit_write(
        self,
        key: ScanAssessmentKey,
        view: ScanAssessmentView,
        receipt: _OperationReceipt,
    ) -> None:
        manifest_payload = (
            canonical_scan_assessment_json(view.manifest.as_dict()) + b"\n"
        )
        if len(manifest_payload) > MAX_SCAN_ASSESSMENT_MANIFEST_BYTES:
            raise self._integrity(key, "the generated manifest exceeds its limit")
        transaction = self._write_set.begin(
            operation_id=receipt.operation_sha256,
            scope="scan_assessment_write",
            metadata={"locator": scan_assessment_locator_digest(key)},
        )
        # The manifest is deliberately the final artifact publication.  The
        # recoverable write set rolls back either order on ordinary failure,
        # while this ordering also minimizes inconsistency during a hard crash.
        transaction.stage_write(
            self._artifact_relative(key, SCAN_ASSESSMENT_TEXT_NAME),
            view.text.encode("utf-8", errors="strict"),
        )
        transaction.stage_write(
            self._artifact_relative(key, SCAN_ASSESSMENT_MANIFEST_NAME),
            manifest_payload,
        )
        transaction.stage_write(
            self._receipt_relative(receipt.operation_sha256),
            self._receipt_bytes(key, receipt),
        )
        transaction.commit(
            receipt={
                "kind": f"scan_assessment_{receipt.action}",
                "revision": view.revision,
            }
        )

    def _receipt_bytes(
        self,
        key: ScanAssessmentKey,
        receipt: _OperationReceipt,
    ) -> bytes:
        payload = canonical_scan_assessment_json(receipt.as_dict()) + b"\n"
        if len(payload) > _MAX_OPERATION_RECEIPT_BYTES:
            raise self._integrity(key, "the operation receipt exceeds its limit")
        return payload

    def _new_revision(
        self,
        key: ScanAssessmentKey,
        operation_sha256: str,
        request_sha256: str,
        *,
        previous_revision: str,
    ) -> str:
        try:
            nonce = self._revision_nonce()
        except Exception as exc:
            raise self._storage_failure(
                key,
                "the scan-assessment revision source failed",
                code="scan_assessment_revision_failed",
                cause=exc,
            ) from exc
        if (
            not isinstance(nonce, str)
            or not nonce
            or len(nonce) > 512
            or any(0xD800 <= ord(character) <= 0xDFFF for character in nonce)
        ):
            raise self._storage_failure(
                key,
                "the scan-assessment revision source returned invalid data",
                code="scan_assessment_revision_failed",
            )
        material = "\0".join(
            (
                scan_assessment_locator_digest(key),
                operation_sha256,
                request_sha256,
                previous_revision,
                nonce,
            )
        ).encode("utf-8", errors="strict")
        revision = "sa-" + hashlib.sha256(material).hexdigest()
        if revision == previous_revision:
            raise self._storage_failure(
                key,
                "the scan-assessment revision did not advance",
                code="scan_assessment_revision_failed",
            )
        return revision

    def _now(self, key: ScanAssessmentKey, *, not_before: str = "") -> str:
        try:
            value = self._clock()
        except Exception as exc:
            raise self._storage_failure(
                key,
                "the scan-assessment clock failed",
                code="scan_assessment_clock_failed",
                cause=exc,
            ) from exc
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise self._storage_failure(
                key,
                "the scan-assessment clock returned a naive timestamp",
                code="scan_assessment_clock_failed",
            )
        value = value.astimezone(timezone.utc)
        if not_before:
            candidate = (
                not_before[:-1] + "+00:00" if not_before.endswith("Z") else not_before
            )
            prior = datetime.fromisoformat(candidate).astimezone(timezone.utc)
            if value <= prior:
                value = prior + timedelta(microseconds=1)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    # -- identifiers, paths, and safe errors ---------------------------

    @staticmethod
    def _safe_relative_root(value: str | PurePosixPath) -> PurePosixPath:
        raw = str(value)
        pure = PurePosixPath(raw)
        if (
            not raw
            or pure.is_absolute()
            or pure.as_posix() != raw
            or "\\" in raw
            or ":" in raw
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any(
                re.fullmatch(r"[A-Za-z0-9._-]+", part, re.ASCII) is None
                for part in pure.parts
            )
            or pure.parts[0].casefold() == ".transactions"
        ):
            raise ValueError("relative_root must be a normalized relative path")
        return pure

    def _artifact_directory_relative(
        self,
        key: ScanAssessmentKey,
    ) -> PurePosixPath:
        return self._relative_root / scan_assessment_locator_digest(key)

    def _artifact_relative(
        self,
        key: ScanAssessmentKey,
        name: str,
    ) -> PurePosixPath:
        return self._artifact_directory_relative(key) / name

    def _receipt_relative(self, operation_sha256: str) -> PurePosixPath:
        return self._relative_root / _OPERATION_DIRECTORY / (operation_sha256 + ".json")

    def _absolute(self, relative: PurePosixPath) -> Path:
        # Every caller supplies a path built exclusively from a validated root,
        # a SHA-256 digest, and fixed member names.
        return self._write_set.root.joinpath(*relative.parts)

    @staticmethod
    def _operation_sha256(operation_id: str) -> str:
        return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _request_sha256(
        action: str,
        key: ScanAssessmentKey,
        *,
        draft: ScanAssessmentDraft | None = None,
        expected_revision: str = "",
    ) -> str:
        value: dict[str, Any] = {
            "action": action,
            **key.as_dict(),
            "expected_revision": expected_revision,
            "draft": None if draft is None else draft.as_dict(),
        }
        return hashlib.sha256(canonical_scan_assessment_json(value)).hexdigest()

    @staticmethod
    def _match_revision(
        key: ScanAssessmentKey,
        current_revision: str,
        expected_revision: str,
    ) -> None:
        if current_revision != expected_revision:
            raise ConflictError(
                "the scan assessment changed since it was read",
                code="scan_assessment_revision_conflict",
                details={
                    **key.as_dict(),
                    "expected_revision": expected_revision,
                    "current_revision": current_revision,
                },
            )

    @staticmethod
    def _optional_revision(value: Any) -> str:
        if value == "":
            return ""
        return validate_scan_assessment_revision(value)

    @staticmethod
    def _stored_sha256(
        key: ScanAssessmentKey,
        value: Any,
        artifact: str,
    ) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in _HEX_SHA256 for character in value)
        ):
            raise ScanAssessmentIntegrityError(
                "stored scan-assessment integrity metadata is invalid",
                details={**key.as_dict(), "artifact": artifact},
            )
        return value

    @staticmethod
    def _is_redirecting(info: os.stat_result) -> bool:
        if stat.S_ISLNK(info.st_mode):
            return True
        if os.name != "nt" or not _REPARSE_POINT_ATTRIBUTE:
            return False
        attributes = int(getattr(info, "st_file_attributes", 0))
        return bool(attributes & _REPARSE_POINT_ATTRIBUTE)

    @staticmethod
    def _require_key(value: Any) -> None:
        if not isinstance(value, ScanAssessmentKey):
            raise TypeError("key must be a ScanAssessmentKey")

    @staticmethod
    def _require_draft(value: Any) -> None:
        if not isinstance(value, ScanAssessmentDraft):
            raise TypeError("draft must be a ScanAssessmentDraft")

    @staticmethod
    def _integrity(
        key: ScanAssessmentKey,
        reason: str,
        *,
        cause: Exception | None = None,
    ) -> ScanAssessmentIntegrityError:
        details: dict[str, Any] = {**key.as_dict(), "reason": reason}
        if cause is not None:
            details["cause_type"] = type(cause).__name__
        return ScanAssessmentIntegrityError(
            "the stored scan assessment failed integrity validation",
            details=details,
        )

    @staticmethod
    def _storage_failure(
        key: ScanAssessmentKey,
        message: str,
        *,
        code: str,
        cause: Exception | None = None,
    ) -> RepositoryError:
        details: dict[str, Any] = key.as_dict()
        if cause is not None:
            details["cause_type"] = type(cause).__name__
        return RepositoryError(
            message,
            code=code,
            details=details,
            retryable=True,
        )


__all__ = [
    "SCAN_ASSESSMENT_MANIFEST_NAME",
    "SCAN_ASSESSMENT_RELATIVE_ROOT",
    "SCAN_ASSESSMENT_TEXT_NAME",
    "FilesystemScanAssessmentRepository",
]
