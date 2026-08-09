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
import math
import os
import re
import stat
import sys
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from librarytool.engine.capture_archives import (  # noqa: E402
    AssociateCaptureArchiveCommand,
    CaptureArchiveAssociation,
    CaptureArchiveAssociationPublisherPort,
    CaptureArchiveDisposition,
    CaptureArchiveMaterializerPort,
    CaptureArchiveService,
    CaptureArchiveSource,
    capture_book_id,
)
from librarytool.engine.errors import EngineError  # noqa: E402
from librarytool.adapters.filesystem.corrections_artifact_repository import (  # noqa: E402
    _AuthorityDirectorySnapshot,
    _AuthoritySnapshot,
    _finish_verified_regular,
    _open_verified_regular,
)


_PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
_CAPTURE_NOTES_SCHEMA = "org.whl.bookcapture.capture-notes"
_ARCHIVE_SOURCE_SCHEMA = "org.whl.capture-lib-source"
_ARCHIVE_SOURCE_VERSION = 1
_MAX_CAPTURE_RESOURCE_BYTES = 100 * 1024 * 1024
_MAX_CAPTURE_TOTAL_BYTES = 300 * 1024 * 1024
_MAX_CAPTURE_JSON_BYTES = 10 * 1024 * 1024
_MAX_CAPTURE_IMAGES = 200
_MAX_REPRESENTATION_LINEAGE = 64
_MAX_PORTABLE_JSON_DEPTH = 64
_MAX_PORTABLE_JSON_NODES = 50_000
_MAX_PORTABLE_JSON_STRING = 1_000_000
_MAX_PORTABLE_INTEGER = (1 << 53) - 1
_MAX_BACKFILL_DIAGNOSTICS = 10_000
_MAX_DIAGNOSTIC_TEXT = 240
_CAPTURE_DIRECTORY_SNAPSHOT_ATTEMPTS = 2
_NUMBERED_IMAGE_RE = re.compile(r"^(orig|photo)_([1-9][0-9]*)\.jpg$")
_BOOK_ID_RE = re.compile(r"^b-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_DIAGNOSTIC_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_ASSET_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_WINDOWS_PATH_RE = re.compile(r"(?:^|[\s\"'(])[A-Za-z]:[\\/]")
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_PRIVATE_LOCATOR_KEYS = frozenset(
    {
        "absolute_path",
        "asset_ref",
        "capture_file",
        "display_ref",
        "file",
        "file_name",
        "filename",
        "filepath",
        "local_path",
        "locator",
        "path",
        "raw_ref",
        "reference",
        "resource_ref",
        "storage_key",
        "storage_locator",
        "storage_path",
        "uri",
        "url",
        "workspace_path",
    }
)
_ORIGINAL_BACKUP_FIELDS = frozenset(
    {"version", "store", "key", "sha256", "bytes", "media_type"}
)
_ORIGINAL_BACKUP_STORE = "output-originals-sha256"
_ORIGINAL_BACKUP_MEDIA_TYPES = frozenset(
    {"image/jpeg"}
)
_PRIVATE_LOCATOR_SUFFIXES = frozenset(
    {"file", "filename", "filepath", "locator", "path", "ref", "reference", "uri", "url"}
)
_DROP = object()
_BUILD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
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


@dataclass(frozen=True, slots=True)
class _CaptureDirectorySnapshot:
    resolved: Path
    parent: tuple[int, ...]
    directory: tuple[int, ...]
    children: tuple[tuple[str, tuple[int, ...]], ...]


@dataclass(frozen=True, slots=True)
class _CaptureDirectoryAuthority:
    resolved: Path
    resolved_parent: Path
    parent: tuple[int, ...]
    directory: tuple[int, ...]


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


def _stable_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
    )


def _authority_stat_identity(info: os.stat_result) -> tuple[int, ...]:
    """Replacement-sensitive identity for a parent whose children may churn."""

    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(getattr(info, "st_file_attributes", 0)),
    )


def _is_redirecting_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if os.name == "nt" and os.path.lexists(path):
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
        return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
    return False


def _capture_directory_snapshot_attempt(
    directory: Path,
    *,
    expected_authority: _CaptureDirectoryAuthority | None,
) -> tuple[_CaptureDirectorySnapshot | None, _CaptureDirectoryAuthority]:
    """Capture one flat generation, or ``None`` if it changed mid-listing."""

    try:
        parent_before = directory.parent.lstat()
        before = directory.lstat()
        resolved = directory.resolve(strict=True)
        resolved_parent = directory.parent.resolve(strict=True)
        resolved.relative_to(resolved_parent)
    except (OSError, ValueError) as exc:
        raise ValueError("the capture asset directory is unavailable") from exc
    if (
        not stat.S_ISDIR(parent_before.st_mode)
        or _is_redirecting_path(directory.parent)
        or not stat.S_ISDIR(before.st_mode)
        or _is_redirecting_path(directory)
    ):
        raise ValueError("the capture asset directory is redirecting or invalid")
    authority = _CaptureDirectoryAuthority(
        resolved=resolved,
        resolved_parent=resolved_parent,
        parent=_authority_stat_identity(parent_before),
        directory=_authority_stat_identity(before),
    )
    if expected_authority is not None and authority != expected_authority:
        raise ValueError("the capture asset directory changed while it was listed")

    children: list[tuple[str, tuple[int, ...]]] = []
    try:
        with os.scandir(directory) as iterator:
            for child in iterator:
                children.append(
                    (
                        child.name,
                        _stable_stat_identity(child.stat(follow_symlinks=False)),
                    )
                )
        after = directory.lstat()
        parent_after = directory.parent.lstat()
    except OSError as exc:
        raise ValueError("the capture asset directory is unavailable") from exc
    if (
        _authority_stat_identity(parent_after)
        != authority.parent
        or _is_redirecting_path(directory.parent)
        or directory.parent.resolve(strict=True) != authority.resolved_parent
        or _authority_stat_identity(after)
        != authority.directory
        or _is_redirecting_path(directory)
        or directory.resolve(strict=True) != authority.resolved
    ):
        raise ValueError("the capture asset directory changed while it was listed")
    if _stable_stat_identity(after) != _stable_stat_identity(before):
        return None, authority
    return (
        _CaptureDirectorySnapshot(
            resolved=authority.resolved,
            parent=authority.parent,
            directory=_stable_stat_identity(after),
            children=tuple(sorted(children)),
        ),
        authority,
    )


def _capture_directory_snapshot(directory: Path) -> _CaptureDirectorySnapshot:
    """Capture one stable, non-redirecting flat directory generation.

    A concurrent child publication or filesystem metadata update can overlap
    one listing without making either settled generation incomplete. Retry the
    whole listing once so the caller gets one generation. Authority changes,
    redirects, unavailable paths, and continuous churn still fail closed.
    """

    authority: _CaptureDirectoryAuthority | None = None
    for _attempt in range(_CAPTURE_DIRECTORY_SNAPSHOT_ATTEMPTS):
        snapshot, authority = _capture_directory_snapshot_attempt(
            directory,
            expected_authority=authority,
        )
        if snapshot is not None:
            return snapshot
    raise ValueError("the capture asset directory changed while it was listed")


def _validate_capture_directory_snapshot(
    directory: Path,
    expected: _CaptureDirectorySnapshot,
) -> None:
    if _capture_directory_snapshot(directory) != expected:
        raise ValueError("the capture asset directory changed while it was archived")


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


def _confined_authority_snapshot(
    authority_root: Path,
    confined_root: Path,
    path: Path,
    *,
    artifact: str,
) -> _AuthoritySnapshot:
    """Snapshot every directory identity from an authority root to a file."""

    authority_root = Path(authority_root)
    confined_root = Path(confined_root)
    path = Path(path)
    if not authority_root.is_absolute():
        raise ValueError(f"{artifact} authority root must be absolute")
    try:
        confined_root.relative_to(authority_root)
        path.relative_to(confined_root)
        relative = path.relative_to(authority_root)
    except ValueError as exc:
        raise ValueError(f"{artifact} escapes its confined store") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{artifact} has an unsafe confined path")

    try:
        named_root = authority_root.lstat()
        resolved_root = authority_root.resolve(strict=True)
        if (
            not stat.S_ISDIR(named_root.st_mode)
            or _is_redirecting_path(authority_root)
        ):
            raise ValueError(f"{artifact} authority root is redirecting or invalid")

        directories: list[_AuthorityDirectorySnapshot] = []
        current = authority_root
        for part in relative.parts[:-1]:
            current /= part
            named = current.lstat()
            if not stat.S_ISDIR(named.st_mode) or _is_redirecting_path(current):
                raise ValueError(
                    f"{artifact} authority redirects through an invalid directory"
                )
            current.resolve(strict=True).relative_to(resolved_root)
            directories.append(_AuthorityDirectorySnapshot(current, named))
        confined_root.resolve(strict=True).relative_to(resolved_root)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"{artifact} authority is unavailable") from exc
    return _AuthoritySnapshot(
        authority_root,
        named_root,
        tuple(directories),
    )


def _same_confined_authority(
    before: _AuthoritySnapshot,
    after: _AuthoritySnapshot,
) -> bool:
    """Compare replacement-sensitive identities without rejecting child churn."""

    if before.root != after.root or not os.path.samestat(
        before.named_root,
        after.named_root,
    ):
        return False
    if _authority_stat_identity(before.named_root) != _authority_stat_identity(
        after.named_root
    ):
        return False
    if len(before.directories) != len(after.directories):
        return False
    for expected, actual in zip(
        before.directories,
        after.directories,
        strict=True,
    ):
        if (
            expected.path != actual.path
            or expected.named is None
            or actual.named is None
            or not os.path.samestat(expected.named, actual.named)
            or _authority_stat_identity(expected.named)
            != _authority_stat_identity(actual.named)
        ):
            return False
    return True


def _read_confined_regular(
    path: Path,
    *,
    authority_root: Path,
    confined_root: Path,
    maximum: int,
    artifact: str,
) -> bytes:
    """Read one private file while pinning its complete authority chain."""

    descriptor = -1
    try:
        authority = _confined_authority_snapshot(
            authority_root,
            confined_root,
            path,
            artifact=artifact,
        )
        named_before = path.lstat()
        if (
            not stat.S_ISREG(named_before.st_mode)
            or named_before.st_nlink != 1
            or _is_redirecting_path(path)
            or named_before.st_size > maximum
        ):
            raise ValueError(f"{artifact} is not a bounded private regular file")
        descriptor, opened_before = _open_verified_regular(
            path,
            named_before,
            authority=authority,
        )
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
        _finish_verified_regular(
            path,
            descriptor,
            named_before=named_before,
            opened_before=opened_before,
        )
        after = _confined_authority_snapshot(
            authority_root,
            confined_root,
            path,
            artifact=artifact,
        )
        if not _same_confined_authority(authority, after):
            raise ValueError(f"{artifact} authority changed while it was read")
        return b"".join(chunks)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
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


def _numbered_images(
    directory: Path,
    snapshot: _CaptureDirectorySnapshot,
) -> tuple[dict[int, Path], dict[int, Path]]:
    originals: dict[int, Path] = {}
    displays: dict[int, Path] = {}
    for name, _identity in snapshot.children:
        match = _NUMBERED_IMAGE_RE.fullmatch(name)
        if match is None:
            continue
        index = int(match.group(2))
        target = originals if match.group(1) == "orig" else displays
        if index in target:
            raise ValueError("capture image sequence aliases an existing index")
        target[index] = directory / name
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
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_PORTABLE_INTEGER
    ):
        return None
    return value


def _measured_dimensions(payload: bytes, *, artifact: str) -> dict[str, int]:
    """Measure and fully verify an embedded JPEG."""

    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "JPEG":
                raise ValueError(f"{artifact} is not a JPEG image")
            width = _positive_int(int(image.width))
            height = _positive_int(int(image.height))
            if width is None or height is None:
                raise ValueError(f"{artifact} has invalid image dimensions")
            orientation: Any = image.getexif().get(274, 1)
            if (
                isinstance(orientation, bool)
                or not isinstance(orientation, int)
                or orientation not in range(1, 9)
            ):
                orientation = 1
            image.verify()
            return {
                "width": width,
                "height": height,
                "orientation": orientation,
            }
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{artifact} is not a valid JPEG image") from exc


def _dimensions(
    payload: bytes,
    advertised: Any,
    *,
    artifact: str,
) -> dict[str, int]:
    measured = _measured_dimensions(payload, artifact=artifact)
    raw = advertised if isinstance(advertised, Mapping) else {}
    has_width = "width" in raw
    has_height = "height" in raw
    if has_width != has_height:
        raise ValueError(f"{artifact} advertises incomplete image dimensions")
    if has_width:
        width = _positive_int(raw.get("width"))
        height = _positive_int(raw.get("height"))
        if width is None or height is None:
            raise ValueError(f"{artifact} advertises invalid image dimensions")
        if (width, height) != (measured["width"], measured["height"]):
            raise ValueError(f"{artifact} dimensions contradict its image bytes")
    if "orientation" in raw and raw.get("orientation") not in (None, 0):
        orientation = raw.get("orientation")
        if (
            isinstance(orientation, bool)
            or not isinstance(orientation, int)
            or orientation not in range(1, 9)
        ):
            raise ValueError(f"{artifact} advertises an invalid orientation")
        if orientation != measured["orientation"]:
            raise ValueError(f"{artifact} orientation contradicts its image bytes")
    return measured


def _normalized_metadata_key(key: str) -> str:
    separated = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", key)
    separated = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", separated)
    return _KEY_SEPARATOR_RE.sub("_", separated).strip("_").casefold()


def _private_locator_key(key: str) -> bool:
    normalized = _normalized_metadata_key(key)
    if normalized in _PRIVATE_LOCATOR_KEYS:
        return True
    return normalized.rsplit("_", 1)[-1] in _PRIVATE_LOCATOR_SUFFIXES


def _looks_like_local_locator(value: str) -> bool:
    stripped = value.strip()
    normalized = stripped.replace("\\", "/")
    lowered = normalized.casefold()
    return bool(
        _WINDOWS_PATH_RE.search(stripped)
        or stripped.startswith(("\\\\", "//", "file:"))
        or "file://" in lowered
        or normalized.startswith("/")
        or normalized.startswith("./")
        or normalized.startswith("../")
        or "/../" in normalized
        or (
            not lowered.startswith(("http://", "https://"))
            and (
                "\\" in stripped
                or (
                    "/" in normalized
                    and (
                        normalized.count("/") > 1
                        or bool(
                            re.search(
                                r"\.[A-Za-z0-9]{1,12}$",
                                normalized,
                            )
                        )
                    )
                )
                or bool(
                    re.fullmatch(
                        r"[^\s/\\]+\.(?:bmp|gif|jpe?g|json|pdf|png|tiff?|txt|webp)",
                        stripped,
                        re.IGNORECASE,
                    )
                )
            )
        )
    )


def _portable_json_projection(
    value: Any,
    *,
    artifact: str,
    drop_local_values: bool = True,
    _depth: int = 0,
    _active: set[int] | None = None,
    _budget: list[int] | None = None,
) -> Any:
    """Detach strict portable JSON while removing adapter-private locators."""

    if _depth > _MAX_PORTABLE_JSON_DEPTH:
        raise ValueError(f"{artifact} is nested too deeply")
    if _budget is None:
        _budget = [0]
    _budget[0] += 1
    if _budget[0] > _MAX_PORTABLE_JSON_NODES:
        raise ValueError(f"{artifact} contains too many values")

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_PORTABLE_JSON_STRING or any(
            ord(character) == 127
            or (ord(character) < 32 and character not in "\n\r\t")
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        ):
            raise ValueError(f"{artifact} contains an invalid string")
        if drop_local_values and _looks_like_local_locator(value):
            return _DROP
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_PORTABLE_INTEGER:
            raise ValueError(f"{artifact} contains a non-portable integer")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{artifact} contains a non-finite number")
        return value

    if _active is None:
        _active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in _active:
            raise ValueError(f"{artifact} contains a reference cycle")
        _active.add(identity)
        try:
            projected: dict[str, Any] = {}
            for key, item in value.items():
                if (
                    not isinstance(key, str)
                    or not key
                    or key != key.strip()
                    or len(key) > 128
                ):
                    raise ValueError(
                        f"{artifact} keys must be bounded, trimmed strings"
                    )
                if any(
                    ord(character) < 32
                    or ord(character) == 127
                    or 0xD800 <= ord(character) <= 0xDFFF
                    for character in key
                ):
                    raise ValueError(f"{artifact} contains an invalid key")
                if _private_locator_key(key):
                    continue
                clean = _portable_json_projection(
                    item,
                    artifact=f"{artifact}.{key}",
                    drop_local_values=drop_local_values,
                    _depth=_depth + 1,
                    _active=_active,
                    _budget=_budget,
                )
                if clean is not _DROP:
                    projected[key] = clean
            return projected
        finally:
            _active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in _active:
            raise ValueError(f"{artifact} contains a reference cycle")
        _active.add(identity)
        try:
            projected_items = []
            for index, item in enumerate(value):
                clean = _portable_json_projection(
                    item,
                    artifact=f"{artifact}[{index}]",
                    drop_local_values=drop_local_values,
                    _depth=_depth + 1,
                    _active=_active,
                    _budget=_budget,
                )
                if clean is not _DROP:
                    projected_items.append(clean)
            return projected_items
        finally:
            _active.remove(identity)
    raise ValueError(f"{artifact} contains a non-JSON value")


def _required_checksum(
    value: Any,
    *,
    artifact: str,
) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{artifact} must contain a SHA-256 checksum")
    return value.casefold()


def _asset_token(value: Any, *, artifact: str) -> str:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or not _ASSET_TOKEN_RE.fullmatch(value)
    ):
        raise ValueError(f"{artifact} must be a portable asset identifier")
    return value


def _asset_rows(
    photo_assets: Mapping[str, Any] | None,
    *,
    count: int,
) -> dict[int, Mapping[str, Any]]:
    if not isinstance(photo_assets, Mapping):
        raise ValueError("photo_assets.json must contain an object")
    rows: dict[int, Mapping[str, Any]] = {}
    assets = photo_assets.get("assets")
    if not isinstance(assets, list) or len(assets) != count:
        raise ValueError(
            "photo_assets.json must describe every captured image exactly once"
        )
    for record in assets:
        if not isinstance(record, Mapping):
            raise ValueError("photo_assets.json contains a non-object asset")
        order = record.get("capture_order")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 1
            or order > count
            or order in rows
        ):
            raise ValueError(
                "photo_assets.json contains an invalid or duplicate capture order"
            )
        _asset_token(
            record.get("asset_id"),
            artifact=f"photo asset {order} id",
        )
        if record.get("capture_file") != f"photo_{order}.jpg":
            raise ValueError(
                f"photo asset {order} does not identify its captured image"
            )
        if not isinstance(record.get("original"), Mapping) or not isinstance(
            record.get("display"), Mapping
        ):
            raise ValueError(
                f"photo asset {order} is missing its representation evidence"
            )
        rows[order] = record
    if set(rows) != set(range(1, count + 1)):
        raise ValueError("photo_assets.json capture order must be dense")
    return rows


def _desktop_import_rows(
    photo_assets: Mapping[str, Any],
    *,
    asset_rows: Mapping[int, Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    imported = photo_assets.get("desktop_import")
    if imported is None:
        return {}
    if (
        not isinstance(imported, Mapping)
        or imported.get("version") != 1
        or not isinstance(imported.get("assets"), list)
        or len(imported["assets"]) != len(asset_rows)
    ):
        raise ValueError("photo_assets.json has an invalid desktop import record")
    rows: dict[int, Mapping[str, Any]] = {}
    for record in imported["assets"]:
        if not isinstance(record, Mapping):
            raise ValueError("desktop import evidence contains a non-object asset")
        order = record.get("order")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 0
            or order >= len(asset_rows)
            or order + 1 in rows
        ):
            raise ValueError(
                "desktop import evidence has an invalid or duplicate order"
            )
        capture_order = order + 1
        if record.get("asset_id") != asset_rows[capture_order].get("asset_id"):
            raise ValueError("desktop import evidence identifies another asset")
        rows[capture_order] = record
    if set(rows) != set(asset_rows):
        raise ValueError("desktop import evidence must cover every captured image")
    return rows


def _capture_image_reference(
        directory: Path, reference: Any, *, artifact: str) -> Path:
    if (
        not isinstance(reference, str)
        or not reference
        or reference in {".", ".."}
        or "/" in reference
        or "\\" in reference
        or Path(reference).name != reference
    ):
        raise ValueError(f"{artifact} has an invalid local reference")
    return directory / reference


def _cold_original_bytes(
        directory: Path, imported: Mapping[str, Any],
        original: Mapping[str, Any], *, index: int) -> bytes:
    """Read one explicitly requested original from live or cold storage."""

    raw_ref = imported.get("raw_ref")
    marker = imported.get("original_backup")
    if "raw_ref" in imported:
        if marker is not None:
            raise ValueError(
                f"photo asset {index} has both live and backed-up originals")
        if not raw_ref:
            raise ValueError(
                f"photo asset {index} has an invalid live original reference"
            )
        return _read_regular(
            _capture_image_reference(
                directory,
                raw_ref,
                artifact=f"photo asset {index} original",
            ),
            maximum=_MAX_CAPTURE_RESOURCE_BYTES,
            artifact=f"capture original {index}",
        )
    source_sha256 = _required_checksum(
        imported.get("source_checksum") or original.get("sha256"),
        artifact=f"photo asset {index} original",
    )
    for anchor in (imported.get("source_checksum"), original.get("sha256")):
        if anchor not in (None, "") and _required_checksum(
            anchor,
            artifact=f"photo asset {index} original",
        ) != source_sha256:
            raise ValueError(
                f"photo asset {index} original checksums disagree"
            )
    if (
        not isinstance(marker, Mapping)
        or set(marker) != _ORIGINAL_BACKUP_FIELDS
        or marker.get("version") != 1
        or isinstance(marker.get("version"), bool)
        or marker.get("store") != _ORIGINAL_BACKUP_STORE
        or marker.get("sha256") != source_sha256
        or marker.get("key") != f"sha256:{source_sha256}"
        or marker.get("media_type") not in _ORIGINAL_BACKUP_MEDIA_TYPES
        or isinstance(marker.get("bytes"), bool)
        or not isinstance(marker.get("bytes"), int)
        or marker["bytes"] <= 0
        or marker["bytes"] > _MAX_CAPTURE_RESOURCE_BYTES
    ):
        raise ValueError(f"photo asset {index} has an invalid original backup")
    data_root = directory.parent.parent
    backup_root = data_root / "output" / "backups" / "originals"
    backup_path = (
        backup_root
        / "v1"
        / "sha256"
        / source_sha256[:2]
        / source_sha256[2:]
    )
    original_bytes = _read_confined_regular(
        backup_path,
        authority_root=data_root,
        confined_root=backup_root,
        maximum=_MAX_CAPTURE_RESOURCE_BYTES,
        artifact=f"capture original backup {index}",
    )
    if (
        len(original_bytes) != marker["bytes"]
        or _digest(original_bytes) != source_sha256
    ):
        raise ValueError(f"photo asset {index} original backup is corrupt")
    return original_bytes


def _asset_evidence(
    *,
    index: int,
    asset: Mapping[str, Any],
    imported: Mapping[str, Any] | None,
    original: bytes,
    display: bytes,
) -> dict[str, Any]:
    original_record = asset["original"]
    display_record = asset["display"]
    assert isinstance(original_record, Mapping)
    assert isinstance(display_record, Mapping)
    actual_original = _digest(original)
    actual_display = _digest(display)
    advertised_original = _required_checksum(
        original_record.get("sha256"),
        artifact=f"photo asset {index} original",
    )
    advertised_display = _required_checksum(
        display_record.get("sha256"),
        artifact=f"photo asset {index} display",
    )
    if advertised_original != actual_original:
        raise ValueError(
            f"photo asset {index} original checksum contradicts its image bytes"
        )

    if imported is None:
        if advertised_display != actual_display:
            raise ValueError(
                f"photo asset {index} display checksum contradicts its image bytes"
            )
    else:
        source_checksum = _required_checksum(
            imported.get("source_checksum"),
            artifact=f"photo asset {index} imported source",
        )
        derivative_checksum = _required_checksum(
            imported.get("derivative_checksum"),
            artifact=f"photo asset {index} imported derivative",
        )
        if source_checksum != actual_original:
            raise ValueError(
                f"photo asset {index} imported source checksum contradicts its bytes"
            )
        if derivative_checksum != actual_display:
            raise ValueError(
                f"photo asset {index} imported derivative checksum contradicts "
                "its bytes"
            )

    original_dimensions = _dimensions(
        original,
        original_record,
        artifact=f"photo asset {index} original",
    )
    # A phone-side display checksum can describe a derivative which is not
    # embedded after desktop processing.  A validated desktop_import record
    # disambiguates that lineage; otherwise the advertised display is the
    # embedded display and its dimensions must agree exactly.
    display_advertised: Any = (
        display_record
        if imported is None or advertised_display == actual_display
        else {}
    )
    display_dimensions = _dimensions(
        display,
        display_advertised,
        artifact=f"photo asset {index} display",
    )
    return {
        "asset_id": _asset_token(
            asset.get("asset_id"),
            artifact=f"photo asset {index} id",
        ),
        "original_sha256": actual_original,
        "display_sha256": actual_display,
        "original_dimensions": original_dimensions,
        "display_dimensions": display_dimensions,
        "upstream_display_matches": advertised_display == actual_display,
    }


def _portable_photo_assets(
    *,
    capture_id: str,
    photo_assets: Mapping[str, Any],
    asset_rows: Mapping[int, Mapping[str, Any]],
    import_rows: Mapping[int, Mapping[str, Any]],
    evidence: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any]:
    """Project legacy/mobile sidecars onto embedded archive identities."""

    top_extra = {
        key: value
        for key, value in photo_assets.items()
        if key
        not in {
            "schema",
            "version",
            "capture_id",
            "assets",
            "desktop_import",
        }
    }
    projected_top = _portable_json_projection(
        top_extra,
        artifact="photo_assets.json",
    )
    assert isinstance(projected_top, dict)
    result: dict[str, Any] = {
        **projected_top,
        "schema": _PHOTO_ASSETS_SCHEMA,
        "version": 1,
        "capture_id": capture_id,
        "archive_projection": {
            "version": 1,
            "representation": "embedded",
        },
    }

    projected_assets = []
    for index in sorted(asset_rows):
        asset = asset_rows[index]
        proof = evidence[index]
        original = asset["original"]
        display = asset["display"]
        assert isinstance(original, Mapping)
        assert isinstance(display, Mapping)
        asset_extra = _portable_json_projection(
            {
                key: value
                for key, value in asset.items()
                if key
                not in {
                    "asset_id",
                    "capture_order",
                    "capture_file",
                    "original",
                    "display",
                }
            },
            artifact=f"photo_assets.json.assets[{index - 1}]",
        )
        original_extra = _portable_json_projection(
            {
                key: value
                for key, value in original.items()
                if key
                not in {
                    "reference",
                    "sha256",
                    "width",
                    "height",
                    "orientation",
                }
            },
            artifact=f"photo asset {index} original metadata",
        )
        display_extra = _portable_json_projection(
            {
                key: value
                for key, value in display.items()
                if key
                not in {
                    "reference",
                    "sha256",
                    "width",
                    "height",
                    "orientation",
                }
            },
            artifact=f"photo asset {index} display metadata",
        )
        assert isinstance(asset_extra, dict)
        assert isinstance(original_extra, dict)
        assert isinstance(display_extra, dict)
        imported = import_rows.get(index)
        if imported is not None and isinstance(imported.get("recipe"), str):
            recipe = _portable_json_projection(
                imported["recipe"],
                artifact=f"photo asset {index} imported recipe",
            )
            if recipe is not _DROP:
                display_extra["recipe"] = recipe
        projected_assets.append(
            {
                **asset_extra,
                "asset_id": proof["asset_id"],
                "capture_order": index,
                "original": {
                    **original_extra,
                    "representation_id": f"capture-original-{index}",
                    "representation_revision": (
                        f"sha256:{proof['original_sha256']}"
                    ),
                    "sha256": proof["original_sha256"],
                    **proof["original_dimensions"],
                },
                "display": {
                    **display_extra,
                    "representation_id": f"capture-display-{index}",
                    "representation_revision": (
                        f"sha256:{proof['display_sha256']}"
                    ),
                    "sha256": proof["display_sha256"],
                    **proof["display_dimensions"],
                },
            }
        )
    result["assets"] = projected_assets

    if import_rows:
        imported_at = ""
        imported = photo_assets.get("desktop_import")
        if isinstance(imported, Mapping):
            raw_imported_at = imported.get("imported_at")
            if isinstance(raw_imported_at, str):
                clean_imported_at = _portable_json_projection(
                    raw_imported_at[:128],
                    artifact="photo_assets.json.desktop_import.imported_at",
                    drop_local_values=False,
                )
                assert isinstance(clean_imported_at, str)
                imported_at = clean_imported_at
        result["desktop_import"] = {
            "version": 1,
            "imported_at": imported_at,
            "assets": [
                {
                    "asset_id": evidence[index]["asset_id"],
                    "capture_order": index,
                    "source_sha256": evidence[index]["original_sha256"],
                    "derivative_sha256": evidence[index]["display_sha256"],
                    **(
                        _portable_json_projection(
                            {
                                key: value
                                for key, value in import_rows[index].items()
                                if key
                                not in {
                                    "order",
                                    "asset_id",
                                    "raw_ref",
                                    "display_ref",
                                    "source_checksum",
                                    "derivative_checksum",
                                    "original_backup",
                                    "active_desktop_correction_id",
                                }
                            },
                            artifact=(
                                "photo_assets.json.desktop_import."
                                f"assets[{index - 1}]"
                            ),
                        )
                    ),
                }
                for index in sorted(import_rows)
            ],
        }
    return result


def _legacy_photo_assets(
    capture_id: str,
    pairs: list[tuple[int, bytes, bytes]],
) -> dict[str, Any]:
    assets = []
    for index, original, display in pairs:
        original_dimensions = _dimensions(
            original,
            {},
            artifact=f"legacy capture original {index}",
        )
        display_dimensions = _dimensions(
            display,
            {},
            artifact=f"legacy capture display {index}",
        )
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


def _capture_aggregate_source(
    *,
    capture_id: str,
    display_sources: list[tuple[str, str]],
    resources: dict[str, bytes],
    representations: list[dict[str, Any]],
) -> tuple[str, str]:
    """Return one representation whose bounded lineage covers every photo.

    A display representation already links to its corresponding immutable
    original. Grouping every display therefore gives aggregate artifacts a
    transitive source path to every representation in the capture without
    exceeding lib/3's 64-entry direct-lineage limit.
    """

    if not display_sources:
        raise ValueError("a capture aggregate needs at least one display source")
    if len(display_sources) == 1:
        return display_sources[0]

    current = [
        {
            "representation_id": representation_id,
            "representation_revision": representation_revision,
            "leaf_count": 1,
        }
        for representation_id, representation_revision in display_sources
    ]
    level = 1
    while len(current) > 1:
        grouped: list[dict[str, Any]] = []
        for group_index, offset in enumerate(
            range(0, len(current), _MAX_REPRESENTATION_LINEAGE),
            start=1,
        ):
            members = current[offset : offset + _MAX_REPRESENTATION_LINEAGE]
            lineage = [
                {
                    "representation_id": member["representation_id"],
                    "representation_revision": member["representation_revision"],
                    "relation": "aggregates",
                }
                for member in members
            ]
            leaf_count = sum(int(member["leaf_count"]) for member in members)
            representation_id = f"capture-group-{level}-{group_index}"
            member_path = (
                f"representations/capture-group-{level}-{group_index}.json"
            )
            content = _canonical_json(
                {
                    "schema": "org.whl.capture-representation-group",
                    "version": 1,
                    "capture_id": capture_id,
                    "level": level,
                    "group_index": group_index,
                    "leaf_count": leaf_count,
                    "members": lineage,
                }
            )
            representation_revision = _revision(content)
            resources[member_path] = content
            representations.append(
                {
                    "id": representation_id,
                    "revision": representation_revision,
                    "role": "capture-group",
                    "media_type": "application/json",
                    "member": member_path,
                    "content_sha256": _digest(content),
                    "lineage": lineage,
                    "ext": {
                        "capture": {
                            "group_level": level,
                            "group_index": group_index,
                            "leaf_count": leaf_count,
                        }
                    },
                }
            )
            grouped.append(
                {
                    "representation_id": representation_id,
                    "representation_revision": representation_revision,
                    "leaf_count": leaf_count,
                }
            )
        current = grouped
        level += 1
    return (
        str(current[0]["representation_id"]),
        str(current[0]["representation_revision"]),
    )


def _portable_entry(entry: Mapping[str, Any], capture_id: str) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "org.whl.capture-generated-metadata",
        "version": 1,
        "capture_id": capture_id,
    }
    for field_name in _BIBLIOGRAPHIC_FIELDS:
        supplied = entry.get(field_name)
        if isinstance(supplied, str) and supplied:
            projected = _portable_json_projection(
                supplied,
                artifact=f"capture metadata {field_name}",
            )
            if projected is not _DROP:
                assert isinstance(projected, str)
                value[field_name] = projected
    extra = entry.get("extra")
    if extra:
        if not isinstance(extra, Mapping):
            raise ValueError("capture metadata extra must be a JSON object")
        projected_extra = _portable_json_projection(
            extra,
            artifact="capture metadata extra",
        )
        if projected_extra:
            value["extra"] = projected_extra
    category_ids = entry.get("category_ids")
    if category_ids:
        if not isinstance(category_ids, list):
            raise ValueError("capture metadata category_ids must be a JSON array")
        projected_categories = _portable_json_projection(
            category_ids,
            artifact="capture metadata category_ids",
        )
        if projected_categories:
            value["category_ids"] = projected_categories
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
    projected = _portable_json_projection({
        "schema": "org.whl.capture-provenance",
        "version": 1,
        "capture_id": capture_id,
        "transport": str(entry.get("capture_transport") or "unknown")[:32],
        "created_at": str(entry.get("created_at") or "")[:128],
        "snapshot": snapshots,
    }, artifact="capture provenance")
    assert isinstance(projected, dict)
    return projected


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
    directory_snapshot = _capture_directory_snapshot(directory)
    photo_assets = _optional_json(directory, "photo_assets.json")
    pairs: list[tuple[int, bytes, bytes]] = []
    total_image_bytes = 0
    if photo_assets is None:
        originals, displays = _numbered_images(directory, directory_snapshot)
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
                raise ValueError(
                    "capture image resources exceed their total size limit"
                )
            pairs.append((index, original, display))
        photo_assets = _legacy_photo_assets(capture_id, pairs)
    if (
        photo_assets.get("schema") != _PHOTO_ASSETS_SCHEMA
        or photo_assets.get("version") != 1
        or str(photo_assets.get("capture_id") or "") != capture_id
    ):
        raise ValueError("photo_assets.json does not describe this capture")
    assets = photo_assets.get("assets")
    count = len(assets) if isinstance(assets, list) else 0
    asset_rows = _asset_rows(photo_assets, count=count or len(pairs))
    import_rows = _desktop_import_rows(photo_assets, asset_rows=asset_rows)
    if not pairs:
        for index in sorted(asset_rows):
            asset = asset_rows[index]
            imported = import_rows.get(index)
            original_record = asset.get("original")
            display_record = asset.get("display")
            assert isinstance(original_record, Mapping)
            assert isinstance(display_record, Mapping)
            if imported is None:
                original = _read_regular(
                    directory / f"orig_{index}.jpg",
                    maximum=_MAX_CAPTURE_RESOURCE_BYTES,
                    artifact=f"capture original {index}",
                )
                display_ref = asset.get("capture_file")
            else:
                original = _cold_original_bytes(
                    directory,
                    imported,
                    original_record,
                    index=index,
                )
                display_ref = imported.get("display_ref")
                try:
                    _capture_image_reference(
                        directory,
                        display_ref,
                        artifact=f"photo asset {index} display",
                    )
                except ValueError:
                    # Older import evidence sometimes retained an absolute
                    # diagnostic locator. The canonical capture_file remains
                    # the confined local rendition in that contract.
                    display_ref = asset.get("capture_file")
            display = _read_regular(
                _capture_image_reference(
                    directory,
                    display_ref or asset.get("capture_file"),
                    artifact=f"photo asset {index} display",
                ),
                maximum=_MAX_CAPTURE_RESOURCE_BYTES,
                artifact=f"capture display {index}",
            )
            total_image_bytes += len(original) + len(display)
            if total_image_bytes > _MAX_CAPTURE_TOTAL_BYTES:
                raise ValueError(
                    "capture image resources exceed their total size limit"
                )
            pairs.append((index, original, display))
    asset_evidence = {
        index: _asset_evidence(
            index=index,
            asset=asset_rows[index],
            imported=import_rows.get(index),
            original=original,
            display=display,
        )
        for index, original, display in pairs
    }
    portable_photo_assets = _portable_photo_assets(
        capture_id=capture_id,
        photo_assets=photo_assets,
        asset_rows=asset_rows,
        import_rows=import_rows,
        evidence=asset_evidence,
    )
    portable_entry = _portable_entry(entry, capture_id)

    resources: dict[str, bytes] = {}
    representations: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    generated_at = str(entry.get("created_at") or "")[:128]
    first_original_id = ""
    first_original_revision = ""
    display_sources: list[tuple[str, str]] = []
    for index, original, display in pairs:
        proof = asset_evidence[index]
        asset_id = str(proof["asset_id"])
        original_member = f"representations/capture-original-{index}.jpg"
        display_member = f"representations/capture-display-{index}.jpg"
        original_id = f"capture-original-{index}"
        display_id = f"capture-display-{index}"
        original_revision = _revision(original)
        display_revision = _revision(display)
        original_dimensions = proof["original_dimensions"]
        display_dimensions = proof["display_dimensions"]
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
        if not first_original_id:
            first_original_id = original_id
            first_original_revision = original_revision
        display_sources.append((display_id, display_revision))

    aggregate_source_id, aggregate_source_revision = _capture_aggregate_source(
        capture_id=capture_id,
        display_sources=display_sources,
        resources=resources,
        representations=representations,
    )

    def add_json_artifact(
        *,
        artifact_id: str,
        kind: str,
        member: str,
        value: Any,
        source_id: str = aggregate_source_id,
        source_revision: str = aggregate_source_revision,
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
        value=portable_photo_assets,
        source_id=(
            first_original_id if len(pairs) == 1 else aggregate_source_id
        ),
        source_revision=(
            first_original_revision
            if len(pairs) == 1
            else aggregate_source_revision
        ),
    )
    add_json_artifact(
        artifact_id="capture-generated-metadata",
        kind="generated-metadata",
        member="artifacts/generated-metadata.json",
        value=portable_entry,
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
                        portable_photo_assets["assets"][index - 1].get(
                            "geometry"
                        )
                        if isinstance(
                            portable_photo_assets["assets"][index - 1].get(
                                "geometry"
                            ),
                            list,
                        )
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
    elif (
        capture_notes.get("schema") != _CAPTURE_NOTES_SCHEMA
        or capture_notes.get("version") != 1
        or str(capture_notes.get("capture_id") or "") != capture_id
        or not isinstance(capture_notes.get("notes"), list)
    ):
        raise ValueError("capture_notes.json does not describe this capture")
    capture_notes = _portable_json_projection(
        capture_notes,
        artifact="capture_notes.json",
    )
    assert isinstance(capture_notes, dict)
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
                representation_id=aggregate_source_id,
                representation_revision=aggregate_source_revision,
                generated_at=generated_at,
            )
        )

    meta = {
        field_name: portable_entry[field_name]
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
        if field_name in portable_entry
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
    _validate_capture_directory_snapshot(directory, directory_snapshot)
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
    *,
    book_id: str = "",
    operation_id: str = "",
) -> AssociateCaptureArchiveCommand:
    """Build the stable command used by both LAN and cloud import retries."""

    source = build_capture_archive_source(capture_id, entry, capture_directory)
    return AssociateCaptureArchiveCommand(
        source=source,
        operation_id=operation_id or f"capture-import-{source.fingerprint}",
        book_id=book_id,
    )


def _bounded_diagnostic_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:_MAX_DIAGNOSTIC_TEXT]


def _safe_engine_error_diagnostic(
    error: EngineError,
) -> dict[str, Any]:
    cause_code = str(error.code or "")
    if not _DIAGNOSTIC_CODE_RE.fullmatch(cause_code):
        cause_code = "engine_error"
    message = _bounded_diagnostic_text(error)
    if not message or _looks_like_local_locator(message):
        message = "capture cloud update failed"
    return {
        "status": "failed",
        "code": "capture_cloud_update_failed",
        "cause_code": cause_code,
        "message": message,
        "retryable": bool(error.retryable),
    }


def _diagnostic(
    *,
    capture_id: str,
    entry_id: str = "",
    status: str,
    code: str,
    message: str,
    changed: bool = False,
    book_id: str = "",
    association: Mapping[str, Any] | None = None,
    cloud_update: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    capture_identity = _bounded_diagnostic_text(capture_id)
    if (
        capture_identity
        and _portable_capture_id(capture_identity) != capture_identity
    ):
        capture_identity = ""
    entry_identity = _bounded_diagnostic_text(entry_id)
    if entry_identity and not _BUILD_ID_RE.fullmatch(entry_identity):
        entry_identity = ""
    book_identity = _bounded_diagnostic_text(book_id)
    if book_identity and not _BOOK_ID_RE.fullmatch(book_identity):
        book_identity = ""
    diagnostic_message = _bounded_diagnostic_text(message)
    if _looks_like_local_locator(diagnostic_message):
        diagnostic_message = "capture backfill diagnostic withheld"
    diagnostic = {
        "capture_id": capture_identity,
        "entry_id": entry_identity,
        "status": status,
        "code": code,
        "message": diagnostic_message,
        "changed": bool(changed),
        "book_id": book_identity,
        "association": dict(association) if association is not None else None,
    }
    if cloud_update is not None:
        detached_cloud_update = dict(cloud_update)
        diagnostic["cloud_update"] = detached_cloud_update
        diagnostic["local_status"] = status
        diagnostic["overall_status"] = (
            "failed"
            if detached_cloud_update.get("status") == "failed"
            else status
        )
    return diagnostic


def _backfill_setup_diagnostic(error: Exception) -> dict[str, Any]:
    code = (
        str(error.code)
        if isinstance(error, EngineError)
        else "capture_backfill_setup_failed"
    )
    if not _DIAGNOSTIC_CODE_RE.fullmatch(code):
        code = "capture_backfill_setup_failed"
    message = _bounded_diagnostic_text(error)
    if not message or _looks_like_local_locator(message):
        message = "capture backfill setup failed"
    return _diagnostic(
        capture_id="",
        status="failed",
        code=code,
        message=message,
    )


def _portable_capture_id(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        return ""
    try:
        capture_book_id(value)
    except (EngineError, TypeError, ValueError):
        return ""
    return value


def historical_build_book_id(
    build_id: str,
    identity_document: Mapping[str, Any] | None = None,
) -> str:
    """Reproduce the stable identity used by exports before capture archives.

    A persisted ``lib-id.json`` wins. Builds that were exported without one
    historically derived their identity from the local build id, so capture
    association backfills must use that exact UUID5 fallback rather than mint
    a new capture-derived identity.
    """

    if not isinstance(build_id, str) or not _BUILD_ID_RE.fullmatch(build_id):
        raise ValueError("build id is not a portable legacy identity")
    if isinstance(identity_document, Mapping):
        persisted = identity_document.get("book_id")
        if isinstance(persisted, str) and _BOOK_ID_RE.fullmatch(persisted):
            return persisted
    return "b-" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://librarytool.local/items/{build_id}",
    ).hex


def discover_capture_build_identities(
    builds: Mapping[str, Any],
    entries_root: str | Path,
    capture_id: str,
) -> dict[str, str]:
    """Discover every historical build identity attached to one capture.

    The returned source labels make conflicting historical identities
    diagnosable. Invalid capture ids are never normalized into another
    capture's authority.
    """

    if not isinstance(builds, Mapping):
        raise TypeError("builds must be a mapping")
    if _portable_capture_id(capture_id) != capture_id:
        raise ValueError("capture id is not portable")
    root = Path(entries_root)
    identities: dict[str, str] = {}
    for raw_build_id in sorted(builds, key=lambda value: str(value)):
        build = builds[raw_build_id]
        if not isinstance(build, Mapping):
            continue
        if _portable_capture_id(build.get("capture_id")) != capture_id:
            continue
        if not isinstance(raw_build_id, str) or not _BUILD_ID_RE.fullmatch(
            raw_build_id
        ):
            raise ValueError(
                "a matching build has a nonportable legacy identity"
            )
        identity_path = (
            root / raw_build_id / "ocr" / "lib-id.json"
        )
        try:
            identity_path.lstat()
        except FileNotFoundError:
            identity_document = None
        else:
            try:
                resolved_root = root.resolve(strict=True)
                resolved_identity = identity_path.resolve(strict=True)
                resolved_identity.relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise ValueError(
                    f"build {raw_build_id} lib identity escaped entries root"
                ) from exc
            payload = _read_regular(
                identity_path,
                maximum=_MAX_CAPTURE_JSON_BYTES,
                artifact=f"build {raw_build_id} lib identity",
            )
            try:
                if identity_path.resolve(strict=True) != resolved_identity:
                    raise ValueError(
                        f"build {raw_build_id} lib identity changed location"
                    )
            except OSError as exc:
                raise ValueError(
                    f"build {raw_build_id} lib identity changed location"
                ) from exc
            identity_document = _json_object(
                payload,
                artifact=f"build {raw_build_id} lib identity",
            )
        linked_book_id = build.get("capture_book_id")
        if linked_book_id not in (None, ""):
            if (
                not isinstance(linked_book_id, str)
                or not _BOOK_ID_RE.fullmatch(linked_book_id)
            ):
                raise ValueError(
                    f"build {raw_build_id} capture book identity is invalid"
                )
            identities[
                f"build:{raw_build_id}:capture_book_id"
            ] = linked_book_id
            persisted_book_id = (
                identity_document.get("book_id")
                if isinstance(identity_document, Mapping)
                else None
            )
            if persisted_book_id not in (None, ""):
                if (
                    not isinstance(persisted_book_id, str)
                    or not _BOOK_ID_RE.fullmatch(persisted_book_id)
                ):
                    raise ValueError(
                        f"build {raw_build_id} lib identity is invalid"
                    )
                identities[
                    f"build:{raw_build_id}:lib-id"
                ] = persisted_book_id
        else:
            identities[f"build:{raw_build_id}"] = (
                historical_build_book_id(
                    raw_build_id,
                    identity_document,
                )
            )
    return identities


def resolve_legacy_capture_book_id(
    entry: Mapping[str, Any],
    capture_id: str,
    *,
    discovered_identities: Mapping[str, Any] | None = None,
) -> str:
    """Resolve one capture identity without replacing any prior book id."""

    if not isinstance(entry, Mapping):
        raise TypeError("entry must be a mapping")
    if discovered_identities is not None and not isinstance(
        discovered_identities,
        Mapping,
    ):
        raise TypeError("discovered_identities must be a mapping")
    candidates: list[tuple[str, Any]] = [
        ("book_id", entry.get("book_id")),
        ("lib_book_id", entry.get("lib_book_id")),
    ]
    extra = entry.get("extra")
    if isinstance(extra, Mapping):
        candidates.append(("extra.book_id", extra.get("book_id")))
        candidates.append(("extra.lib_book_id", extra.get("lib_book_id")))
    if discovered_identities is not None:
        candidates.extend(
            (str(field_name), raw_value)
            for field_name, raw_value in sorted(
                discovered_identities.items(),
                key=lambda item: str(item[0]),
            )
        )
    identities: dict[str, list[str]] = {}
    for field_name, raw_value in candidates:
        if raw_value in (None, ""):
            continue
        if not isinstance(raw_value, str) or not _BOOK_ID_RE.fullmatch(raw_value):
            raise ValueError(
                f"{field_name} is not a valid stable lib book identity"
            )
        identities.setdefault(raw_value, []).append(field_name)
    if len(identities) > 1:
        raise ValueError("capture has conflicting stable book identities")
    return next(iter(identities), capture_book_id(capture_id))


def _backfill_operation_id(
    *,
    capture_id: str,
    book_id: str,
    source_fingerprint: str,
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "schema": "org.whl.capture-lib-backfill-operation",
                "version": 1,
                "capture_id": capture_id,
                "book_id": book_id,
                "source_fingerprint": source_fingerprint,
            }
        )
    ).hexdigest()
    return f"capture-backfill-{digest}"


def _capture_id_scope(
    values: Iterable[str] | None,
    *,
    field_name: str,
) -> frozenset[str] | None:
    """Validate an optional exact capture-id scope for an embedding host."""

    if values is None:
        return None
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{field_name} must be an iterable of capture ids")
    try:
        candidates = list(values)
    except TypeError as exc:
        raise TypeError(
            f"{field_name} must be an iterable of capture ids"
        ) from exc
    normalized: set[str] = set()
    for value in candidates:
        capture_id = _portable_capture_id(value)
        if not capture_id or capture_id != value:
            raise ValueError(
                f"{field_name} contains a nonportable capture identity"
            )
        normalized.add(capture_id)
    return frozenset(normalized)


def _capture_directories(
    capture_root: Path,
    *,
    capture_ids: frozenset[str] | None = None,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    directories: dict[str, Path] = {}
    diagnostics: list[dict[str, Any]] = []
    try:
        root_info = capture_root.lstat()
    except FileNotFoundError:
        return directories, diagnostics
    except OSError as exc:
        diagnostics.append(
            _diagnostic(
                capture_id="",
                status="failed",
                code="capture_root_unavailable",
                message=f"capture root cannot be inspected: {type(exc).__name__}",
            )
        )
        return directories, diagnostics
    if not stat.S_ISDIR(root_info.st_mode) or capture_root.is_symlink():
        diagnostics.append(
            _diagnostic(
                capture_id="",
                status="failed",
                code="capture_root_invalid",
                message="capture root is redirecting or is not a directory",
            )
        )
        return directories, diagnostics
    if capture_ids is None:
        try:
            children = sorted(capture_root.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            diagnostics.append(
                _diagnostic(
                    capture_id="",
                    status="failed",
                    code="capture_root_unavailable",
                    message=(
                        "capture root cannot be listed: "
                        f"{type(exc).__name__}"
                    ),
                )
            )
            return directories, diagnostics
    else:
        # An embedding host can bound maintenance to named captures without
        # enumerating or diagnosing unrelated private capture directories.
        children = [capture_root / capture_id for capture_id in sorted(capture_ids)]
    for child in children:
        capture_id = _portable_capture_id(child.name)
        if not capture_id:
            continue
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            diagnostics.append(
                _diagnostic(
                    capture_id=capture_id,
                    status="failed",
                    code="capture_asset_directory_unavailable",
                    message=(
                        "capture asset directory cannot be inspected: "
                        f"{type(exc).__name__}"
                    ),
                )
            )
            continue
        if not stat.S_ISDIR(info.st_mode) or child.is_symlink():
            diagnostics.append(
                _diagnostic(
                    capture_id=capture_id,
                    status="failed",
                    code="capture_asset_directory_invalid",
                    message="capture asset directory is redirecting or invalid",
                )
            )
            continue
        directories[capture_id] = child
    return directories, diagnostics


def backfill_capture_archives(
    manual_entries: Mapping[str, Any],
    capture_root: str | Path,
    *,
    materializer: CaptureArchiveMaterializerPort,
    service: CaptureArchiveService | None = None,
    association_lookup: (
        Callable[[str], CaptureArchiveAssociation | None] | None
    ) = None,
    legacy_identity_lookup: (
        Callable[[str], Mapping[str, Any] | None] | None
    ) = None,
    association_publisher: (
        CaptureArchiveAssociationPublisherPort | None
    ) = None,
    capture_ids: Iterable[str] | None = None,
    publication_capture_ids: Iterable[str] | None = None,
    apply: bool = False,
    diagnostic_limit: int = _MAX_BACKFILL_DIAGNOSTICS,
) -> dict[str, Any]:
    """Plan or apply missing capture archive associations.

    The scan is deterministic and independent per capture. Failures are
    reported and skipped, so a later apply resumes from every association that
    already committed. An optional association publisher runs only after a
    local archive is verified; it is retried for an already-associated capture
    after a prior remote failure. Embedding hosts may bound local work with
    ``capture_ids`` and independently restrict publication to an authorized
    subset with ``publication_capture_ids``. The compatibility manual
    catalogue and capture assets are read-only inputs.
    """

    if not isinstance(manual_entries, Mapping):
        raise TypeError("manual_entries must be a mapping")
    if not isinstance(materializer, CaptureArchiveMaterializerPort):
        raise TypeError("materializer must implement CaptureArchiveMaterializerPort")
    if service is not None and not isinstance(service, CaptureArchiveService):
        raise TypeError("service must be a CaptureArchiveService")
    if apply and service is None:
        raise TypeError("apply requires a CaptureArchiveService")
    if association_lookup is not None and not callable(association_lookup):
        raise TypeError("association_lookup must be callable")
    if legacy_identity_lookup is not None and not callable(
        legacy_identity_lookup
    ):
        raise TypeError("legacy_identity_lookup must be callable")
    if (
        association_publisher is not None
        and not isinstance(
            association_publisher,
            CaptureArchiveAssociationPublisherPort,
        )
    ):
        raise TypeError(
            "association_publisher must implement "
            "CaptureArchiveAssociationPublisherPort"
        )
    if not isinstance(apply, bool):
        raise TypeError("apply must be boolean")
    capture_scope = _capture_id_scope(
        capture_ids,
        field_name="capture_ids",
    )
    publication_scope = _capture_id_scope(
        publication_capture_ids,
        field_name="publication_capture_ids",
    )
    if (
        capture_scope is not None
        and publication_scope is not None
        and not publication_scope.issubset(capture_scope)
    ):
        raise ValueError(
            "publication_capture_ids must be a subset of capture_ids"
        )
    if (
        not isinstance(diagnostic_limit, int)
        or isinstance(diagnostic_limit, bool)
        or diagnostic_limit < 1
        or diagnostic_limit > _MAX_BACKFILL_DIAGNOSTICS
    ):
        raise ValueError(
            f"diagnostic_limit must be between 1 and {_MAX_BACKFILL_DIAGNOSTICS}"
        )

    capture_root = Path(capture_root)
    lookup = association_lookup or (
        service.get if service is not None else None
    )
    if lookup is None:
        raise TypeError("an association lookup is required")
    directories, initial_diagnostics = _capture_directories(
        capture_root,
        capture_ids=capture_scope,
    )
    entries_by_capture: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
    pending_diagnostics = list(initial_diagnostics)
    for raw_entry_id in sorted(manual_entries, key=lambda value: str(value)):
        raw_entry = manual_entries[raw_entry_id]
        if not isinstance(raw_entry, Mapping):
            continue
        raw_capture_id = raw_entry.get("capture_id")
        if raw_capture_id in (None, ""):
            continue
        capture_id = _portable_capture_id(raw_capture_id)
        if not capture_id:
            if capture_scope is not None:
                continue
            pending_diagnostics.append(
                _diagnostic(
                    capture_id=_bounded_diagnostic_text(raw_capture_id),
                    entry_id=str(raw_entry_id),
                    status="failed",
                    code="capture_id_invalid",
                    message="manual entry has a nonportable capture identity",
                )
            )
            continue
        if capture_scope is not None and capture_id not in capture_scope:
            continue
        entries_by_capture.setdefault(capture_id, []).append(
            (str(raw_entry_id), raw_entry)
        )

    candidate_ids = sorted(
        capture_scope
        if capture_scope is not None
        else set(entries_by_capture) | set(directories)
    )
    diagnostics: list[dict[str, Any]] = []
    counts = {
        "created": 0,
        "would_create": 0,
        "unchanged": 0,
        "failed": 0,
    }
    cloud_counts = {
        "succeeded": 0,
        "failed": 0,
    }
    omitted = 0

    def record(diagnostic: dict[str, Any]) -> None:
        nonlocal omitted
        status = str(diagnostic["status"])
        counts[status] = counts.get(status, 0) + 1
        if len(diagnostics) < diagnostic_limit:
            diagnostics.append(diagnostic)
        else:
            omitted += 1

    def publish_association(
        association: CaptureArchiveAssociation,
    ) -> dict[str, str] | None:
        nonlocal cloud_counts
        if (
            not apply
            or association_publisher is None
            or (
                publication_scope is not None
                and association.capture_id not in publication_scope
            )
        ):
            return None
        try:
            association_publisher.publish(association)
        except EngineError as exc:
            cloud_counts["failed"] += 1
            return _safe_engine_error_diagnostic(exc)
        except Exception as exc:
            cloud_counts["failed"] += 1
            return {
                "status": "failed",
                "code": "capture_cloud_update_failed",
                "cause_code": "unexpected_publisher_error",
                "message": type(exc).__name__,
            }
        cloud_counts["succeeded"] += 1
        return {
            "status": "succeeded",
            "code": "capture_cloud_update_succeeded",
            "message": "capture cloud association/status is current",
        }

    for diagnostic in pending_diagnostics:
        record(diagnostic)

    for capture_id in candidate_ids:
        rows = entries_by_capture.get(capture_id, [])
        entry_id = rows[0][0] if len(rows) == 1 else ""
        try:
            existing = lookup(capture_id)
        except EngineError as exc:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="failed",
                    code=str(exc.code or "capture_association_invalid"),
                    message=str(exc),
                )
            )
            continue
        if existing is not None:
            cloud_update = publish_association(existing)
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="unchanged",
                    code="capture_archive_already_associated",
                    message="stable capture archive association already exists",
                    book_id=existing.book_id,
                    association=existing.as_dict(),
                    cloud_update=cloud_update,
                )
            )
            continue
        if len(rows) > 1:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    status="failed",
                    code="duplicate_capture_entries",
                    message=(
                        f"{len(rows)} manual entries claim this capture identity"
                    ),
                )
            )
            continue
        if not rows:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    status="failed",
                    code="capture_manual_entry_missing",
                    message="capture directory has no matching manual entry",
                )
            )
            continue
        directory = directories.get(capture_id)
        if directory is None:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="failed",
                    code="capture_assets_missing",
                    message="manual entry has no durable capture asset directory",
                )
            )
            continue
        entry = rows[0][1]
        try:
            discovered_identities = (
                legacy_identity_lookup(capture_id)
                if legacy_identity_lookup is not None
                else None
            )
            book_id = resolve_legacy_capture_book_id(
                entry,
                capture_id,
                discovered_identities=discovered_identities,
            )
        except (OSError, TypeError, ValueError) as exc:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="failed",
                    code="capture_book_identity_invalid",
                    message=str(exc),
                )
            )
            continue
        try:
            source = build_capture_archive_source(
                capture_id,
                entry,
                directory,
            )
            command = AssociateCaptureArchiveCommand(
                source=source,
                operation_id=_backfill_operation_id(
                    capture_id=capture_id,
                    book_id=book_id,
                    source_fingerprint=source.fingerprint,
                ),
                book_id=book_id,
            )
            if not apply:
                materializer.materialize(source, book_id=book_id)
                record(
                    _diagnostic(
                        capture_id=capture_id,
                        entry_id=entry_id,
                        status="would_create",
                        code="capture_archive_missing",
                        message="capture archive association would be created",
                        book_id=book_id,
                    )
                )
                continue
            if service is None:
                raise RuntimeError("apply service is unavailable")
            result = service.associate(command)
        except EngineError as exc:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="failed",
                    code=str(exc.code or "capture_backfill_failed"),
                    message=str(exc),
                    book_id=book_id,
                )
            )
            continue
        except (OSError, TypeError, ValueError) as exc:
            message = str(exc).replace(str(capture_root), "<capture-root>")
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="failed",
                    code="capture_source_invalid",
                    message=message or type(exc).__name__,
                    book_id=book_id,
                )
            )
            continue
        except Exception as exc:
            record(
                _diagnostic(
                    capture_id=capture_id,
                    entry_id=entry_id,
                    status="failed",
                    code="capture_backfill_failed",
                    message=type(exc).__name__,
                    book_id=book_id,
                )
            )
            continue

        association = result.receipt.association
        cloud_update = publish_association(association)
        changed = (
            not result.replayed
            and result.receipt.disposition is CaptureArchiveDisposition.CREATED
        )
        record(
            _diagnostic(
                capture_id=capture_id,
                entry_id=entry_id,
                status="created" if changed else "unchanged",
                code=(
                    "capture_archive_created"
                    if changed
                    else "capture_archive_already_associated"
                ),
                message=(
                    "capture archive association created"
                    if changed
                    else "stable capture archive association already exists"
                ),
                changed=changed,
                book_id=association.book_id,
                association=association.as_dict(),
                cloud_update=cloud_update,
            )
        )

    local_failed = counts.get("failed", 0)
    failed = local_failed + cloud_counts["failed"]
    summary = {
        "total": sum(counts.values()),
        "created": counts.get("created", 0),
        "would_create": counts.get("would_create", 0),
        "unchanged": counts.get("unchanged", 0),
        "failed": failed,
        "omitted_diagnostics": omitted,
    }
    if apply and association_publisher is not None:
        summary.update({
            "local_failed": local_failed,
            "cloud_succeeded": cloud_counts["succeeded"],
            "cloud_failed": cloud_counts["failed"],
        })
    return {
        "schema": "org.whl.capture-lib-backfill-report",
        "version": 1,
        "mode": "apply" if apply else "dry-run",
        "ok": failed == 0,
        "summary": summary,
        "diagnostics": diagnostics,
    }


def _load_manual_entries(path: Path) -> dict[str, Any]:
    try:
        payload = _read_regular(
            path,
            maximum=_MAX_CAPTURE_JSON_BYTES,
            artifact="manual entries catalogue",
        )
    except FileNotFoundError:
        return {}
    return _json_object(payload, artifact="manual entries catalogue")


def _load_builds(path: Path) -> dict[str, Any]:
    try:
        payload = _read_regular(
            path,
            maximum=_MAX_CAPTURE_JSON_BYTES,
            artifact="build catalogue",
        )
    except FileNotFoundError:
        return {}
    return _json_object(payload, artifact="build catalogue")


def run_capture_archive_backfill(
    *,
    manual_entries_path: str | Path,
    capture_root: str | Path,
    workspace_root: str | Path,
    format_module: Any,
    association_publisher: (
        CaptureArchiveAssociationPublisherPort | None
    ) = None,
    capture_ids: Iterable[str] | None = None,
    publication_capture_ids: Iterable[str] | None = None,
    apply: bool = False,
    diagnostic_limit: int = _MAX_BACKFILL_DIAGNOSTICS,
) -> dict[str, Any]:
    """Compose and run the standalone backfill against one local workspace."""

    from librarytool.adapters.capture_lib import Lib3CaptureArchiveMaterializer
    from librarytool.adapters.filesystem.capture_archive_repository import (
        FilesystemCaptureArchiveRepository,
    )
    from librarytool.adapters.filesystem.recoverable_write_set import (
        RecoverableWriteSet,
    )

    manual_entries = _load_manual_entries(Path(manual_entries_path))
    workspace_root = Path(workspace_root)
    builds = _load_builds(workspace_root / "whl_builds.json")
    materializer = Lib3CaptureArchiveMaterializer(
        format_module,
        generator="library-tool/capture-backfill-v1",
    )
    service = None
    association_lookup = None
    if apply:
        write_set = RecoverableWriteSet(Path(workspace_root))
        repository = FilesystemCaptureArchiveRepository(write_set)
        service = CaptureArchiveService(repository, materializer)
    else:
        def inspect_existing(
            capture_id: str,
        ) -> CaptureArchiveAssociation | None:
            return FilesystemCaptureArchiveRepository.inspect_association(
                workspace_root,
                capture_id,
            )

        association_lookup = inspect_existing
    return backfill_capture_archives(
        manual_entries,
        capture_root,
        materializer=materializer,
        service=service,
        association_lookup=association_lookup,
        legacy_identity_lookup=lambda capture_id: (
            discover_capture_build_identities(
                builds,
                workspace_root / "entries",
                capture_id,
            )
        ),
        association_publisher=association_publisher,
        capture_ids=capture_ids,
        publication_capture_ids=publication_capture_ids,
        apply=apply,
        diagnostic_limit=diagnostic_limit,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the machine-readable dry-run/apply capture backfill command."""

    import argparse

    import libcommon as lib
    import libformat

    parser = argparse.ArgumentParser(
        description="Backfill stable .lib/3 associations for legacy captures."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report changes without writing (the default)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="atomically create missing archive associations",
    )
    parser.add_argument(
        "--manual-entries",
        type=Path,
        default=lib.MANUAL_ENTRIES_PATH,
    )
    parser.add_argument(
        "--captures",
        type=Path,
        default=lib.DATA_ROOT / "captures",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=lib.OUTPUT_DIR,
    )
    parser.add_argument(
        "--diagnostic-limit",
        type=int,
        default=_MAX_BACKFILL_DIAGNOSTICS,
    )
    args = parser.parse_args(argv)
    apply = bool(args.apply)
    try:
        report = run_capture_archive_backfill(
            manual_entries_path=args.manual_entries,
            capture_root=args.captures,
            workspace_root=args.workspace,
            format_module=libformat,
            apply=apply,
            diagnostic_limit=args.diagnostic_limit,
        )
    except (EngineError, OSError, TypeError, ValueError) as exc:
        report = {
            "schema": "org.whl.capture-lib-backfill-report",
            "version": 1,
            "mode": "apply" if apply else "dry-run",
            "ok": False,
            "summary": {
                "total": 1,
                "created": 0,
                "would_create": 0,
                "unchanged": 0,
                "failed": 1,
                "omitted_diagnostics": 0,
            },
            "diagnostics": [
                _backfill_setup_diagnostic(exc)
            ],
        }
    print(
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if report["ok"] else 1


__all__ = [
    "backfill_capture_archives",
    "build_capture_archive_command",
    "build_capture_archive_source",
    "discover_capture_build_identities",
    "historical_build_book_id",
    "main",
    "resolve_legacy_capture_book_id",
    "run_capture_archive_backfill",
]


if __name__ == "__main__":
    raise SystemExit(main())
