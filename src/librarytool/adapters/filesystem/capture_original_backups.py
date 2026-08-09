"""Cold, content-addressed storage for corrected capture originals.

The desktop import row remains the owner of local filenames.  Once a capture
display is corrected, its verified ``raw_ref`` bytes move to a SHA-256 object
outside the capture tree and the row records only an opaque, versioned marker.
Normal raster projection consumes that marker as metadata; only the explicit
view and restore methods in this adapter open the backup object.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager, TypeAlias

from PIL import Image, UnidentifiedImageError

from ...engine.correction_transforms import (
    CorrectionTransformCommitDraft,
    CorrectionTransformCommitResult,
)
from ...engine.errors import ConflictError, NotFoundError, RepositoryError
from ...engine.raster_artifacts import RasterArtifactKey, RasterArtifactView
from .correction_transform_store import (
    CorrectionTransformPublicationPlan,
    correction_display_head_path,
)
from .corrections_artifact_repository import (
    ResolvedRasterResource,
    _AuthorityDirectorySnapshot,
    _AuthoritySnapshot,
    _finish_verified_regular,
    _open_verified_regular,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    _is_redirecting_path,
)


ORIGINAL_BACKUP_VERSION = 1
ORIGINAL_BACKUP_STORE = "output-originals-sha256"
ORIGINAL_BACKUP_KEY_PREFIX = "sha256:"
ORIGINAL_BACKUP_MARKER_FIELDS = frozenset(
    {"version", "store", "key", "sha256", "bytes", "media_type"}
)
ORIGINAL_RESTORE_RECEIPT_SCHEMA = "librarytool.original-backup-restore-receipt"
ORIGINAL_RESTORE_RECEIPT_VERSION = 1

_PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
_PHOTO_ASSETS_NAME = "photo_assets.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CAPTURE_ARTIFACT_RE = re.compile(
    r"^capture:[0-9a-f]{40}:(?:display|original)$"
)
_LEAF_RE = re.compile(r"^(?!\.+$)[\w.\-]{1,255}$")
_MEDIA_BY_FORMAT = {
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}
_MAX_IMAGE_BYTES = 100 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024

CaptureIdentityLookup: TypeAlias = Callable[[str], str | None]
CaptureDirectoryLookup: TypeAlias = Callable[[str], Path]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]
ArtifactLookup: TypeAlias = Callable[[RasterArtifactKey], RasterArtifactView | None]
ArtifactPublicationRevision: TypeAlias = Callable[
    [str, str, str, Mapping[str, Any], bytes], str
]
ItemUpdatedAtPublication: TypeAlias = Callable[[str], tuple[Path, bytes, str]]


@dataclass(frozen=True, slots=True)
class OriginalBackupDescriptor:
    sha256: str
    size: int
    media_type: str

    @property
    def key(self) -> str:
        return ORIGINAL_BACKUP_KEY_PREFIX + self.sha256

    def as_marker(self) -> dict[str, Any]:
        return {
            "version": ORIGINAL_BACKUP_VERSION,
            "store": ORIGINAL_BACKUP_STORE,
            "key": self.key,
            "sha256": self.sha256,
            "bytes": self.size,
            "media_type": self.media_type,
        }

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "sha256": self.sha256,
            "bytes": self.size,
            "media_type": self.media_type,
        }


def parse_original_backup_marker(
    imported: Mapping[str, Any],
    original: Mapping[str, Any],
) -> OriginalBackupDescriptor | None:
    """Return one strict desktop backup marker, or reject malformed state.

    ``raw_ref`` and ``original_backup`` are mutually exclusive.  The marker's
    digest must agree with both desktop and portable source anchors whenever
    those anchors are present.  Its ``key`` is opaque metadata, never a path.
    """

    if "original_backup" not in imported:
        return None
    marker = imported.get("original_backup")
    if not isinstance(marker, Mapping) or frozenset(marker) != (
        ORIGINAL_BACKUP_MARKER_FIELDS
    ):
        raise ValueError("original_backup fields are invalid")
    sha256 = marker.get("sha256")
    size = marker.get("bytes")
    media_type = marker.get("media_type")
    if (
        marker.get("version") != ORIGINAL_BACKUP_VERSION
        or isinstance(marker.get("version"), bool)
        or marker.get("store") != ORIGINAL_BACKUP_STORE
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or marker.get("key") != ORIGINAL_BACKUP_KEY_PREFIX + sha256
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size > _MAX_IMAGE_BYTES
        or media_type != "image/jpeg"
        or "raw_ref" in imported
    ):
        raise ValueError("original_backup is invalid")
    for declared in (
        imported.get("source_checksum"),
        original.get("sha256"),
    ):
        if declared not in (None, "") and (
            not isinstance(declared, str)
            or declared.casefold() != sha256
        ):
            raise ValueError("original_backup digest disagrees with source")
    if "active_desktop_correction_id" in imported:
        active = imported.get("active_desktop_correction_id")
        if (
            not isinstance(active, str)
            or _IDENTIFIER_RE.fullmatch(active) is None
        ):
            raise ValueError("active desktop correction identity is invalid")
    return OriginalBackupDescriptor(sha256, size, media_type)


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


def _public_text(value: Any, *, maximum: int) -> str:
    """Mirror the capture projector's bounded public-text normalization."""

    if not isinstance(value, str):
        return ""
    return "".join(
        character
        for character in value[:maximum]
        if not (
            ord(character) == 127
            or (ord(character) < 32 and character not in "\n\r\t")
            or 0xD800 <= ord(character) <= 0xDFFF
        )
    )


def _capture_region_annotation_id(
    capture_id: str,
    asset_id: str,
    geometry: Mapping[str, Any],
    region: Mapping[str, Any],
) -> str | None:
    """Return the stable public identity used by capture projection."""

    region_id = _public_text(region.get("id"), maximum=120).strip()
    if not region_id:
        return None
    payload = {
        "capture_id": capture_id,
        "asset_id": asset_id,
        "coordinate_space": "display_normalized",
        "engine": _public_text(geometry.get("engine"), maximum=80),
        "model": _public_text(geometry.get("model"), maximum=120),
        "region_id": region_id,
    }
    # _canonical_json appends the newline used for files; public projection
    # hashes the same canonical payload without that record terminator.
    digest = hashlib.sha256(_canonical_json(payload)[:-1]).hexdigest()
    return f"capture-region:{digest[:40]}"


def _normalized_sha256(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.casefold()
    return normalized if _SHA256_RE.fullmatch(normalized) else ""


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
    """Return Android's monotonic epoch-millisecond lifecycle timestamp."""

    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    if (
        isinstance(previous, int)
        and not isinstance(previous, bool)
        and previous >= now
    ):
        now = previous + 1
    return now


def _repository_error(message: str, *, code: str, **details: Any) -> RepositoryError:
    return RepositoryError(message, code=code, details=details)


@dataclass(frozen=True, slots=True)
class _CaptureTarget:
    item_id: str
    capture_id: str
    directory: Path
    manifest_path: Path
    manifest: dict[str, Any]
    asset_index: int
    import_index: int
    display_artifact_id: str

    @property
    def asset(self) -> dict[str, Any]:
        return self.manifest["assets"][self.asset_index]

    @property
    def imported(self) -> dict[str, Any]:
        return self.manifest["desktop_import"]["assets"][self.import_index]


class FilesystemCaptureOriginalBackupStore:
    """Plan atomic backup promotion and serve explicit cold-store actions."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        coordination_write_set: RecoverableWriteSet,
        storage_root: Path,
        capture_authority_root: Path,
        backup_root: Path,
        capture_id_for: CaptureIdentityLookup,
        capture_directory_for: CaptureDirectoryLookup,
        artifact_for: ArtifactLookup,
        artifact_revision_for_publication: ArtifactPublicationRevision,
        lock_context_for: LockContextFactory,
        item_updated_at_publication_for: ItemUpdatedAtPublication | None = None,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        if not isinstance(coordination_write_set, RecoverableWriteSet):
            raise TypeError("coordination_write_set must be a RecoverableWriteSet")
        for callback, name in (
            (capture_id_for, "capture_id_for"),
            (capture_directory_for, "capture_directory_for"),
            (artifact_for, "artifact_for"),
            (
                artifact_revision_for_publication,
                "artifact_revision_for_publication",
            ),
            (lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if (
            item_updated_at_publication_for is not None
            and not callable(item_updated_at_publication_for)
        ):
            raise TypeError(
                "item_updated_at_publication_for must be callable or None"
            )
        self._write_set = write_set
        self._coordination_write_set = coordination_write_set
        self._storage_root = self._managed_root(storage_root, "storage_root")
        self._capture_root = self._managed_root(
            capture_authority_root,
            "capture_authority_root",
        )
        self._backup_root = self._managed_root(backup_root, "backup_root")
        self._capture_id_for = capture_id_for
        self._capture_directory_for = capture_directory_for
        self._artifact_for = artifact_for
        self._artifact_revision_for_publication = (
            artifact_revision_for_publication
        )
        self._lock_context_for = lock_context_for
        self._item_updated_at_publication_for = (
            item_updated_at_publication_for
        )

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

    def plan_transform_publication(
        self,
        draft: CorrectionTransformCommitDraft,
        result: CorrectionTransformCommitResult,
        logical_display_artifact_id: str | None,
    ) -> CorrectionTransformPublicationPlan | None:
        """Join original backup metadata to one transform transaction.

        Normal display transforms leave the stable base rendition and its
        geometry untouched; the transform store publishes their corrected
        logical display through its mutable head. A transform explicitly
        started from the separately exposed original retains the legacy
        physical-promotion fallback because it has no logical display head.
        """

        if not isinstance(draft, CorrectionTransformCommitDraft):
            raise TypeError("draft must be a CorrectionTransformCommitDraft")
        if not isinstance(result, CorrectionTransformCommitResult):
            raise TypeError("result must be a CorrectionTransformCommitResult")
        if logical_display_artifact_id is not None and not isinstance(
            logical_display_artifact_id,
            str,
        ):
            raise TypeError("logical_display_artifact_id must be a string or None")
        target = self._target_for_artifact(
            draft.command.item_id,
            logical_display_artifact_id or draft.command.artifact_id,
            required=False,
        )
        if target is None:
            return None
        corrected = draft.output("corrected-display")
        if corrected.media_type != "image/png":
            raise _repository_error(
                "the corrected display media type is unsupported",
                code="invalid_capture_display_promotion",
                item_id=target.item_id,
            )
        asset = target.asset
        imported = target.imported
        original = asset.get("original")
        display = asset.get("display")
        if not isinstance(original, dict) or not isinstance(display, dict):
            raise _repository_error(
                "the capture renditions cannot be promoted",
                code="invalid_capture_photo_assets",
                item_id=target.item_id,
            )

        prefix: list[tuple[Path, bytes]] = []
        deletes: set[Path] = set()
        try:
            backup = parse_original_backup_marker(imported, original)
        except ValueError as exc:
            raise _repository_error(
                "the capture original backup marker is invalid",
                code="invalid_capture_original_backup",
                item_id=target.item_id,
            ) from exc

        old_display_ref = imported.get("display_ref") or display.get("reference")
        old_display_path = self._capture_leaf(target, old_display_ref, "display_ref")
        if backup is None:
            original_ref = imported.get("raw_ref") or original.get("reference")
            original_path = self._capture_leaf(target, original_ref, "raw_ref")
            original_bytes, media_type = self._read_image(
                self._capture_root,
                original_path,
                maximum=_MAX_IMAGE_BYTES,
            )
            if media_type != "image/jpeg":
                raise _repository_error(
                    "the capture original media type is unsupported",
                    code="invalid_capture_original_backup",
                    item_id=target.item_id,
                )
            original_sha = hashlib.sha256(original_bytes).hexdigest()
            declared = tuple(
                value
                for value in (
                    imported.get("source_checksum"),
                    original.get("sha256"),
                )
                if value not in (None, "")
            )
            if not declared or any(
                not isinstance(value, str)
                or _SHA256_RE.fullmatch(value.casefold()) is None
                or original_sha != value.casefold()
                for value in declared
            ):
                raise ConflictError(
                    "the capture original changed before backup",
                    code="capture_original_sha256_conflict",
                    details={"item_id": target.item_id},
                )
            backup = OriginalBackupDescriptor(
                original_sha,
                len(original_bytes),
                media_type,
            )
            backup_path = self._backup_path(backup.sha256)
            existing = self._read_optional_image(
                self._backup_root,
                backup_path,
                maximum=_MAX_IMAGE_BYTES,
            )
            if existing is None:
                prefix.append((backup_path, original_bytes))
            else:
                existing_bytes, existing_media_type = existing
                if (
                    hashlib.sha256(existing_bytes).hexdigest() != backup.sha256
                    or len(existing_bytes) != backup.size
                    or existing_media_type != backup.media_type
                ):
                    raise ConflictError(
                        "the content-addressed original backup is corrupt",
                        code="capture_original_backup_conflict",
                        details={"item_id": target.item_id},
                    )
            imported.pop("raw_ref", None)
            imported["original_backup"] = backup.as_marker()
            if original_path != old_display_path:
                deletes.add(original_path)

        display_writes: tuple[tuple[Path, bytes], ...] = ()
        if logical_display_artifact_id is None:
            corrected_display = self._jpeg_display(corrected.content)
            corrected_display_sha256 = hashlib.sha256(
                corrected_display
            ).hexdigest()
            order = asset.get("capture_order")
            if isinstance(order, bool) or not isinstance(order, int) or order <= 0:
                raise _repository_error(
                    "the capture order is invalid",
                    code="invalid_capture_photo_assets",
                    item_id=target.item_id,
                )
            # The original-source fallback cannot be represented by a #301
            # logical display head. Promote its deterministic JPEG to the
            # stable capture slot and remove any older head in the same write
            # set so it cannot continue shadowing the new base display.
            new_display_path = target.directory / f"photo_{order}.jpg"
            self._assert_target(new_display_path, self._capture_root)
            if old_display_path != new_display_path:
                deletes.add(old_display_path)
            deletes.discard(new_display_path)
            display_head_path = correction_display_head_path(
                self._storage_root,
                target.item_id,
                target.display_artifact_id,
            )
            self._assert_target(display_head_path, self._storage_root)
            deletes.add(display_head_path)

            display_revision = display.get("revision")
            next_revision = (
                display_revision + 1
                if isinstance(display_revision, int)
                and not isinstance(display_revision, bool)
                and display_revision > 0
                else 1
            )
            declared_display_sha256 = _normalized_sha256(
                imported.get("derivative_checksum") or display.get("sha256")
            )
            old_display_bytes, _old_display_media_type = self._read_image(
                self._capture_root,
                old_display_path,
                maximum=_MAX_IMAGE_BYTES,
            )
            current_display_sha256 = hashlib.sha256(
                old_display_bytes
            ).hexdigest()
            if (
                declared_display_sha256
                and declared_display_sha256 != current_display_sha256
            ):
                raise ConflictError(
                    "the capture display changed before promotion",
                    code="capture_display_sha256_conflict",
                    details={"item_id": target.item_id},
                )
            display.update(
                {
                    "reference": new_display_path.name,
                    "sha256": corrected_display_sha256,
                    "revision": next_revision,
                    "width": corrected.dimensions.width,
                    "height": corrected.dimensions.height,
                    "orientation": 0,
                    "recipe": "desktop-correction-transform",
                    "recipe_version": "1",
                }
            )
            display.pop("source_to_display_homography", None)
            asset["capture_file"] = new_display_path.name
            asset["geometry"] = self._promoted_geometry(
                target,
                draft,
                original=original,
                current_display_sha256=current_display_sha256,
                promoted_display_sha256=corrected_display_sha256,
                display_revision=next_revision,
                display_width=corrected.dimensions.width,
                display_height=corrected.dimensions.height,
            )
            imported.update(
                {
                    "display_ref": new_display_path.name,
                    "derivative_checksum": corrected_display_sha256,
                    "recipe": "desktop-correction-transform-v1",
                    "lifecycle": "completed",
                }
            )
            display_writes = ((new_display_path, corrected_display),)
        lifecycle = asset.get("lifecycle")
        lifecycle = dict(lifecycle) if isinstance(lifecycle, Mapping) else {}
        lifecycle["state"] = "completed"
        lifecycle["updated_at"] = _next_timestamp(lifecycle.get("updated_at"))
        asset["lifecycle"] = lifecycle
        imported["active_desktop_correction_id"] = result.operation_id

        item_writes: tuple[tuple[Path, bytes], ...] = ()
        if self._item_updated_at_publication_for is not None:
            item_path, item_payload, _item_updated_at = (
                self._item_updated_at_publication_for(target.item_id)
            )
            item_path = Path(item_path)
            self._assert_target(item_path, self._write_set.root)
            if not isinstance(item_payload, bytes):
                raise TypeError("item updated_at payload must be bytes")
            item_writes = ((item_path, item_payload),)
        return CorrectionTransformPublicationPlan(
            prefix_writes=tuple(prefix),
            writes=(*display_writes, *item_writes),
            deletes=tuple(sorted(deletes, key=str)),
            final_writes=((target.manifest_path, _canonical_json(target.manifest)),),
        )

    def _promoted_geometry(
        self,
        target: _CaptureTarget,
        draft: CorrectionTransformCommitDraft,
        *,
        original: Mapping[str, Any],
        current_display_sha256: str,
        promoted_display_sha256: str,
        display_revision: int,
        display_width: int,
        display_height: int,
    ) -> list[dict[str, Any]]:
        """Pin surviving capture regions to the promoted stable display.

        The transform service has already clipped and mapped every live source
        annotation.  Capture-region public identities intentionally exclude
        polygons, so retaining the provider record identity while replacing
        only its points keeps durable human role/caption overlays attached.
        """

        raw_geometry = target.asset.get("geometry")
        if not isinstance(raw_geometry, list) or not draft.mapped_annotations:
            return []
        asset_id = target.asset.get("asset_id")
        original_revision = original.get("revision")
        original_sha256 = original.get("sha256")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or isinstance(original_revision, bool)
            or not isinstance(original_revision, int)
            or original_revision <= 0
            or not isinstance(original_sha256, str)
            or _SHA256_RE.fullmatch(original_sha256.casefold()) is None
        ):
            raise _repository_error(
                "the capture geometry source identity is invalid",
                code="invalid_capture_photo_assets",
                item_id=target.item_id,
            )

        mapped = {
            value.annotation_id: value
            for value in draft.mapped_annotations
        }
        consumed: set[str] = set()
        candidates = [
            value for value in raw_geometry if isinstance(value, Mapping)
        ]
        candidates.sort(
            key=lambda value: (
                _public_text(value.get("engine"), maximum=80),
                _public_text(value.get("model"), maximum=120),
            )
        )
        # Match projection precedence: a record pinned to the current display
        # owns an identity before an otherwise equivalent phone-frame record.
        candidates.sort(
            key=lambda value: (
                0
                if _normalized_sha256(value.get("display_sha256"))
                == current_display_sha256
                else 1
                if not _normalized_sha256(value.get("display_sha256"))
                else 2
            )
        )

        promoted: list[dict[str, Any]] = []
        for geometry in candidates:
            pinned = _normalized_sha256(geometry.get("display_sha256"))
            if pinned and pinned != current_display_sha256:
                continue
            regions = geometry.get("regions")
            if not isinstance(regions, list):
                continue
            survivors: list[tuple[int, dict[str, Any]]] = []
            for region in regions:
                if not isinstance(region, Mapping):
                    continue
                annotation_id = _capture_region_annotation_id(
                    target.capture_id,
                    asset_id,
                    geometry,
                    region,
                )
                mapped_annotation = (
                    mapped.get(annotation_id) if annotation_id else None
                )
                if mapped_annotation is None or annotation_id in consumed:
                    continue
                survivor = dict(region)
                survivor["polygon"] = [
                    [point.x, point.y]
                    for point in mapped_annotation.points
                ]
                survivors.append((mapped_annotation.order, survivor))
                consumed.add(annotation_id)
            if not survivors:
                continue
            survivors.sort(key=lambda value: value[0])
            record = dict(geometry)
            record.update(
                {
                    "asset_id": asset_id,
                    "source_sha256": original_sha256.casefold(),
                    "source_revision": original_revision,
                    "display_revision": display_revision,
                    "coordinate_space": "display_normalized",
                    "width": display_width,
                    "height": display_height,
                    "orientation": 0,
                    "display_sha256": promoted_display_sha256,
                    "remap_recipe": "desktop-geometry-remap-v1",
                    "regions": [value for _order, value in survivors],
                }
            )
            promoted.append(record)
        return promoted

    def resolve_original_backup(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
    ) -> ResolvedRasterResource | None:
        with self._coordination_write_set.workspace_lease():
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    self._assert_artifact_revision(
                        item_id,
                        artifact_id,
                        expected_revision,
                    )
                    target = self._target_for_artifact(
                        item_id,
                        artifact_id,
                        required=True,
                    )
                    assert target is not None
                    original = target.asset.get("original")
                    if not isinstance(original, Mapping):
                        return None
                    try:
                        backup = parse_original_backup_marker(
                            target.imported,
                            original,
                        )
                    except ValueError as exc:
                        raise _repository_error(
                            "the capture original backup marker is invalid",
                            code="invalid_capture_original_backup",
                            item_id=item_id,
                        ) from exc
                    if backup is None:
                        return None
                    content, media_type = self._read_image(
                        self._backup_root,
                        self._backup_path(backup.sha256),
                        maximum=_MAX_IMAGE_BYTES,
                    )
                    if (
                        hashlib.sha256(content).hexdigest() != backup.sha256
                        or len(content) != backup.size
                        or media_type != backup.media_type
                    ):
                        raise ConflictError(
                            "the capture original backup failed verification",
                            code="capture_original_backup_corrupt",
                            details={"item_id": item_id},
                        )
                    stream = tempfile.TemporaryFile(mode="w+b")
                    stream.write(content)
                    stream.seek(0)
                    return ResolvedRasterResource(
                        stream=stream,
                        media_type=media_type,
                        content_sha256=backup.sha256,
                        size=backup.size,
                        # The HTTP resource grant is pinned to the stable
                        # display revision supplied by the caller. Integrity
                        # remains independently exposed by content_sha256.
                        revision=expected_revision,
                    )

    def restore_original_backup(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
        operation_id: str,
    ) -> Mapping[str, Any]:
        if not isinstance(operation_id, str) or not _IDENTIFIER_RE.fullmatch(
            operation_id
        ):
            raise ValueError("operation_id is invalid")
        command_sha = hashlib.sha256(
            f"{item_id}\0{artifact_id}\0{expected_revision}".encode("utf-8")
        ).hexdigest()
        receipt_path = self._restore_receipt_path(operation_id)
        with self._coordination_write_set.workspace_lease():
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    replay = self._read_restore_receipt(
                        receipt_path,
                        operation_id,
                    )
                    if replay is not None:
                        if replay.get("command_sha256") != command_sha:
                            raise ConflictError(
                                "the restore operation was reused",
                                code="operation_id_conflict",
                                details={"operation_id": operation_id},
                            )
                        result = dict(replay["result"])
                        result["replayed"] = True
                        return result

                    self._assert_artifact_revision(
                        item_id,
                        artifact_id,
                        expected_revision,
                    )
                    target = self._target_for_artifact(
                        item_id,
                        artifact_id,
                        required=True,
                    )
                    assert target is not None
                    asset = target.asset
                    original = asset.get("original")
                    display = asset.get("display")
                    if not isinstance(original, dict) or not isinstance(display, dict):
                        raise _repository_error(
                            "the capture renditions cannot be restored",
                            code="invalid_capture_photo_assets",
                            item_id=item_id,
                        )
                    try:
                        backup = parse_original_backup_marker(
                            target.imported,
                            original,
                        )
                    except ValueError as exc:
                        raise _repository_error(
                            "the capture original backup marker is invalid",
                            code="invalid_capture_original_backup",
                            item_id=item_id,
                        ) from exc
                    if backup is None:
                        raise NotFoundError(
                            "the capture has no original backup",
                            code="capture_original_backup_not_found",
                            details={"item_id": item_id},
                        )
                    if "active_desktop_correction_id" not in target.imported:
                        raise ConflictError(
                            "the capture display is already restored",
                            code="capture_display_already_original",
                            details={"item_id": item_id},
                        )
                    content, media_type = self._read_image(
                        self._backup_root,
                        self._backup_path(backup.sha256),
                        maximum=_MAX_IMAGE_BYTES,
                    )
                    if (
                        hashlib.sha256(content).hexdigest() != backup.sha256
                        or len(content) != backup.size
                        or media_type != backup.media_type
                    ):
                        raise ConflictError(
                            "the capture original backup failed verification",
                            code="capture_original_backup_corrupt",
                            details={"item_id": item_id},
                        )

                    order = asset.get("capture_order")
                    if isinstance(order, bool) or not isinstance(order, int) or order <= 0:
                        raise _repository_error(
                            "the capture order is invalid",
                            code="invalid_capture_photo_assets",
                            item_id=item_id,
                        )
                    restored_path = target.directory / f"photo_{order}.jpg"
                    self._assert_target(restored_path, self._capture_root)
                    display_head_path = correction_display_head_path(
                        self._storage_root,
                        item_id,
                        target.display_artifact_id,
                    )
                    self._assert_target(display_head_path, self._storage_root)
                    old_display = self._capture_leaf(
                        target,
                        target.imported.get("display_ref")
                        or display.get("reference"),
                        "display_ref",
                    )
                    display_revision = display.get("revision")
                    next_revision = (
                        display_revision + 1
                        if isinstance(display_revision, int)
                        and not isinstance(display_revision, bool)
                        and display_revision > 0
                        else 1
                    )
                    width = original.get("width")
                    height = original.get("height")
                    orientation = original.get("orientation")
                    restored_display = (
                        content
                        if media_type == "image/jpeg"
                        else self._jpeg_display(content)
                    )
                    restored_display_sha256 = hashlib.sha256(
                        restored_display
                    ).hexdigest()
                    display.update(
                        {
                            "reference": restored_path.name,
                            "sha256": restored_display_sha256,
                            "revision": next_revision,
                            "width": width if isinstance(width, int) and width > 0 else 1,
                            "height": height if isinstance(height, int) and height > 0 else 1,
                            "orientation": (
                                orientation
                                if isinstance(orientation, int)
                                and not isinstance(orientation, bool)
                                and orientation in {0, 90, 180, 270}
                                else 0
                            ),
                            "recipe": "camera-original",
                            "recipe_version": "1",
                        }
                    )
                    display.pop("source_to_display_homography", None)
                    asset["capture_file"] = restored_path.name
                    asset["geometry"] = []
                    lifecycle = asset.get("lifecycle")
                    lifecycle = dict(lifecycle) if isinstance(lifecycle, Mapping) else {}
                    lifecycle["state"] = "completed"
                    lifecycle["updated_at"] = _next_timestamp(
                        lifecycle.get("updated_at")
                    )
                    asset["lifecycle"] = lifecycle
                    target.imported.update(
                        {
                            "display_ref": restored_path.name,
                            "derivative_checksum": restored_display_sha256,
                            "recipe": "camera-original",
                            "lifecycle": "completed",
                        }
                    )
                    target.imported.pop("active_desktop_correction_id", None)

                    after_revision = self._artifact_revision_for_publication(
                        item_id,
                        target.capture_id,
                        target.display_artifact_id,
                        target.manifest,
                        restored_display,
                    )
                    if (
                        not isinstance(after_revision, str)
                        or _IDENTIFIER_RE.fullmatch(after_revision) is None
                        or after_revision == expected_revision
                    ):
                        raise _repository_error(
                            "the restored capture display revision is invalid",
                            code="invalid_capture_original_restore_revision",
                            item_id=item_id,
                        )
                    result = {
                        "operation_id": operation_id,
                        "item_id": item_id,
                        "artifact_id": artifact_id,
                        "before_revision": expected_revision,
                        "after_revision": after_revision,
                        "backup_sha256": backup.sha256,
                        "replayed": False,
                    }
                    receipt = {
                        "schema": ORIGINAL_RESTORE_RECEIPT_SCHEMA,
                        "version": ORIGINAL_RESTORE_RECEIPT_VERSION,
                        "operation_id": operation_id,
                        "command_sha256": command_sha,
                        "result": result,
                    }
                    transaction = self._write_set.begin(
                        operation_id=operation_id,
                        scope="capture-original-restore",
                        metadata={"item_id": item_id, "artifact_id": artifact_id},
                    )
                    transaction.stage_write(
                        self._relative(restored_path),
                        restored_display,
                    )
                    if old_display != restored_path:
                        transaction.stage_delete(self._relative(old_display))
                    transaction.stage_delete(self._relative(display_head_path))
                    if self._item_updated_at_publication_for is not None:
                        item_path, item_payload, _item_updated_at = (
                            self._item_updated_at_publication_for(item_id)
                        )
                        item_path = Path(item_path)
                        self._assert_target(item_path, self._write_set.root)
                        if not isinstance(item_payload, bytes):
                            raise TypeError("item updated_at payload must be bytes")
                        transaction.stage_write(
                            self._relative(item_path),
                            item_payload,
                        )
                    transaction.stage_write(
                        self._relative(target.manifest_path),
                        _canonical_json(target.manifest),
                    )
                    # The receipt is the terminal publication boundary: replay
                    # cannot become visible before the stable display, head
                    # removal, item revision, and manifest are recoverable.
                    transaction.stage_write(
                        self._relative(receipt_path),
                        _canonical_json(receipt),
                    )
                    transaction.commit(receipt=result)
                    return result

    def _assert_artifact_revision(
        self,
        item_id: str,
        artifact_id: str,
        expected_revision: str,
    ) -> None:
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("expected_revision is required")
        key = RasterArtifactKey(item_id, artifact_id)
        artifact = self._artifact_for(key)
        if artifact is None:
            raise NotFoundError(
                "the capture display does not exist",
                code="raster_artifact_not_found",
                details=key.as_dict(),
            )
        if artifact.revision != expected_revision:
            raise ConflictError(
                "the capture display revision changed",
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
    ) -> _CaptureTarget | None:
        if _CAPTURE_ARTIFACT_RE.fullmatch(artifact_id) is None:
            if required:
                raise NotFoundError(
                    "the capture display does not exist",
                    code="raster_artifact_not_found",
                    details={"item_id": item_id, "artifact_id": artifact_id},
                )
            return None
        capture_id = self._capture_id_for(item_id)
        if not capture_id:
            if required:
                raise NotFoundError(
                    "the item has no capture",
                    code="capture_original_backup_not_found",
                    details={"item_id": item_id},
                )
            return None
        directory = Path(self._capture_directory_for(capture_id))
        self._assert_target(directory / "authority", self._capture_root)
        manifest_path = directory / _PHOTO_ASSETS_NAME
        try:
            manifest = self._read_json(manifest_path)
        except NotFoundError:
            if not required:
                return None
            raise
        if (
            manifest.get("schema") != _PHOTO_ASSETS_SCHEMA
            or manifest.get("version") != 1
            or manifest.get("capture_id") != capture_id
        ):
            if not required:
                return None
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
            if not required:
                return None
            raise _repository_error(
                "the capture photo asset rows are invalid",
                code="invalid_capture_photo_assets",
                item_id=item_id,
            )
        imports_by_id: dict[str, int] = {}
        for index, row in enumerate(import_rows):
            if isinstance(row, dict) and isinstance(row.get("asset_id"), str):
                imports_by_id.setdefault(row["asset_id"], index)
        for index, asset in enumerate(assets):
            if not isinstance(asset, dict):
                continue
            asset_id = asset.get("asset_id")
            if not isinstance(asset_id, str):
                continue
            namespace = _opaque_identity("capture", capture_id, asset_id)
            display_id = f"{namespace}:display"
            original_id = f"{namespace}:original"
            if artifact_id not in {display_id, original_id}:
                continue
            import_index = imports_by_id.get(asset_id)
            if import_index is None:
                raise _repository_error(
                    "the capture display has no desktop import row",
                    code="invalid_capture_photo_assets",
                    item_id=item_id,
                )
            return _CaptureTarget(
                item_id,
                capture_id,
                directory,
                manifest_path,
                manifest,
                index,
                import_index,
                display_id,
            )
        if required:
            raise NotFoundError(
                "the capture display does not exist",
                code="raster_artifact_not_found",
                details={"item_id": item_id, "artifact_id": artifact_id},
            )
        return None

    def _capture_leaf(
        self,
        target: _CaptureTarget,
        reference: Any,
        field: str,
    ) -> Path:
        if (
            not isinstance(reference, str)
            or _LEAF_RE.fullmatch(reference) is None
            or "/" in reference
            or "\\" in reference
            or reference in {".", ".."}
        ):
            raise _repository_error(
                "the capture rendition reference is invalid",
                code="invalid_capture_photo_assets",
                item_id=target.item_id,
                field=field,
            )
        path = target.directory / reference
        self._assert_target(path, self._capture_root)
        return path

    def _backup_path(self, sha256: str) -> Path:
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("backup sha256 is invalid")
        path = self._backup_root / "v1" / "sha256" / sha256[:2] / sha256[2:]
        self._assert_target(path, self._backup_root)
        return path

    def _restore_receipt_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        path = (
            self._storage_root
            / ".engine"
            / "receipts"
            / "original-restores"
            / f"{digest}.json"
        )
        self._assert_target(path, self._storage_root)
        return path

    def _read_restore_receipt(
        self,
        path: Path,
        operation_id: str,
    ) -> Mapping[str, Any] | None:
        value = self._read_optional_bytes(
            self._storage_root,
            path,
            maximum=1024 * 1024,
        )
        if value is None:
            return None
        try:
            receipt = json.loads(
                value.decode("ascii"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, ValueError) as exc:
            raise _repository_error(
                "the original restore receipt is invalid",
                code="invalid_capture_original_restore_receipt",
            ) from exc
        result = receipt.get("result") if isinstance(receipt, Mapping) else None
        if (
            not isinstance(receipt, Mapping)
            or set(receipt)
            != {
                "schema",
                "version",
                "operation_id",
                "command_sha256",
                "result",
            }
            or receipt.get("schema") != ORIGINAL_RESTORE_RECEIPT_SCHEMA
            or receipt.get("version") != ORIGINAL_RESTORE_RECEIPT_VERSION
            or isinstance(receipt.get("version"), bool)
            or receipt.get("operation_id") != operation_id
            or not isinstance(receipt.get("command_sha256"), str)
            or _SHA256_RE.fullmatch(receipt["command_sha256"]) is None
            or not isinstance(result, Mapping)
            or set(result)
            != {
                "operation_id",
                "item_id",
                "artifact_id",
                "before_revision",
                "after_revision",
                "backup_sha256",
                "replayed",
            }
            or result.get("operation_id") != operation_id
            or any(
                not isinstance(result.get(field), str)
                or _IDENTIFIER_RE.fullmatch(result[field]) is None
                for field in (
                    "item_id",
                    "artifact_id",
                    "before_revision",
                    "after_revision",
                )
            )
            or result.get("before_revision") == result.get("after_revision")
            or not isinstance(result.get("backup_sha256"), str)
            or _SHA256_RE.fullmatch(result["backup_sha256"]) is None
            or result.get("replayed") is not False
        ):
            raise _repository_error(
                "the original restore receipt is invalid",
                code="invalid_capture_original_restore_receipt",
            )
        return receipt

    def _read_json(self, path: Path) -> dict[str, Any]:
        payload = self._read_optional_bytes(
            self._capture_root,
            path,
            maximum=_MAX_MANIFEST_BYTES,
        )
        if payload is None:
            raise NotFoundError(
                "the capture photo asset manifest is missing",
                code="capture_original_backup_not_found",
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

    def _read_optional_image(
        self,
        root: Path,
        path: Path,
        *,
        maximum: int,
    ) -> tuple[bytes, str] | None:
        payload = self._read_optional_bytes(root, path, maximum=maximum)
        if payload is None:
            return None
        return payload, self._verified_image_media_type(payload)

    def _read_image(
        self,
        root: Path,
        path: Path,
        *,
        maximum: int,
    ) -> tuple[bytes, str]:
        result = self._read_optional_image(root, path, maximum=maximum)
        if result is None:
            raise NotFoundError(
                "the capture original bytes are unavailable",
                code="capture_original_backup_not_found",
            )
        return result

    @staticmethod
    def _verified_image_media_type(payload: bytes) -> str:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    image_format = str(image.format or "").upper()
                    image.verify()
                with Image.open(io.BytesIO(payload)) as image:
                    image.load()
                    if str(image.format or "").upper() != image_format:
                        raise ValueError("image format changed during decode")
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise _repository_error(
                "the capture original is not a decodable image",
                code="invalid_capture_original_backup",
            ) from exc
        media_type = _MEDIA_BY_FORMAT.get(image_format)
        if media_type is None:
            raise _repository_error(
                "the capture original media type is unsupported",
                code="invalid_capture_original_backup",
            )
        return media_type

    @staticmethod
    def _jpeg_display(payload: bytes) -> bytes:
        """Return a deterministic metadata-free JPEG display rendition."""

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as source:
                    source.load()
                    rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=95)
            return output.getvalue()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            UnidentifiedImageError,
            ValueError,
        ) as exc:
            raise _repository_error(
                "the corrected capture display cannot be encoded",
                code="invalid_capture_display_promotion",
            ) from exc

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
                "a capture backup authority path cannot be inspected",
                code="capture_original_backup_unavailable",
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
                "a capture backup authority target is unsafe",
                code="unsafe_capture_original_backup",
            )
        authority = self._authority_snapshot(root, path)
        descriptor = -1
        try:
            descriptor, opened = _open_verified_regular(path, named, authority=authority)
            chunks: list[bytes] = []
            total = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                total += len(block)
                if total > maximum:
                    raise ValueError("capture backup exceeds its size limit")
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
                "the capture backup authority changed during read",
                code="capture_original_backup_unavailable",
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
                "the capture backup authority root is unavailable",
                code="capture_original_backup_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting_path(root) or not stat.S_ISDIR(named_root.st_mode):
            raise _repository_error(
                "the capture backup authority root is unsafe",
                code="unsafe_capture_original_backup",
            )
        directories: list[_AuthorityDirectorySnapshot] = []
        current = root
        for part in path.relative_to(root).parts[:-1]:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a capture backup path crosses a redirecting directory",
                    code="unsafe_capture_original_backup",
                )
            try:
                named = current.lstat()
            except FileNotFoundError:
                named = None
            if named is not None and not stat.S_ISDIR(named.st_mode):
                raise _repository_error(
                    "a capture backup path component is not a directory",
                    code="unsafe_capture_original_backup",
                )
            directories.append(_AuthorityDirectorySnapshot(current, named))
        try:
            path.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a capture backup path escapes its authority",
                code="unsafe_capture_original_backup",
            ) from exc
        return _AuthoritySnapshot(root, named_root, tuple(directories))

    @staticmethod
    def _assert_target(path: Path, root: Path) -> None:
        try:
            relative = Path(path).relative_to(root)
        except ValueError as exc:
            raise ValueError("capture backup target escapes its authority") from exc
        current = root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise ValueError("capture backup target is unsafe")
            current /= part
            if _is_redirecting_path(current):
                raise ValueError("capture backup target crosses a redirecting path")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._write_set.root).as_posix()


__all__ = [
    "FilesystemCaptureOriginalBackupStore",
    "ORIGINAL_BACKUP_KEY_PREFIX",
    "ORIGINAL_BACKUP_MARKER_FIELDS",
    "ORIGINAL_BACKUP_STORE",
    "ORIGINAL_BACKUP_VERSION",
    "OriginalBackupDescriptor",
    "parse_original_backup_marker",
]
