"""Build portable ``.lib/3`` seeds from the legacy desktop capture store.

The explorer still owns capture processing and its compatibility directory
layout.  This adapter reads that durable result and translates it into the
framework-free :mod:`librarytool.engine.capture_archives` contract.  Local
paths are inputs only; neither the manifest nor the command fingerprint
contains one.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from librarytool.engine.capture_archives import (
    AssociateCaptureArchiveCommand,
    CaptureArchiveSource,
)


_PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
_CAPTURE_NOTES_SCHEMA = "org.whl.bookcapture.capture-notes"
_ARCHIVE_SOURCE_SCHEMA = "org.whl.capture-lib-source"
_ARCHIVE_SOURCE_VERSION = 1
_MAX_CAPTURE_RESOURCE_BYTES = 100 * 1024 * 1024
_MAX_CAPTURE_TOTAL_BYTES = 300 * 1024 * 1024
_MAX_CAPTURE_JSON_BYTES = 10 * 1024 * 1024
_MAX_CAPTURE_IMAGES = 200
_NUMBERED_IMAGE_RE = re.compile(r"^(orig|photo)_([1-9][0-9]*)\.jpg$")
_BIBLIOGRAPHIC_FIELDS = (
    "title",
    "subtitle",
    "author",
    "publisher",
    "city",
    "year",
    "edition",
    "volume",
    "language",
    "notes",
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _json_object(payload: bytes, *, artifact: str) -> dict[str, Any]:
    if len(payload) > _MAX_CAPTURE_JSON_BYTES:
        raise ValueError(f"{artifact} exceeds its size limit")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite number {token}")
            ),
        )
    except (RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{artifact} is not valid strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{artifact} must contain a JSON object")
    return value


def _read_regular(path: Path, *, maximum: int, artifact: str) -> bytes:
    """Read one owned capture sidecar without following a redirecting file."""

    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or path.is_symlink()
            or before.st_size > maximum
        ):
            raise ValueError(f"{artifact} is not a bounded private regular file")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"{artifact} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ValueError(f"{artifact} exceeds its size limit")
        after = os.fstat(descriptor)
        named_after = path.lstat()
        if (
            (after.st_dev, after.st_ino, after.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (named_after.st_dev, named_after.st_ino)
            != (before.st_dev, before.st_ino)
            or path.is_symlink()
        ):
            raise ValueError(f"{artifact} changed while it was read")
        return b"".join(chunks)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"{artifact} could not be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _optional_json(directory: Path, name: str) -> dict[str, Any] | None:
    path = directory / name
    try:
        payload = _read_regular(
            path,
            maximum=_MAX_CAPTURE_JSON_BYTES,
            artifact=name,
        )
    except FileNotFoundError:
        return None
    return _json_object(payload, artifact=name)


def _numbered_images(directory: Path) -> tuple[dict[int, Path], dict[int, Path]]:
    try:
        info = directory.lstat()
        resolved = directory.resolve(strict=True)
        resolved.relative_to(directory.parent.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("the capture asset directory is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or directory.is_symlink():
        raise ValueError("the capture asset directory is redirecting or invalid")
    originals: dict[int, Path] = {}
    displays: dict[int, Path] = {}
    try:
        children = tuple(directory.iterdir())
    except OSError as exc:
        raise ValueError("the capture asset directory is unavailable") from exc
    for child in children:
        match = _NUMBERED_IMAGE_RE.fullmatch(child.name)
        if match is None:
            continue
        index = int(match.group(2))
        target = originals if match.group(1) == "orig" else displays
        if index in target:
            raise ValueError("capture image sequence aliases an existing index")
        target[index] = child
    if not originals or set(originals) != set(displays):
        raise ValueError(
            "capture originals and display renditions must form one paired sequence"
        )
    expected = set(range(1, len(originals) + 1))
    if set(originals) != expected:
        raise ValueError("capture image sequence must be dense and one-based")
    if len(originals) > _MAX_CAPTURE_IMAGES:
        raise ValueError("capture image sequence exceeds its item limit")
    return originals, displays


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _revision(payload: bytes) -> str:
    return f"sha256:{_digest(payload)}"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _dimensions(payload: bytes, advertised: Any) -> dict[str, int]:
    raw = advertised if isinstance(advertised, Mapping) else {}
    width = _positive_int(raw.get("width"))
    height = _positive_int(raw.get("height"))
    orientation = raw.get("orientation")
    if isinstance(orientation, bool) or orientation not in range(1, 9):
        orientation = 1
    if width is None or height is None:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(payload)) as image:
                width = _positive_int(int(image.width))
                height = _positive_int(int(image.height))
                exif_orientation = image.getexif().get(274, orientation)
                if (
                    not isinstance(exif_orientation, bool)
                    and exif_orientation in range(1, 9)
                ):
                    orientation = exif_orientation
        except Exception:
            # Historical tests and a few pre-standardization captures contain
            # opaque image bytes.  The graph still needs bounded dimensions;
            # a 1x1 unknown raster is the honest compatibility sentinel.
            width = width or 1
            height = height or 1
    return {
        "width": int(width or 1),
        "height": int(height or 1),
        "orientation": int(orientation),
    }


def _asset_rows(
    photo_assets: Mapping[str, Any] | None,
    *,
    count: int,
) -> dict[int, Mapping[str, Any]]:
    if not isinstance(photo_assets, Mapping):
        return {}
    rows: dict[int, Mapping[str, Any]] = {}
    assets = photo_assets.get("assets")
    if not isinstance(assets, list):
        return {}
    for record in assets:
        if not isinstance(record, Mapping):
            continue
        order = record.get("capture_order")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 1
            or order > count
            or order in rows
        ):
            continue
        rows[order] = record
    return rows


def _legacy_photo_assets(
    capture_id: str,
    pairs: list[tuple[int, bytes, bytes]],
) -> dict[str, Any]:
    assets = []
    for index, original, display in pairs:
        original_dimensions = _dimensions(original, {})
        display_dimensions = _dimensions(display, {})
        assets.append(
            {
                "asset_id": f"legacy-{index}",
                "capture_order": index,
                "capture_file": f"photo_{index}.jpg",
                "original": {
                    "reference": f"archive-original-{index}",
                    "sha256": _digest(original),
                    "revision": 1,
                    **original_dimensions,
                },
                "display": {
                    "reference": f"archive-display-{index}",
                    "sha256": _digest(display),
                    "revision": 1,
                    **display_dimensions,
                    "recipe": "desktop-perspective-standardize",
                    "recipe_version": "1",
                },
                "lifecycle": {"state": "completed"},
                "role": {"suggested": "other", "confidence": 0},
                "geometry": [],
            }
        )
    return {
        "schema": _PHOTO_ASSETS_SCHEMA,
        "version": 1,
        "capture_id": capture_id,
        "legacy_fallback": True,
        "assets": assets,
        "selections": {
            "primary_title": {"asset_id": f"legacy-{pairs[0][0]}"},
            "thumbnail": {"asset_id": None},
        },
        "transport": {"representation": "original", "version": 1},
    }


def _provenance(
    *,
    generated_at: str,
    provider_id: str = "",
    model: str = "",
    recipe_revision: str = "",
) -> dict[str, Any]:
    return {
        "origin": "capture",
        "provider_id": provider_id,
        "model": model,
        "recipe_revision": recipe_revision,
        "operation_id": "",
        "generated_at": generated_at[:128],
        "ext": {},
    }


def _artifact(
    *,
    artifact_id: str,
    kind: str,
    media_type: str,
    member: str,
    content: bytes,
    representation_id: str,
    representation_revision: str,
    generated_at: str,
    dimensions: Mapping[str, int] | None = None,
    relationships: list[dict[str, Any]] | None = None,
    recipe_revision: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": artifact_id,
        "revision": _revision(content),
        "kind": kind,
        "media_type": media_type,
        "member": member,
        "content_sha256": _digest(content),
        "source": {
            "representation_id": representation_id,
            "representation_revision": representation_revision,
        },
        "provenance": _provenance(
            generated_at=generated_at,
            recipe_revision=recipe_revision,
        ),
        "category_assignments": [],
        "caption_assertions": [],
        "role_assignments": [],
        "relationships": list(relationships or []),
        "ext": {},
    }
    if dimensions is not None:
        value["dimensions"] = dict(dimensions)
    return value


def _portable_entry(entry: Mapping[str, Any], capture_id: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "org.whl.capture-generated-metadata",
        "version": 1,
        "capture_id": capture_id,
    }
    for field_name in _BIBLIOGRAPHIC_FIELDS:
        supplied = entry.get(field_name)
        if isinstance(supplied, str) and supplied:
            value[field_name] = supplied
    for field_name in ("extra", "category_ids"):
        supplied = entry.get(field_name)
        if isinstance(supplied, (dict, list)) and supplied:
            value[field_name] = supplied
    return value


def _capture_provenance_document(
    entry: Mapping[str, Any],
    capture_id: str,
) -> dict[str, Any]:
    extra = entry.get("extra")
    snapshots = {}
    if isinstance(extra, Mapping):
        for key in ("scan_collection_id", "scan_collection", "scan_from"):
            value = extra.get(key)
            if isinstance(value, str) and value:
                snapshots[key] = value
    return {
        "schema": "org.whl.capture-provenance",
        "version": 1,
        "capture_id": capture_id,
        "transport": str(entry.get("capture_transport") or "unknown")[:32],
        "created_at": str(entry.get("created_at") or "")[:128],
        "snapshot": snapshots,
    }


def build_capture_archive_source(
    capture_id: str,
    entry: Mapping[str, Any],
    capture_directory: str | Path,
) -> CaptureArchiveSource:
    """Translate one committed desktop capture into a canonical lib/3 seed."""

    if not isinstance(entry, Mapping):
        raise TypeError("entry must be a mapping")
    if str(entry.get("capture_id") or "") != capture_id:
        raise ValueError("capture entry identity does not match its asset directory")
    directory = Path(capture_directory)
    originals, displays = _numbered_images(directory)
    pairs = []
    total_image_bytes = 0
    for index in sorted(originals):
        original = _read_regular(
            originals[index],
            maximum=_MAX_CAPTURE_RESOURCE_BYTES,
            artifact=f"capture original {index}",
        )
        display = _read_regular(
            displays[index],
            maximum=_MAX_CAPTURE_RESOURCE_BYTES,
            artifact=f"capture display {index}",
        )
        total_image_bytes += len(original) + len(display)
        if total_image_bytes > _MAX_CAPTURE_TOTAL_BYTES:
            raise ValueError("capture image resources exceed their total size limit")
        pairs.append((index, original, display))

    photo_assets = _optional_json(directory, "photo_assets.json")
    if photo_assets is None:
        photo_assets = _legacy_photo_assets(capture_id, pairs)
    if (
        photo_assets.get("schema") != _PHOTO_ASSETS_SCHEMA
        or photo_assets.get("version") != 1
        or str(photo_assets.get("capture_id") or "") != capture_id
    ):
        raise ValueError("photo_assets.json does not describe this capture")
    asset_rows = _asset_rows(photo_assets, count=len(pairs))

    resources: dict[str, bytes] = {}
    representations: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    generated_at = str(entry.get("created_at") or "")[:128]
    first_display_id = ""
    first_display_revision = ""
    first_original_id = ""
    first_original_revision = ""
    for index, original, display in pairs:
        asset = asset_rows.get(index, {})
        asset_id = str(asset.get("asset_id") or f"legacy-{index}")[:128]
        original_member = f"representations/capture-original-{index}.jpg"
        display_member = f"representations/capture-display-{index}.jpg"
        original_id = f"capture-original-{index}"
        display_id = f"capture-display-{index}"
        original_revision = _revision(original)
        display_revision = _revision(display)
        original_dimensions = _dimensions(original, asset.get("original"))
        display_dimensions = _dimensions(display, asset.get("display"))
        resources[original_member] = original
        resources[display_member] = display
        representations.extend(
            [
                {
                    "id": original_id,
                    "revision": original_revision,
                    "role": "capture-original",
                    "media_type": "image/jpeg",
                    "member": original_member,
                    "content_sha256": _digest(original),
                    "dimensions": original_dimensions,
                    "lineage": [],
                    "ext": {
                        "capture": {
                            "asset_id": asset_id,
                            "capture_order": index,
                        }
                    },
                },
                {
                    "id": display_id,
                    "revision": display_revision,
                    "role": "capture-display",
                    "media_type": "image/jpeg",
                    "member": display_member,
                    "content_sha256": _digest(display),
                    "dimensions": display_dimensions,
                    "lineage": [
                        {
                            "representation_id": original_id,
                            "representation_revision": original_revision,
                            "relation": "derived-from",
                        }
                    ],
                    "ext": {
                        "capture": {
                            "asset_id": asset_id,
                            "capture_order": index,
                            "recipe": "desktop-perspective-standardize-v1",
                        }
                    },
                },
            ]
        )
        original_artifact = _artifact(
            artifact_id=f"capture-original-raster-{index}",
            kind="raster-image",
            media_type="image/jpeg",
            member=original_member,
            content=original,
            representation_id=original_id,
            representation_revision=original_revision,
            generated_at=generated_at,
            dimensions=original_dimensions,
        )
        artifacts.append(original_artifact)
        artifacts.append(
            _artifact(
                artifact_id=f"capture-display-raster-{index}",
                kind="raster-image",
                media_type="image/jpeg",
                member=display_member,
                content=display,
                representation_id=display_id,
                representation_revision=display_revision,
                generated_at=generated_at,
                dimensions=display_dimensions,
                relationships=[
                    {
                        "artifact_id": original_artifact["id"],
                        "artifact_revision": original_artifact["revision"],
                        "relation": "derived-from",
                    }
                ],
                recipe_revision="desktop-perspective-standardize-v1",
            )
        )
        if not first_display_id:
            first_display_id = display_id
            first_display_revision = display_revision
            first_original_id = original_id
            first_original_revision = original_revision

    def add_json_artifact(
        *,
        artifact_id: str,
        kind: str,
        member: str,
        value: Any,
        source_id: str = first_display_id,
        source_revision: str = first_display_revision,
    ) -> None:
        content = _canonical_json(value)
        if len(content) > _MAX_CAPTURE_JSON_BYTES:
            raise ValueError(f"{member} exceeds its JSON size limit")
        resources[member] = content
        artifacts.append(
            _artifact(
                artifact_id=artifact_id,
                kind=kind,
                media_type="application/json",
                member=member,
                content=content,
                representation_id=source_id,
                representation_revision=source_revision,
                generated_at=generated_at,
            )
        )

    add_json_artifact(
        artifact_id="capture-photo-assets",
        kind="capture-asset-manifest",
        member="artifacts/photo-assets.json",
        value=photo_assets,
        source_id=first_original_id,
        source_revision=first_original_revision,
    )
    add_json_artifact(
        artifact_id="capture-generated-metadata",
        kind="generated-metadata",
        member="artifacts/generated-metadata.json",
        value=_portable_entry(entry, capture_id),
    )
    add_json_artifact(
        artifact_id="capture-geometry",
        kind="capture-geometry",
        member="artifacts/geometry.json",
        value={
            "schema": "org.whl.capture-geometry",
            "version": 1,
            "capture_id": capture_id,
            "assets": [
                {
                    "asset_id": str(record.get("asset_id") or f"legacy-{index}"),
                    "capture_order": index,
                    "geometry": (
                        record.get("geometry")
                        if isinstance(record.get("geometry"), list)
                        else []
                    ),
                }
                for index, record in (
                    (index, asset_rows.get(index, {}))
                    for index in range(1, len(pairs) + 1)
                )
            ],
        },
    )
    capture_notes = _optional_json(directory, "capture_notes.json")
    if capture_notes is None:
        capture_notes = {
            "schema": _CAPTURE_NOTES_SCHEMA,
            "version": 1,
            "capture_id": capture_id,
            "notes": [],
        }
    add_json_artifact(
        artifact_id="capture-notes",
        kind="capture-notes",
        member="artifacts/capture-notes.json",
        value=capture_notes,
    )
    add_json_artifact(
        artifact_id="capture-provenance",
        kind="capture-provenance",
        member="artifacts/capture-provenance.json",
        value=_capture_provenance_document(entry, capture_id),
    )

    ocr_path = directory / "ocr.txt"
    try:
        ocr = _read_regular(
            ocr_path,
            maximum=_MAX_CAPTURE_RESOURCE_BYTES,
            artifact="capture OCR",
        )
    except FileNotFoundError:
        ocr = b""
    if ocr:
        ocr_member = "artifacts/ocr.txt"
        resources[ocr_member] = ocr
        artifacts.append(
            _artifact(
                artifact_id="capture-ocr",
                kind="ocr-text",
                media_type="text/plain",
                member=ocr_member,
                content=ocr,
                representation_id=first_display_id,
                representation_revision=first_display_revision,
                generated_at=generated_at,
            )
        )

    meta = {
        field_name: value
        for field_name in (
            "title",
            "subtitle",
            "author",
            "publisher",
            "city",
            "year",
            "edition",
            "volume",
            "language",
        )
        if isinstance((value := entry.get(field_name)), str) and value
    }
    manifest: dict[str, Any] = {
        "source": "capture",
        "meta": meta,
        "instructions": {
            "book": (
                "Preserve immutable capture originals, representation lineage, "
                "source revisions, provenance, and human-authored metadata."
            )
        },
        "representations": representations,
        "artifacts": artifacts,
        "review_policy": {"mode": "all-durable"},
        "ext": {
            "capture": {
                "capture_id": capture_id,
                "transport": str(
                    entry.get("capture_transport") or "unknown"
                )[:32],
            }
        },
    }
    if generated_at:
        manifest["created_at"] = generated_at
    revision_seed = {
        "schema": _ARCHIVE_SOURCE_SCHEMA,
        "version": _ARCHIVE_SOURCE_VERSION,
        "capture_id": capture_id,
        "manifest": manifest,
        "resources": [
            {
                "member": member,
                "bytes": len(content),
                "sha256": _digest(content),
            }
            for member, content in sorted(resources.items())
        ],
    }
    source_revision = "sha256:" + _digest(_canonical_json(revision_seed))
    return CaptureArchiveSource(
        capture_id=capture_id,
        source_revision=source_revision,
        manifest=manifest,
        resources=resources,
    )


def build_capture_archive_command(
    capture_id: str,
    entry: Mapping[str, Any],
    capture_directory: str | Path,
) -> AssociateCaptureArchiveCommand:
    """Build the stable command used by both LAN and cloud import retries."""

    source = build_capture_archive_source(capture_id, entry, capture_directory)
    return AssociateCaptureArchiveCommand(
        source=source,
        operation_id=f"capture-import-{source.fingerprint}",
    )


__all__ = [
    "build_capture_archive_command",
    "build_capture_archive_source",
]
