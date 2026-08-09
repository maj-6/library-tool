"""Recoverable desktop membership lifecycle for capture photo assets.

Deleting a capture page is a logical membership change.  The portable asset,
desktop import row, and every owned file remain in place so a Trash restore can
reactivate the same stable ``capture_id``/``asset_id`` identity.  Membership is
recorded in an additive ``desktop_lifecycle`` object; the existing Android
processing ``lifecycle`` object is deliberately left untouched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager, TypeAlias

from ...engine.errors import ConflictError, NotFoundError, RepositoryError
from ...engine.raster_artifacts import RasterArtifactKey, RasterArtifactView
from .corrections_artifact_repository import (
    _AuthorityDirectorySnapshot,
    _AuthoritySnapshot,
    _finish_verified_regular,
    _open_verified_regular,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    RecoverableWriteTransaction,
    _is_redirecting_path,
)


CAPTURE_ASSET_LIFECYCLE_FIELD = "desktop_lifecycle"
CAPTURE_ASSET_LIFECYCLE_RECEIPT_SCHEMA = "librarytool.capture-asset-lifecycle-receipt"
CAPTURE_ASSET_LIFECYCLE_RECEIPT_VERSION = 1
CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA = "librarytool.capture-asset-lifecycle-inverse/1"

_PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
_PHOTO_ASSETS_NAME = "photo_assets.json"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAPTURE_ARTIFACT_RE = re.compile(r"^capture:[0-9a-f]{40}:(?:display|original)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LIFECYCLE_FIELDS = frozenset({"state", "revision", "updated_at"})
_INVERSE_FIELDS = frozenset(
    {
        "schema",
        "action",
        "source_operation_id",
        "item_id",
        "capture_id",
        "asset_id",
        "artifact_id",
        "artifact_revision",
        "capture_order",
        "expected_lifecycle",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "operation_id",
        "action",
        "item_id",
        "capture_id",
        "asset_id",
        "artifact_id",
        "artifact_revision",
        "capture_order",
        "before_lifecycle",
        "after_lifecycle",
        "item_updated_at",
        "inverse",
        "replayed",
    }
)
_RECEIPT_FIELDS = frozenset(
    {"schema", "version", "operation_id", "action", "command_sha256", "result"}
)

CaptureIdentityLookup: TypeAlias = Callable[[str], str | None]
CaptureDirectoryLookup: TypeAlias = Callable[[str], Path]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]
ArtifactLookup: TypeAlias = Callable[[RasterArtifactKey], RasterArtifactView | None]
ItemUpdatedAtPublication: TypeAlias = Callable[[str], tuple[Path, bytes, str]]


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _opaque_identity(namespace: str, *parts: Any) -> str:
    encoded = json.dumps(
        parts,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(encoded).hexdigest()[:40]}"


def _next_timestamp(previous: Any) -> int:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if isinstance(previous, int) and not isinstance(previous, bool) and previous >= now:
        return previous + 1
    return now


def _repository_error(message: str, *, code: str, **details: Any) -> RepositoryError:
    return RepositoryError(message, code=code, details=details)


def _bounded_text(value: Any, *, field: str, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(
            ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _lifecycle(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or frozenset(value) != _LIFECYCLE_FIELDS:
        raise ValueError(f"{field} is invalid")
    state = value.get("state")
    revision = value.get("revision")
    updated_at = value.get("updated_at")
    if (
        state not in {"active", "deleted"}
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision <= 0
        or isinstance(updated_at, bool)
        or not isinstance(updated_at, int)
        or updated_at <= 0
    ):
        raise ValueError(f"{field} is invalid")
    return {
        "state": state,
        "revision": revision,
        "updated_at": updated_at,
    }


def _command_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class _CaptureAssetTarget:
    item_id: str
    capture_id: str
    asset_id: str
    directory: Path
    manifest_path: Path
    manifest: dict[str, Any]
    asset_index: int
    import_index: int
    capture_order: int
    display_artifact_id: str
    original_artifact_id: str

    @property
    def asset(self) -> dict[str, Any]:
        return self.manifest["assets"][self.asset_index]


class FilesystemCaptureAssetLifecycleStore:
    """Atomically delete and restore one stable capture asset membership."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        coordination_write_set: RecoverableWriteSet,
        storage_root: Path,
        capture_authority_root: Path,
        capture_id_for: CaptureIdentityLookup,
        capture_directory_for: CaptureDirectoryLookup,
        artifact_for: ArtifactLookup,
        lock_context_for: LockContextFactory,
        item_updated_at_publication_for: ItemUpdatedAtPublication,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not isinstance(coordination_write_set, RecoverableWriteSet):
            raise TypeError("coordination_write_set must be a RecoverableWriteSet")
        for callback, name in (
            (capture_id_for, "capture_id_for"),
            (capture_directory_for, "capture_directory_for"),
            (artifact_for, "artifact_for"),
            (lock_context_for, "lock_context_for"),
            (item_updated_at_publication_for, "item_updated_at_publication_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._coordination_write_set = coordination_write_set
        self._storage_root = self._managed_root(storage_root, "storage_root")
        self._capture_root = self._managed_root(
            capture_authority_root,
            "capture_authority_root",
        )
        self._capture_id_for = capture_id_for
        self._capture_directory_for = capture_directory_for
        self._artifact_for = artifact_for
        self._lock_context_for = lock_context_for
        self._item_updated_at_publication_for = item_updated_at_publication_for

    def delete_capture_asset(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
        operation_id: str,
    ) -> Mapping[str, Any]:
        """Mark one active capture asset deleted and return its restore inverse."""

        key = RasterArtifactKey(item_id, artifact_id)
        self._operation_id(operation_id)
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("expected_revision is required")
        command_sha = _command_sha256(
            {
                "action": "delete",
                "item_id": key.item_id,
                "artifact_id": key.artifact_id,
                "expected_revision": expected_revision,
            }
        )
        receipt_path = self._receipt_path(operation_id)
        with self._coordination_write_set.workspace_lease():
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    replay = self._replay(receipt_path, operation_id, command_sha)
                    if replay is not None:
                        return replay
                    self._assert_artifact_revision(key, expected_revision)
                    target = self._target_for_artifact(
                        key.item_id,
                        key.artifact_id,
                        required=True,
                    )
                    assert target is not None
                    before = self._asset_lifecycle(target)
                    if before is not None and before["state"] == "deleted":
                        raise ConflictError(
                            "the capture asset is already deleted",
                            code="capture_asset_already_deleted",
                            details={
                                "item_id": key.item_id,
                                "artifact_id": key.artifact_id,
                                "lifecycle_revision": before["revision"],
                            },
                        )
                    after = self._next_lifecycle(before, "deleted")
                    target.asset[CAPTURE_ASSET_LIFECYCLE_FIELD] = after
                    return self._commit(
                        action="delete",
                        target=target,
                        artifact_id=key.artifact_id,
                        artifact_revision=expected_revision,
                        operation_id=operation_id,
                        command_sha=command_sha,
                        before=before,
                        after=after,
                        receipt_path=receipt_path,
                    )

    def restore_capture_asset(
        self,
        item_id: str,
        artifact_id: str,
        inverse: Mapping[str, Any],
        operation_id: str,
    ) -> Mapping[str, Any]:
        """Apply a persisted delete inverse to the exact deleted asset."""

        key = RasterArtifactKey(item_id, artifact_id)
        self._operation_id(operation_id)
        normalized_inverse = self._normalize_inverse(inverse)
        if (
            normalized_inverse["action"] != "restore"
            or normalized_inverse["item_id"] != key.item_id
            or normalized_inverse["artifact_id"] != key.artifact_id
        ):
            raise ConflictError(
                "the capture asset restore inverse belongs to another target",
                code="capture_asset_restore_target_conflict",
                details={
                    "item_id": key.item_id,
                    "artifact_id": key.artifact_id,
                },
            )
        command_sha = _command_sha256(
            {
                "action": "restore",
                "item_id": key.item_id,
                "artifact_id": key.artifact_id,
                "inverse": normalized_inverse,
            }
        )
        receipt_path = self._receipt_path(operation_id)
        with self._coordination_write_set.workspace_lease():
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    replay = self._replay(receipt_path, operation_id, command_sha)
                    if replay is not None:
                        return replay
                    self._assert_persisted_delete_inverse(normalized_inverse)
                    target = self._target_for_artifact(
                        key.item_id,
                        key.artifact_id,
                        required=True,
                    )
                    assert target is not None
                    if (
                        target.capture_id != normalized_inverse["capture_id"]
                        or target.asset_id != normalized_inverse["asset_id"]
                        or target.capture_order != normalized_inverse["capture_order"]
                    ):
                        raise ConflictError(
                            "the capture asset restore target changed",
                            code="capture_asset_restore_target_conflict",
                            details={
                                "item_id": key.item_id,
                                "artifact_id": key.artifact_id,
                            },
                        )
                    before = self._asset_lifecycle(target)
                    expected = normalized_inverse["expected_lifecycle"]
                    if before != expected or before["state"] != "deleted":
                        raise ConflictError(
                            "the capture asset lifecycle changed after deletion",
                            code="capture_asset_lifecycle_revision_conflict",
                            details={
                                "item_id": key.item_id,
                                "artifact_id": key.artifact_id,
                                "expected_revision": expected["revision"],
                                "actual_revision": before and before["revision"],
                            },
                        )
                    after = self._next_lifecycle(before, "active")
                    target.asset[CAPTURE_ASSET_LIFECYCLE_FIELD] = after
                    return self._commit(
                        action="restore",
                        target=target,
                        artifact_id=key.artifact_id,
                        artifact_revision=normalized_inverse["artifact_revision"],
                        operation_id=operation_id,
                        command_sha=command_sha,
                        before=before,
                        after=after,
                        receipt_path=receipt_path,
                    )

    def _commit(
        self,
        *,
        action: str,
        target: _CaptureAssetTarget,
        artifact_id: str,
        artifact_revision: str,
        operation_id: str,
        command_sha: str,
        before: Mapping[str, Any] | None,
        after: Mapping[str, Any],
        receipt_path: Path,
    ) -> Mapping[str, Any]:
        transaction = self._write_set.begin(
            operation_id=operation_id,
            scope=f"capture-asset-{action}",
            metadata={
                "item_id": target.item_id,
                "capture_id": target.capture_id,
                "asset_id": target.asset_id,
                "artifact_id": artifact_id,
            },
        )
        item_updated_at = self._stage_item_updated_at(transaction, target.item_id)
        inverse = self._inverse(
            action="restore" if action == "delete" else "delete",
            source_operation_id=operation_id,
            target=target,
            artifact_id=artifact_id,
            artifact_revision=artifact_revision,
            expected_lifecycle=after,
        )
        result = {
            "operation_id": operation_id,
            "action": action,
            "item_id": target.item_id,
            "capture_id": target.capture_id,
            "asset_id": target.asset_id,
            "artifact_id": artifact_id,
            "artifact_revision": artifact_revision,
            "capture_order": target.capture_order,
            "before_lifecycle": dict(before) if before is not None else None,
            "after_lifecycle": dict(after),
            "item_updated_at": item_updated_at,
            "inverse": inverse,
            "replayed": False,
        }
        receipt = {
            "schema": CAPTURE_ASSET_LIFECYCLE_RECEIPT_SCHEMA,
            "version": CAPTURE_ASSET_LIFECYCLE_RECEIPT_VERSION,
            "operation_id": operation_id,
            "action": action,
            "command_sha256": command_sha,
            "result": result,
        }
        transaction.stage_write(
            self._relative(target.manifest_path),
            _canonical_json(target.manifest),
        )
        # Receipt publication is terminal: a replay cannot be visible before
        # both the item timestamp and membership manifest are recoverable.
        transaction.stage_write(
            self._relative(receipt_path),
            _canonical_json(receipt),
        )
        transaction.commit(receipt=result)
        return result

    def _stage_item_updated_at(
        self,
        transaction: RecoverableWriteTransaction,
        item_id: str,
    ) -> str:
        item_path, item_payload, item_updated_at = (
            self._item_updated_at_publication_for(item_id)
        )
        item_path = Path(item_path)
        self._assert_target(item_path, self._write_set.root)
        if not isinstance(item_payload, bytes):
            raise TypeError("item updated_at payload must be bytes")
        if (
            not isinstance(item_updated_at, str)
            or not item_updated_at
            or len(item_updated_at) > 2048
        ):
            raise TypeError("item updated_at revision must be a non-empty string")
        transaction.stage_write(self._relative(item_path), item_payload)
        return item_updated_at

    @staticmethod
    def _next_lifecycle(
        before: Mapping[str, Any] | None,
        state: str,
    ) -> dict[str, Any]:
        return {
            "state": state,
            "revision": before["revision"] + 1 if before is not None else 1,
            "updated_at": _next_timestamp(
                before.get("updated_at") if before is not None else None
            ),
        }

    @staticmethod
    def _inverse(
        *,
        action: str,
        source_operation_id: str,
        target: _CaptureAssetTarget,
        artifact_id: str,
        artifact_revision: str,
        expected_lifecycle: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "schema": CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA,
            "action": action,
            "source_operation_id": source_operation_id,
            "item_id": target.item_id,
            "capture_id": target.capture_id,
            "asset_id": target.asset_id,
            "artifact_id": artifact_id,
            "artifact_revision": artifact_revision,
            "capture_order": target.capture_order,
            "expected_lifecycle": dict(expected_lifecycle),
        }

    def _assert_artifact_revision(
        self,
        key: RasterArtifactKey,
        expected_revision: str,
    ) -> None:
        artifact = self._artifact_for(key)
        if artifact is None:
            raise NotFoundError(
                "the capture asset does not exist",
                code="raster_artifact_not_found",
                details=key.as_dict(),
            )
        if artifact.revision != expected_revision:
            raise ConflictError(
                "the capture asset revision changed",
                code="raster_resource_revision_conflict",
                details={
                    **key.as_dict(),
                    "expected_revision": expected_revision,
                    "actual_revision": artifact.revision,
                },
            )

    def _target_for_artifact(
        self,
        item_id: str,
        artifact_id: str,
        *,
        required: bool,
    ) -> _CaptureAssetTarget | None:
        if _CAPTURE_ARTIFACT_RE.fullmatch(artifact_id) is None:
            return self._missing_target(item_id, artifact_id, required=required)
        capture_id = self._capture_id_for(item_id)
        if not capture_id:
            return self._missing_target(item_id, artifact_id, required=required)
        capture_id = _bounded_text(capture_id, field="capture_id")
        directory = Path(self._capture_directory_for(capture_id))
        self._assert_target(directory / "authority", self._capture_root)
        manifest_path = directory / _PHOTO_ASSETS_NAME
        manifest = self._read_json(manifest_path)
        if (
            manifest.get("schema") != _PHOTO_ASSETS_SCHEMA
            or manifest.get("version") != 1
            or manifest.get("capture_id") != capture_id
        ):
            raise _repository_error(
                "the capture photo asset contract is unsupported",
                code="unsupported_capture_photo_assets",
                item_id=item_id,
            )
        assets = manifest.get("assets")
        desktop = manifest.get("desktop_import")
        import_rows = desktop.get("assets") if isinstance(desktop, Mapping) else None
        if (
            not isinstance(assets, list)
            or not isinstance(import_rows, list)
            or len(assets) > 4096
            or len(import_rows) > 4096
        ):
            raise self._invalid_manifest(item_id)

        imports_by_id: dict[str, int] = {}
        for index, row in enumerate(import_rows):
            if not isinstance(row, dict):
                raise self._invalid_manifest(item_id)
            asset_id = _bounded_text(row.get("asset_id"), field="asset_id")
            if asset_id in imports_by_id:
                raise self._invalid_manifest(item_id)
            imports_by_id[asset_id] = index

        asset_ids: set[str] = set()
        capture_orders: set[int] = set()
        match: _CaptureAssetTarget | None = None
        for index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                raise self._invalid_manifest(item_id)
            asset_id = _bounded_text(asset.get("asset_id"), field="asset_id")
            order = asset.get("capture_order")
            if (
                asset_id in asset_ids
                or isinstance(order, bool)
                or not isinstance(order, int)
                or order <= 0
                or order in capture_orders
            ):
                raise self._invalid_manifest(item_id)
            asset_ids.add(asset_id)
            capture_orders.add(order)
            import_index = imports_by_id.get(asset_id)
            if import_index is None:
                raise self._invalid_manifest(item_id)
            namespace = _opaque_identity("capture", capture_id, asset_id)
            display_id = f"{namespace}:display"
            original_id = f"{namespace}:original"
            if artifact_id not in {display_id, original_id}:
                continue
            match = _CaptureAssetTarget(
                item_id=item_id,
                capture_id=capture_id,
                asset_id=asset_id,
                directory=directory,
                manifest_path=manifest_path,
                manifest=manifest,
                asset_index=index,
                import_index=import_index,
                capture_order=order,
                display_artifact_id=display_id,
                original_artifact_id=original_id,
            )
        if set(imports_by_id) != asset_ids:
            raise self._invalid_manifest(item_id)
        if match is None:
            return self._missing_target(item_id, artifact_id, required=required)
        return match

    @staticmethod
    def _missing_target(
        item_id: str,
        artifact_id: str,
        *,
        required: bool,
    ) -> None:
        if required:
            raise NotFoundError(
                "the capture asset does not exist",
                code="raster_artifact_not_found",
                details={"item_id": item_id, "artifact_id": artifact_id},
            )
        return None

    @staticmethod
    def _invalid_manifest(item_id: str) -> RepositoryError:
        return _repository_error(
            "the capture photo asset rows are invalid",
            code="invalid_capture_photo_assets",
            item_id=item_id,
        )

    def _asset_lifecycle(
        self,
        target: _CaptureAssetTarget,
    ) -> dict[str, Any] | None:
        if CAPTURE_ASSET_LIFECYCLE_FIELD not in target.asset:
            return None
        try:
            return _lifecycle(
                target.asset[CAPTURE_ASSET_LIFECYCLE_FIELD],
                field=CAPTURE_ASSET_LIFECYCLE_FIELD,
            )
        except ValueError as exc:
            raise _repository_error(
                "the capture asset lifecycle is invalid",
                code="invalid_capture_asset_lifecycle",
                item_id=target.item_id,
                asset_id=target.asset_id,
            ) from exc

    def _assert_persisted_delete_inverse(
        self,
        inverse: Mapping[str, Any],
    ) -> None:
        source_operation = inverse["source_operation_id"]
        receipt = self._read_receipt(
            self._receipt_path(source_operation),
            source_operation,
        )
        if receipt is None:
            raise NotFoundError(
                "the capture asset deletion receipt does not exist",
                code="capture_asset_delete_receipt_not_found",
                details={"operation_id": source_operation},
            )
        result = receipt["result"]
        if receipt["action"] != "delete" or result["inverse"] != inverse:
            raise ConflictError(
                "the capture asset restore inverse is foreign",
                code="capture_asset_restore_inverse_conflict",
                details={"operation_id": source_operation},
            )

    def _normalize_inverse(self, value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(value, Mapping) or frozenset(value) != _INVERSE_FIELDS:
                raise ValueError("inverse fields are invalid")
            if value.get("schema") != CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA:
                raise ValueError("inverse schema is invalid")
            action = value.get("action")
            if action not in {"delete", "restore"}:
                raise ValueError("inverse action is invalid")
            source_operation_id = self._operation_id(value.get("source_operation_id"))
            item_id = _bounded_text(value.get("item_id"), field="item_id", maximum=128)
            capture_id = _bounded_text(value.get("capture_id"), field="capture_id")
            asset_id = _bounded_text(value.get("asset_id"), field="asset_id")
            artifact_id = _bounded_text(
                value.get("artifact_id"), field="artifact_id", maximum=128
            )
            if _CAPTURE_ARTIFACT_RE.fullmatch(artifact_id) is None:
                raise ValueError("artifact_id is invalid")
            artifact_revision = _bounded_text(
                value.get("artifact_revision"),
                field="artifact_revision",
                maximum=128,
            )
            capture_order = value.get("capture_order")
            if (
                isinstance(capture_order, bool)
                or not isinstance(capture_order, int)
                or capture_order <= 0
            ):
                raise ValueError("capture_order is invalid")
            expected = _lifecycle(
                value.get("expected_lifecycle"),
                field="expected_lifecycle",
            )
        except ValueError as exc:
            raise ValueError("capture asset lifecycle inverse is invalid") from exc
        return {
            "schema": CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA,
            "action": action,
            "source_operation_id": source_operation_id,
            "item_id": item_id,
            "capture_id": capture_id,
            "asset_id": asset_id,
            "artifact_id": artifact_id,
            "artifact_revision": artifact_revision,
            "capture_order": capture_order,
            "expected_lifecycle": expected,
        }

    def _replay(
        self,
        receipt_path: Path,
        operation_id: str,
        command_sha: str,
    ) -> Mapping[str, Any] | None:
        receipt = self._read_receipt(receipt_path, operation_id)
        if receipt is None:
            return None
        if receipt["command_sha256"] != command_sha:
            raise ConflictError(
                "the capture asset lifecycle operation was reused",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        result = copy.deepcopy(receipt["result"])
        result["replayed"] = True
        return result

    def _read_receipt(
        self,
        path: Path,
        operation_id: str,
    ) -> dict[str, Any] | None:
        payload = self._read_optional_bytes(
            self._storage_root,
            path,
            maximum=_MAX_RECEIPT_BYTES,
        )
        if payload is None:
            return None
        try:
            receipt = json.loads(
                payload.decode("ascii"),
                object_pairs_hook=_unique_object,
            )
            self._validate_receipt(receipt, operation_id)
        except (UnicodeError, ValueError) as exc:
            raise _repository_error(
                "the capture asset lifecycle receipt is invalid",
                code="invalid_capture_asset_lifecycle_receipt",
                operation_id=operation_id,
            ) from exc
        return receipt

    def _validate_receipt(self, receipt: Any, operation_id: str) -> None:
        if (
            not isinstance(receipt, dict)
            or frozenset(receipt) != _RECEIPT_FIELDS
            or receipt.get("schema") != CAPTURE_ASSET_LIFECYCLE_RECEIPT_SCHEMA
            or receipt.get("version") != CAPTURE_ASSET_LIFECYCLE_RECEIPT_VERSION
            or isinstance(receipt.get("version"), bool)
            or receipt.get("operation_id") != operation_id
            or receipt.get("action") not in {"delete", "restore"}
            or not isinstance(receipt.get("command_sha256"), str)
            or _SHA256_RE.fullmatch(receipt["command_sha256"]) is None
        ):
            raise ValueError("receipt envelope is invalid")
        result = receipt.get("result")
        if not isinstance(result, dict) or frozenset(result) != _RESULT_FIELDS:
            raise ValueError("receipt result fields are invalid")
        action = receipt["action"]
        if (
            result.get("operation_id") != operation_id
            or result.get("action") != action
            or result.get("replayed") is not False
        ):
            raise ValueError("receipt result identity is invalid")
        item_id = _bounded_text(result.get("item_id"), field="item_id", maximum=128)
        capture_id = _bounded_text(result.get("capture_id"), field="capture_id")
        asset_id = _bounded_text(result.get("asset_id"), field="asset_id")
        artifact_id = _bounded_text(
            result.get("artifact_id"), field="artifact_id", maximum=128
        )
        if _CAPTURE_ARTIFACT_RE.fullmatch(artifact_id) is None:
            raise ValueError("receipt artifact is invalid")
        artifact_revision = _bounded_text(
            result.get("artifact_revision"),
            field="artifact_revision",
            maximum=128,
        )
        capture_order = result.get("capture_order")
        if (
            isinstance(capture_order, bool)
            or not isinstance(capture_order, int)
            or capture_order <= 0
        ):
            raise ValueError("receipt capture order is invalid")
        raw_before = result.get("before_lifecycle")
        before = (
            None
            if raw_before is None
            else _lifecycle(raw_before, field="before_lifecycle")
        )
        after = _lifecycle(result.get("after_lifecycle"), field="after_lifecycle")
        expected_revision = before["revision"] + 1 if before is not None else 1
        if (
            after["state"] != ("deleted" if action == "delete" else "active")
            or after["revision"] != expected_revision
            or (before is not None and after["updated_at"] <= before["updated_at"])
            or (
                action == "restore" and (before is None or before["state"] != "deleted")
            )
            or (
                action == "delete"
                and before is not None
                and before["state"] != "active"
            )
        ):
            raise ValueError("receipt lifecycle transition is invalid")
        _bounded_text(
            result.get("item_updated_at"),
            field="item_updated_at",
            maximum=2048,
        )
        inverse = self._normalize_inverse(result.get("inverse"))
        if (
            inverse["action"] != ("restore" if action == "delete" else "delete")
            or inverse["source_operation_id"] != operation_id
            or inverse["item_id"] != item_id
            or inverse["capture_id"] != capture_id
            or inverse["asset_id"] != asset_id
            or inverse["artifact_id"] != artifact_id
            or inverse["artifact_revision"] != artifact_revision
            or inverse["capture_order"] != capture_order
            or inverse["expected_lifecycle"] != after
        ):
            raise ValueError("receipt inverse is invalid")

    @staticmethod
    def _operation_id(value: Any) -> str:
        if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
            raise ValueError("operation_id is invalid")
        return value

    def _receipt_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        path = (
            self._storage_root
            / ".engine"
            / "receipts"
            / "capture-asset-lifecycle"
            / f"{digest}.json"
        )
        self._assert_target(path, self._storage_root)
        return path

    def _read_json(self, path: Path) -> dict[str, Any]:
        payload = self._read_optional_bytes(
            self._capture_root,
            path,
            maximum=_MAX_MANIFEST_BYTES,
        )
        if payload is None:
            raise NotFoundError(
                "the capture photo asset manifest is missing",
                code="capture_photo_assets_not_found",
            )
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
        except (UnicodeError, ValueError) as exc:
            raise _repository_error(
                "the capture photo asset manifest is invalid",
                code="invalid_capture_photo_assets",
            ) from exc
        if not isinstance(value, dict):
            raise _repository_error(
                "the capture photo asset manifest is invalid",
                code="invalid_capture_photo_assets",
            )
        return value

    def _read_optional_bytes(
        self,
        root: Path,
        path: Path,
        *,
        maximum: int,
    ) -> bytes | None:
        self._assert_target(path, root)
        try:
            named = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _repository_error(
                "a capture asset lifecycle authority path cannot be inspected",
                code="capture_asset_lifecycle_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or named.st_size < 1
            or named.st_size > maximum
        ):
            raise _repository_error(
                "a capture asset lifecycle authority target is unsafe",
                code="unsafe_capture_asset_lifecycle",
            )
        authority = self._authority_snapshot(root, path)
        descriptor = -1
        try:
            descriptor, opened = _open_verified_regular(
                path, named, authority=authority
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
                        "capture asset lifecycle file exceeds its size limit"
                    )
                chunks.append(block)
            _finish_verified_regular(
                path,
                descriptor,
                named_before=named,
                opened_before=opened,
            )
            return b"".join(chunks)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "the capture asset lifecycle authority changed during read",
                code="capture_asset_lifecycle_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _authority_snapshot(self, root: Path, path: Path) -> _AuthoritySnapshot:
        self._assert_target(path, root)
        try:
            named_root = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise _repository_error(
                "the capture asset lifecycle authority root is unavailable",
                code="capture_asset_lifecycle_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting_path(root) or not stat.S_ISDIR(named_root.st_mode):
            raise _repository_error(
                "the capture asset lifecycle authority root is unsafe",
                code="unsafe_capture_asset_lifecycle",
            )
        directories: list[_AuthorityDirectorySnapshot] = []
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a capture asset lifecycle path crosses a redirecting directory",
                    code="unsafe_capture_asset_lifecycle",
                )
            try:
                named = current.lstat()
            except FileNotFoundError:
                named = None
            if named is not None and not stat.S_ISDIR(named.st_mode):
                raise _repository_error(
                    "a capture asset lifecycle path component is not a directory",
                    code="unsafe_capture_asset_lifecycle",
                )
            directories.append(_AuthorityDirectorySnapshot(current, named))
        try:
            path.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a capture asset lifecycle path escapes its authority",
                code="unsafe_capture_asset_lifecycle",
            ) from exc
        return _AuthoritySnapshot(root, named_root, tuple(directories))

    def _managed_root(self, value: Path, name: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"{name} must be absolute")
        try:
            path.relative_to(self._write_set.root)
        except ValueError as exc:
            raise ValueError(f"{name} must be below the write-set root") from exc
        self._assert_target(path, self._write_set.root)
        return path

    @staticmethod
    def _assert_target(path: Path, root: Path) -> None:
        try:
            relative = Path(path).relative_to(root)
        except ValueError as exc:
            raise ValueError(
                "capture asset lifecycle target escapes its authority"
            ) from exc
        current = root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise ValueError("capture asset lifecycle target is unsafe")
            current /= part
            if _is_redirecting_path(current):
                raise ValueError(
                    "capture asset lifecycle target crosses a redirecting path"
                )

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._write_set.root).as_posix()


__all__ = [
    "CAPTURE_ASSET_LIFECYCLE_FIELD",
    "CAPTURE_ASSET_LIFECYCLE_INVERSE_SCHEMA",
    "CAPTURE_ASSET_LIFECYCLE_RECEIPT_SCHEMA",
    "CAPTURE_ASSET_LIFECYCLE_RECEIPT_VERSION",
    "FilesystemCaptureAssetLifecycleStore",
]
