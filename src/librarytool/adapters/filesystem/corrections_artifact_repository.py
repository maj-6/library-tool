"""Read-only projections of legacy Corrections artifacts.

This adapter deliberately treats the existing Android capture manifest and
Replica/Mistral layout sidecar as private persistence formats.  Public views
contain stable identities copied from those formats, revisioned engine data,
and opaque resource references only. Mutable paths remain private; a trusted
transport receives an immutable verified stream snapshot.

Reads never create identities, repair sidecars, or write inferred metadata.
Records without a persisted identity are omitted instead of being assigned one
as a side effect of inspection.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    ContextManager,
    Protocol,
    TypeAlias,
    runtime_checkable,
)

from PIL import Image, UnidentifiedImageError

from ...engine.errors import EngineError, NotFoundError, RepositoryError, ValidationError
from ...engine.raster_artifacts import (
    IMAGE_CATEGORIES,
    ArtifactFreshness,
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterDimensions,
    RasterLineageRef,
    RasterResourceRef,
    RasterSourceRef,
    ResourceState,
)
from ...engine.spatial_annotations import (
    NormalizedPoint,
    NormalizedPolygonSelector,
    RoleAssignmentOrigin,
    SpatialAnnotationKey,
    SpatialAnnotationProjectorPort,
    SpatialAnnotationView,
    SpatialRoleAssignment,
    SpatialSourceRef,
    project_legacy_rectangle_annotation,
)
from .recoverable_write_set import RecoverableWriteSet, _is_redirecting_path


ItemExists: TypeAlias = Callable[[str], bool]
CaptureIdentityLookup: TypeAlias = Callable[[str], str | None]
DirectoryResolver: TypeAlias = Callable[[str], Path]
RepresentationRevisionLookup: TypeAlias = Callable[[str, str], str | None]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]

PHOTO_ASSETS_SCHEMA = "org.whl.bookcapture.photo-assets"
PHOTO_ASSETS_VERSION = 1
PHOTO_ASSETS_NAME = "photo_assets.json"
MISTRAL_LAYOUT_RELATIVE = ("ocr", "layout.json")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PERSISTED_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FIGURE_NAME_RE = re.compile(r"^(?!\.+$)[\w.\-]{1,120}$")
_RESOURCE_LEAF_RE = re.compile(r"^(?!\.+$)[\w.\-]{1,255}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FIGURE_REFERENCE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_MAX_PHOTO_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_LAYOUT_BYTES = 64 * 1024 * 1024
_MAX_CAPTURE_ASSETS = 4096
_MAX_CAPTURE_GEOMETRIES_PER_ASSET = 64
_MAX_CAPTURE_REGIONS_PER_GEOMETRY = 500
_MAX_CAPTURE_POLYGON_POINTS = 16
_MAX_LAYOUT_PAGES = 100_000
_MAX_PAGE_REGIONS = 20_000
_MAX_FIGURES = 100_000
_RESERVED_ROOT_PARTS = frozenset({".engine", ".librarytool", ".transactions"})
_KNOWN_MEDIA_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
_PIL_MEDIA_TYPES = {
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}
_UNKNOWN_IMAGE_MEDIA_TYPE = "image/unknown"
_PHOTO_ASSET_FIELDS = frozenset(
    {
        "asset_id",
        "capture_order",
        "capture_file",
        "original",
        "display",
        "lifecycle",
        "role",
        "geometry",
        "processing_request",
    }
)
_PHOTO_RENDITION_FIELDS = frozenset(
    {
        "reference",
        "sha256",
        "revision",
        "width",
        "height",
        "orientation",
        "recipe",
        "recipe_version",
        "source_to_display_homography",
    }
)
_REGION_FIELDS = frozenset(
    {
        "id",
        "rid",
        "role",
        "box",
        "order",
        "text",
        "norm",
        "confidence",
        "caption",
        "src_type",
    }
)
_FIGURE_FIELDS = frozenset(
    {
        "page",
        "src_key",
        "x",
        "y",
        "w",
        "h",
        "width",
        "height",
        "sha256",
        "caption",
        "rework_of",
        "proposal_id",
        "ext",
    }
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_revision(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    return f"{prefix}:{digest}"


def _repository_error(
    message: str,
    *,
    code: str,
    item_id: str,
    section: str = "",
    **details: Any,
) -> RepositoryError:
    public: dict[str, Any] = {"item_id": item_id}
    if section:
        public["section"] = section
    public.update(details)
    return RepositoryError(message, code=code, details=public)


def _identifier(value: Any, *, item_id: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _repository_error(
            "the Corrections query contains an invalid identity",
            code="invalid_corrections_artifact_identity",
            item_id=item_id,
            field=field,
        )
    return value


def _persisted_token(
    value: Any,
    *,
    item_id: str,
    field: str,
    code: str,
) -> str:
    if not isinstance(value, str) or not _PERSISTED_TOKEN_RE.fullmatch(value):
        raise _repository_error(
            "a persisted Corrections identity is invalid",
            code=code,
            item_id=item_id,
            field=field,
        )
    return value


def _figure_name(value: Any, *, item_id: str) -> str:
    if not isinstance(value, str) or not _FIGURE_NAME_RE.fullmatch(value):
        raise _repository_error(
            "a persisted Mistral figure name is invalid",
            code="invalid_mistral_layout",
            item_id=item_id,
            field="figure_name",
        )
    return value


def _opaque_identity(namespace: str, *parts: Any) -> str:
    digest = hashlib.sha256(_canonical_bytes(parts)).hexdigest()
    return f"{namespace}:{digest[:40]}"


def _composite_identity(
    *parts: str,
    item_id: str,
    field: str,
    code: str,
) -> str:
    value = ":".join(parts)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise _repository_error(
            "a persisted Corrections identity cannot be represented safely",
            code=code,
            item_id=item_id,
            field=field,
        )
    return value


def _revision(
    value: Any,
    *,
    item_id: str,
    field: str,
    code: str = "invalid_corrections_authority_snapshot",
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(
            not 0x21 <= ord(character) <= 0x7E
            or character in {'"', "\\"}
            for character in value
        )
    ):
        raise _repository_error(
            "a Corrections source revision is invalid",
            code=code,
            item_id=item_id,
            field=field,
        )
    return value


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _non_negative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _confidence(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1:
        return None
    return int(result) if result.is_integer() else result


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        return ""
    return value.casefold()


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


@dataclass(frozen=True, slots=True)
class _AuthorityDirectorySnapshot:
    path: Path
    named: os.stat_result | None


@dataclass(frozen=True, slots=True)
class _AuthoritySnapshot:
    root: Path
    named_root: os.stat_result
    directories: tuple[_AuthorityDirectorySnapshot, ...]


def _windows_normalized_path(value: str) -> str:
    normalized = value.replace("/", "\\")
    if normalized.startswith("\\\\?\\UNC\\"):
        normalized = "\\\\" + normalized[8:]
    elif normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return normalized.rstrip("\\")


def _windows_path_is_below(candidate: str, authority_root: str) -> bool:
    value = _windows_normalized_path(candidate)
    root = _windows_normalized_path(authority_root)
    if len(value) <= len(root) or value[len(root)] != "\\":
        return False
    # These are canonical paths returned by opened handles, not user input.
    # Exact comparison is required because NTFS directories can opt into
    # case-sensitive names where ``root`` and ``ROOT`` are distinct siblings.
    return value[: len(root)] == root


def _windows_descriptor_path(descriptor: int) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final_path.restype = wintypes.DWORD
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    capacity = 32_768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = get_final_path(handle, buffer, capacity, 0)
    if not length or length >= capacity:
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


def _windows_open_directory_guard(path: Path) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        file_read_attributes,
        # Excluding delete sharing is defense in depth. The authority proof
        # below does not rely on sharing semantics: it revalidates the exact
        # canonical root-to-parent handle chain after opening the file.
        share_read | share_write,
        None,
        open_existing,
        flag_backup_semantics | flag_open_reparse_point,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY
            | int(getattr(os, "O_BINARY", 0))
            | int(getattr(os, "O_NOINHERIT", 0)),
        )
    except BaseException:
        close_handle(handle)
        raise


def _open_authorized_descriptor(
    path: Path,
    authority: _AuthoritySnapshot,
) -> int:
    relative = path.relative_to(authority.root)
    if not relative.parts:
        raise OSError("private file must be below its authority root")
    file_flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    file_flags |= int(getattr(os, "O_CLOEXEC", 0))
    file_flags |= int(getattr(os, "O_NOINHERIT", 0))
    file_flags |= int(getattr(os, "O_NOFOLLOW", 0))
    file_flags |= int(getattr(os, "O_NONBLOCK", 0))
    if os.name == "nt":
        guards: list[int] = []
        try:
            for directory in (
                _AuthorityDirectorySnapshot(
                    authority.root,
                    authority.named_root,
                ),
                *authority.directories,
            ):
                if directory.named is None:
                    raise OSError("authority path component appeared during read")
                guard = _windows_open_directory_guard(directory.path)
                guards.append(guard)
                guard_info = os.fstat(guard)
                if (
                    not stat.S_ISDIR(guard_info.st_mode)
                    or not os.path.samestat(guard_info, directory.named)
                    # The no-delete-share guard makes this pathname identity
                    # stable while we reject a junction/reparse point that
                    # raced the earlier lexical inspection.
                    or _is_redirecting_path(directory.path)
                ):
                    raise OSError("authority path component identity changed")
            guard_paths = tuple(
                _windows_descriptor_path(guard) for guard in guards
            )
            if any(
                not _windows_path_is_below(child, parent)
                for parent, child in zip(
                    guard_paths[:-1],
                    guard_paths[1:],
                    strict=True,
                )
            ):
                raise OSError("authority path component escaped its root")
            descriptor = os.open(path, file_flags)
            try:
                guard_paths = tuple(
                    _windows_descriptor_path(guard) for guard in guards
                )
                if any(
                    not _windows_path_is_below(child, parent)
                    for parent, child in zip(
                        guard_paths[:-1],
                        guard_paths[1:],
                        strict=True,
                    )
                ):
                    raise OSError(
                        "authority path component changed while opening"
                    )
                if not _windows_path_is_below(
                    _windows_descriptor_path(descriptor),
                    guard_paths[-1],
                ):
                    raise OSError("opened file escaped its authority parent")
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
        finally:
            for guard in reversed(guards):
                os.close(guard)

    directory_flags = os.O_RDONLY | int(getattr(os, "O_CLOEXEC", 0))
    directory_flags |= int(getattr(os, "O_NOFOLLOW", 0))
    directory_flags |= int(getattr(os, "O_NONBLOCK", 0))
    directory_flags |= int(getattr(os, "O_DIRECTORY", 0))
    if (
        _is_redirecting_path(authority.root)
        or not stat.S_ISDIR(authority.named_root.st_mode)
    ):
        raise OSError("authority root is not a private directory")
    descriptors: list[int] = []
    try:
        current = os.open(authority.root, directory_flags)
        descriptors.append(current)
        root_opened = os.fstat(current)
        if (
            not stat.S_ISDIR(root_opened.st_mode)
            or not os.path.samestat(root_opened, authority.named_root)
        ):
            raise OSError("authority root identity changed")
        directory_parts = relative.parts[:-1]
        if len(directory_parts) != len(authority.directories):
            raise OSError("authority path snapshot is incomplete")
        for part, directory in zip(
            directory_parts,
            authority.directories,
            strict=True,
        ):
            if directory.named is None:
                raise OSError("authority path component appeared during read")
            current = os.open(
                part,
                directory_flags,
                dir_fd=current,
            )
            descriptors.append(current)
            opened_directory = os.fstat(current)
            if (
                not stat.S_ISDIR(opened_directory.st_mode)
                or not os.path.samestat(opened_directory, directory.named)
            ):
                raise OSError("authority path component identity changed")
        return os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=current,
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _open_verified_regular(
    path: Path,
    named_before: os.stat_result,
    *,
    authority: _AuthoritySnapshot,
) -> tuple[int, os.stat_result]:
    descriptor = _open_authorized_descriptor(path, authority)
    try:
        opened = os.fstat(descriptor)
        named_opened = path.lstat()
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named_opened.st_mode)
            or opened.st_nlink != 1
            or named_opened.st_nlink != 1
            or not os.path.samestat(named_before, named_opened)
            or not os.path.samestat(opened, named_opened)
            or _stable_stat_identity(named_before)
            != _stable_stat_identity(named_opened)
        ):
            raise OSError("private file identity changed while it was opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, opened


def _finish_verified_regular(
    path: Path,
    descriptor: int,
    *,
    named_before: os.stat_result,
    opened_before: os.stat_result,
) -> os.stat_result:
    opened_after = os.fstat(descriptor)
    named_after = path.lstat()
    if (
        _is_redirecting_path(path)
        or not stat.S_ISREG(opened_after.st_mode)
        or not stat.S_ISREG(named_after.st_mode)
        or opened_after.st_nlink != 1
        or named_after.st_nlink != 1
        or not os.path.samestat(opened_after, named_after)
        or _stable_stat_identity(opened_before)
        != _stable_stat_identity(opened_after)
        or _stable_stat_identity(named_before)
        != _stable_stat_identity(named_after)
    ):
        raise OSError("private file changed while it was read")
    return named_after


def _orientation(value: Any) -> int:
    # Android persists clockwise degrees while RasterDimensions uses EXIF tags.
    return {0: 1, 90: 6, 180: 3, 270: 8}.get(value, 1)


def _orientation_degrees(value: int) -> int:
    return {1: 0, 6: 90, 3: 180, 8: 270}.get(value, 0)


def _media_type(reference: Any) -> str:
    if not isinstance(reference, str):
        return ""
    return _KNOWN_MEDIA_TYPES.get(Path(reference).suffix.casefold(), "")


def _verified_image_properties(stream: BinaryIO) -> tuple[int, int, str] | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            stream.seek(0)
            with Image.open(stream) as image:
                width, height = image.size
                image_format = str(image.format or "").upper()
                image.verify()
            # ``verify`` checks structure without decoding pixels. Reopen and
            # load once so an AVAILABLE grant always names fully decodable
            # raster bytes, not merely a plausible header.
            stream.seek(0)
            with Image.open(stream) as image:
                image.load()
                if image.size != (width, height):
                    return None
                if str(image.format or "").upper() != image_format:
                    return None
        media_type = _PIL_MEDIA_TYPES.get(image_format, "")
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None
    if width <= 0 or height <= 0 or not media_type:
        return None
    return int(width), int(height), media_type


def _public_extensions(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return a bounded public view of permissive legacy metadata.

    Legacy stores intentionally retain a wider JSON extension contract than
    the engine exposes. Keep safe metadata verbatim; otherwise publish only a
    deterministic quarantine receipt while leaving the source sidecar
    untouched.
    """

    try:
        ArtifactProvenance(extensions=value)
    except ValidationError:
        encoded = _canonical_bytes(value)
        return {
            "quarantine": {
                "reason": "legacy-extension-not-public",
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "encoded_bytes": len(encoded),
                "top_level_fields": len(value),
            }
        }
    return value


def _public_text(value: Any, *, maximum: int) -> str:
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


def _public_provider_id(value: Any) -> str:
    text = _public_text(value, maximum=127)
    return text if _IDENTIFIER_RE.fullmatch(text) else ""


def _capture_region_role(value: Any) -> str | None:
    text = _public_text(value, maximum=80).strip().casefold()
    aliases = {
        "ill": "figure",
        "illustration": "figure",
        "mar": "marginalia",
    }
    if text in aliases:
        return aliases[text]
    return text if _IDENTIFIER_RE.fullmatch(text) else None


def _unknown_fields(value: Mapping[str, Any], known: frozenset[str]) -> dict[str, Any]:
    return {str(key): item for key, item in value.items() if key not in known}


@dataclass(frozen=True, slots=True)
class ResolvedRasterResource:
    """Immutable verified stream for a trusted transport adapter.

    The stream is a private temporary snapshot, not the mutable source file.
    A transport owns and must close it after the response. This record is
    intentionally absent from engine serialization; browsers receive only the
    corresponding :class:`RasterResourceRef`.
    """

    stream: BinaryIO
    media_type: str
    content_sha256: str
    size: int
    revision: str


@dataclass(frozen=True, slots=True)
class _ResolvedRasterCandidate:
    path: Path
    file_identity: tuple[int, ...]
    section: str
    media_type: str
    content_sha256: str
    size: int
    revision: str


@runtime_checkable
class FilesystemRasterResourceResolverPort(Protocol):
    """Resolve one item-scoped opaque reference after revalidation."""

    def resolve_raster_resource(
        self,
        item_id: str,
        resource: RasterResourceRef,
    ) -> ResolvedRasterResource | None: ...


@dataclass(frozen=True, slots=True)
class _ResourceObservation:
    state: ResourceState
    media_type: str
    content_sha256: str
    dimensions: RasterDimensions
    resolved: _ResolvedRasterCandidate | None
    integrity_mismatch: bool = False


@dataclass(frozen=True, slots=True)
class _Projection:
    raster_artifacts: tuple[RasterArtifactView, ...]
    spatial_annotations: tuple[SpatialAnnotationView, ...]
    resources: Mapping[tuple[str, str, str], _ResolvedRasterCandidate]


@dataclass(frozen=True, slots=True)
class _FigureDraft:
    name: str
    artifact_id: str
    revision: str
    source: RasterSourceRef
    observation: _ResourceObservation
    info: Mapping[str, Any]
    selector: NormalizedPolygonSelector | None
    annotation_id: str
    annotation_revision: str
    caption: CaptionAssertion | None
    rework_of: str


class FilesystemCorrectionsArtifactRepository(
    RasterArtifactProjectorPort,
    SpatialAnnotationProjectorPort,
    FilesystemRasterResourceResolverPort,
):
    """Project capture and Mistral stores without exposing their paths."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_exists: ItemExists,
        capture_id_for: CaptureIdentityLookup,
        entry_directory_for: DirectoryResolver,
        capture_directory_for: DirectoryResolver,
        capture_authority_root: Path | None = None,
        representation_revision_for: RepresentationRevisionLookup,
        lock_context_for: LockContextFactory,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (item_exists, "item_exists"),
            (capture_id_for, "capture_id_for"),
            (entry_directory_for, "entry_directory_for"),
            (capture_directory_for, "capture_directory_for"),
            (representation_revision_for, "representation_revision_for"),
            (lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._item_exists = item_exists
        self._capture_id_for = capture_id_for
        self._entry_directory_for = entry_directory_for
        self._capture_directory_for = capture_directory_for
        if capture_authority_root is None:
            self._capture_authority_root = write_set.root
        else:
            configured_capture_root = Path(capture_authority_root)
            if not configured_capture_root.is_absolute():
                raise ValueError("capture_authority_root must be absolute")
            self._capture_authority_root = Path(
                os.path.abspath(configured_capture_root)
            )
        self._representation_revision_for = representation_revision_for
        self._lock_context_for = lock_context_for

    def list_raster_artifacts(
        self,
        item_id: str,
    ) -> tuple[RasterArtifactView, ...]:
        return self._project(item_id).raster_artifacts

    def get_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be RasterArtifactKey")
        return next(
            (
                artifact
                for artifact in self.list_raster_artifacts(key.item_id)
                if artifact.key == key
            ),
            None,
        )

    def list_spatial_annotations(
        self,
        item_id: str,
        *,
        representation_id: str = "",
        canvas_id: str = "",
    ) -> tuple[SpatialAnnotationView, ...]:
        if representation_id:
            _identifier(
                representation_id,
                item_id=str(item_id or ""),
                field="representation_id",
            )
        if canvas_id:
            _identifier(canvas_id, item_id=str(item_id or ""), field="canvas_id")
        values = self._project(item_id).spatial_annotations
        return tuple(
            value
            for value in values
            if (
                not representation_id
                or value.source.representation_id == representation_id
            )
            and (not canvas_id or value.source.canvas_id == canvas_id)
        )

    def get_spatial_annotation(
        self,
        key: SpatialAnnotationKey,
    ) -> SpatialAnnotationView | None:
        if not isinstance(key, SpatialAnnotationKey):
            raise TypeError("key must be SpatialAnnotationKey")
        return next(
            (
                annotation
                for annotation in self.list_spatial_annotations(key.item_id)
                if annotation.key == key
            ),
            None,
        )

    def resolve_raster_resource(
        self,
        item_id: str,
        resource: RasterResourceRef,
    ) -> ResolvedRasterResource | None:
        if not isinstance(resource, RasterResourceRef):
            raise TypeError("resource must be RasterResourceRef")
        projection = self._project(item_id)
        candidate = projection.resources.get(
            (resource.resource_id, resource.revision, resource.variant)
        )
        if candidate is None:
            return None
        try:
            snapshot = tempfile.TemporaryFile(mode="w+b")
        except OSError:
            return None
        digest = hashlib.sha256()
        size = 0
        granted = False
        descriptor = -1
        try:
            authority = self._assert_safe_path(
                candidate.path,
                item_id=item_id,
                section=candidate.section,
            )
            named = candidate.path.lstat()
            if (
                _is_redirecting_path(candidate.path)
                or _stable_stat_identity(named) != candidate.file_identity
            ):
                return None
            descriptor, opened = _open_verified_regular(
                candidate.path,
                named,
                authority=authority,
            )
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                snapshot.write(block)
            _finish_verified_regular(
                candidate.path,
                descriptor,
                named_before=named,
                opened_before=opened,
            )
            self._assert_safe_path(
                candidate.path,
                item_id=item_id,
                section=candidate.section,
            )
            if (
                size != candidate.size
                or digest.hexdigest() != candidate.content_sha256
            ):
                return None
            snapshot.seek(0)
            resolved = ResolvedRasterResource(
                stream=snapshot,
                media_type=candidate.media_type,
                content_sha256=candidate.content_sha256,
                size=size,
                revision=candidate.revision,
            )
            granted = True
            return resolved
        except (OSError, RepositoryError):
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if not granted:
                snapshot.close()

    def _project(self, item_id: str) -> _Projection:
        item = _identifier(
            item_id,
            item_id=str(item_id or ""),
            field="item_id",
        )
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    if not self._live_item_exists(item):
                        raise NotFoundError(
                            "the item does not exist",
                            code="item_not_found",
                            details={"item_id": item},
                        )
                    entry_directory = self._managed_directory(
                        self._entry_directory_for,
                        item,
                        item_id=item,
                        section="entry",
                        authority_root=self._write_set.root,
                    )
                    capture_id = self._live_capture_id(item)
                    capture_directory = (
                        self._managed_directory(
                            self._capture_directory_for,
                            capture_id,
                            item_id=item,
                            section="capture",
                            authority_root=self._capture_authority_root,
                        )
                        if capture_id
                        else None
                    )
                    return self._project_locked(
                        item,
                        entry_directory=entry_directory,
                        capture_id=capture_id,
                        capture_directory=capture_directory,
                    )
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the Corrections artifact repository is unavailable",
                code="corrections_artifact_repository_unavailable",
                item_id=item,
                cause_type=type(exc).__name__,
            ) from exc

    def _live_item_exists(self, item_id: str) -> bool:
        try:
            result = self._item_exists(item_id)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the live item catalogue could not be queried",
                code="corrections_artifact_repository_unavailable",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if not isinstance(result, bool):
            raise _repository_error(
                "the live item catalogue returned invalid state",
                code="invalid_corrections_authority_snapshot",
                item_id=item_id,
                field="item_exists",
            )
        return result

    def _live_capture_id(self, item_id: str) -> str:
        try:
            result = self._capture_id_for(item_id)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the capture identity could not be queried",
                code="corrections_artifact_repository_unavailable",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if result in (None, ""):
            return ""
        return _persisted_token(
            result,
            item_id=item_id,
            field="capture_id",
            code="invalid_corrections_authority_snapshot",
        )

    def _live_representation_revision(
        self,
        item_id: str,
        representation_id: str,
    ) -> str | None:
        try:
            result = self._representation_revision_for(
                item_id,
                representation_id,
            )
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the representation authority could not be queried",
                code="corrections_artifact_repository_unavailable",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if result is None:
            return None
        return _revision(
            result,
            item_id=item_id,
            field="representation_revision",
        )

    def _managed_directory(
        self,
        resolver: DirectoryResolver,
        identity: str,
        *,
        item_id: str,
        section: str,
        authority_root: Path,
    ) -> Path:
        try:
            configured = Path(resolver(identity))
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "a Corrections store directory is invalid",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            not configured.parts
            or any(part in {"", ".", ".."} for part in configured.parts)
        ):
            raise _repository_error(
                "a Corrections store directory is invalid",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            )
        candidate = (
            configured
            if configured.is_absolute()
            else authority_root / configured
        )
        lexical = Path(os.path.abspath(candidate))
        try:
            relative = lexical.relative_to(authority_root)
        except ValueError as exc:
            raise _repository_error(
                "a Corrections store directory escapes the workspace",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            ) from exc
        if (
            not relative.parts
            or relative.parts[0].casefold() in _RESERVED_ROOT_PARTS
        ):
            raise _repository_error(
                "a Corrections store directory uses a reserved workspace path",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            )
        self._assert_safe_path(
            lexical,
            item_id=item_id,
            section=section,
            authority_root=authority_root,
        )
        if lexical.exists() and (
            _is_redirecting_path(lexical) or not lexical.is_dir()
        ):
            raise _repository_error(
                "a Corrections store is not a private directory",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            )
        return lexical

    def _assert_safe_path(
        self,
        path: Path,
        *,
        item_id: str,
        section: str,
        authority_root: Path | None = None,
    ) -> _AuthoritySnapshot:
        root = (
            self._authority_root_for(section)
            if authority_root is None
            else authority_root
        )
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise _repository_error(
                "a Corrections store path escapes the workspace",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            ) from exc
        try:
            named_root = root.lstat()
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise _repository_error(
                "a Corrections store root cannot be inspected",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            _is_redirecting_path(root)
            or not stat.S_ISDIR(named_root.st_mode)
        ):
            raise _repository_error(
                "a Corrections store root redirects outside its authority",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            )
        directory_snapshots: list[_AuthorityDirectorySnapshot] = []
        current = root
        for index, part in enumerate(relative.parts):
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a Corrections store path redirects outside its authority",
                    code="unsafe_corrections_store_path",
                    item_id=item_id,
                    section=section,
                )
            if index >= len(relative.parts) - 1:
                continue
            try:
                named_directory = current.lstat()
            except FileNotFoundError:
                named_directory = None
            except OSError as exc:
                raise _repository_error(
                    "a Corrections store path cannot be inspected",
                    code="unsafe_corrections_store_path",
                    item_id=item_id,
                    section=section,
                    cause_type=type(exc).__name__,
                ) from exc
            if (
                named_directory is not None
                and not stat.S_ISDIR(named_directory.st_mode)
            ):
                raise _repository_error(
                    "a Corrections store path component is not a directory",
                    code="unsafe_corrections_store_path",
                    item_id=item_id,
                    section=section,
                )
            directory_snapshots.append(
                _AuthorityDirectorySnapshot(current, named_directory)
            )
        try:
            path.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a Corrections store path escapes the workspace",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            ) from exc
        return _AuthoritySnapshot(
            root,
            named_root,
            tuple(directory_snapshots),
        )

    def _authority_root_for(self, section: str) -> Path:
        return (
            self._capture_authority_root
            if section == "capture"
            else self._write_set.root
        )

    def _read_json(
        self,
        path: Path,
        *,
        item_id: str,
        section: str,
        maximum_bytes: int,
    ) -> Mapping[str, Any] | None:
        authority = self._assert_safe_path(
            path,
            item_id=item_id,
            section=section,
        )
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _repository_error(
                "a Corrections sidecar cannot be inspected",
                code="corrections_artifact_repository_unavailable",
                item_id=item_id,
                section=section,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            raise _repository_error(
                "a Corrections sidecar is not a private regular file",
                code="unsafe_corrections_store_path",
                item_id=item_id,
                section=section,
            )
        descriptor = -1
        try:
            descriptor, opened = _open_verified_regular(
                path,
                info,
                authority=authority,
            )
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            _finish_verified_regular(
                path,
                descriptor,
                named_before=info,
                opened_before=opened,
            )
            self._assert_safe_path(path, item_id=item_id, section=section)
            if len(encoded) > maximum_bytes:
                raise ValueError("sidecar exceeds its size limit")
            value = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise _repository_error(
                "a Corrections sidecar cannot be decoded",
                code=(
                    "invalid_capture_photo_assets"
                    if section == "capture"
                    else "invalid_mistral_layout"
                ),
                item_id=item_id,
                section=section,
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, Mapping):
            raise _repository_error(
                "a Corrections sidecar must contain an object",
                code=(
                    "invalid_capture_photo_assets"
                    if section == "capture"
                    else "invalid_mistral_layout"
                ),
                item_id=item_id,
                section=section,
            )
        return value

    def _project_locked(
        self,
        item_id: str,
        *,
        entry_directory: Path,
        capture_id: str,
        capture_directory: Path | None,
    ) -> _Projection:
        raster: list[RasterArtifactView] = []
        spatial: list[SpatialAnnotationView] = []
        resources: dict[tuple[str, str, str], _ResolvedRasterCandidate] = {}

        if capture_directory is not None:
            photo_assets = self._read_json(
                capture_directory / PHOTO_ASSETS_NAME,
                item_id=item_id,
                section="capture",
                maximum_bytes=_MAX_PHOTO_MANIFEST_BYTES,
            )
            if photo_assets is not None:
                (
                    capture_views,
                    capture_spatial,
                    capture_resources,
                ) = self._project_capture(
                    item_id,
                    capture_id,
                    capture_directory,
                    photo_assets,
                )
                raster.extend(capture_views)
                spatial.extend(capture_spatial)
                resources.update(capture_resources)

        layout_path = entry_directory.joinpath(*MISTRAL_LAYOUT_RELATIVE)
        layout = self._read_json(
            layout_path,
            item_id=item_id,
            section="layout",
            maximum_bytes=_MAX_LAYOUT_BYTES,
        )
        if layout is not None:
            layout_raster, layout_spatial, layout_resources = self._project_layout(
                item_id,
                entry_directory,
                layout,
            )
            raster.extend(layout_raster)
            spatial.extend(layout_spatial)
            resources.update(layout_resources)

        raster.sort(key=lambda value: value.key.artifact_id)
        spatial.sort(key=lambda value: value.key.annotation_id)
        self._unique_projected_ids(
            (value.key.artifact_id for value in raster),
            item_id=item_id,
            field="artifact_id",
        )
        self._unique_projected_ids(
            (value.key.annotation_id for value in spatial),
            item_id=item_id,
            field="annotation_id",
        )
        return _Projection(tuple(raster), tuple(spatial), resources)

    def _unique_projected_ids(
        self,
        values: Sequence[str] | Any,
        *,
        item_id: str,
        field: str,
    ) -> None:
        identities = list(values)
        if len(identities) != len(set(identities)):
            raise _repository_error(
                "the Corrections stores contain duplicate projected identities",
                code="duplicate_corrections_artifact_identity",
                item_id=item_id,
                field=field,
            )

    def _project_capture(
        self,
        item_id: str,
        capture_id: str,
        directory: Path,
        manifest: Mapping[str, Any],
    ) -> tuple[
        tuple[RasterArtifactView, ...],
        tuple[SpatialAnnotationView, ...],
        Mapping[tuple[str, str, str], _ResolvedRasterCandidate],
    ]:
        if (
            manifest.get("schema") != PHOTO_ASSETS_SCHEMA
            or manifest.get("version") != PHOTO_ASSETS_VERSION
            or isinstance(manifest.get("version"), bool)
            or manifest.get("capture_id") != capture_id
        ):
            raise _repository_error(
                "the Android photo asset contract is unsupported",
                code="unsupported_capture_photo_assets",
                item_id=item_id,
                section="capture",
            )
        assets = manifest.get("assets")
        if (
            isinstance(assets, (str, bytes))
            or not isinstance(assets, Sequence)
            or len(assets) > _MAX_CAPTURE_ASSETS
        ):
            raise _repository_error(
                "the Android photo asset list is invalid",
                code="invalid_capture_photo_assets",
                item_id=item_id,
                section="capture",
            )
        import_rows: dict[str, Mapping[str, Any]] = {}
        desktop_import = manifest.get("desktop_import")
        if isinstance(desktop_import, Mapping):
            rows = desktop_import.get("assets")
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    asset_id = row.get("asset_id")
                    if isinstance(asset_id, str) and asset_id not in import_rows:
                        import_rows[asset_id] = row

        manifest_source = []
        for raw in assets:
            if isinstance(raw, Mapping):
                original = raw.get("original")
                if isinstance(original, Mapping):
                    manifest_source.append(
                        {
                            "asset_id": raw.get("asset_id"),
                            "sha256": original.get("sha256"),
                            "revision": original.get("revision"),
                        }
                    )
        representation_revision = _digest_revision(
            "capture",
            {"capture_id": capture_id, "originals": manifest_source},
        )
        values: list[RasterArtifactView] = []
        spatial: list[SpatialAnnotationView] = []
        resources: dict[tuple[str, str, str], _ResolvedRasterCandidate] = {}
        seen_assets: set[str] = set()
        seen_orders: set[int] = set()
        for raw in assets:
            if not isinstance(raw, Mapping):
                raise _repository_error(
                    "an Android photo asset is not an object",
                    code="invalid_capture_photo_assets",
                    item_id=item_id,
                    section="capture",
                )
            asset_id = _persisted_token(
                raw.get("asset_id"),
                item_id=item_id,
                field="asset_id",
                code="invalid_capture_photo_assets",
            )
            order = _positive_integer(raw.get("capture_order"))
            if (
                order is None
                or asset_id in seen_assets
                or order in seen_orders
            ):
                raise _repository_error(
                    "Android photo identities and orders must be unique",
                    code="invalid_capture_photo_assets",
                    item_id=item_id,
                    section="capture",
                )
            seen_assets.add(asset_id)
            seen_orders.add(order)
            original = raw.get("original")
            display = raw.get("display")
            if not isinstance(original, Mapping) or not isinstance(display, Mapping):
                raise _repository_error(
                    "an Android photo asset has invalid renditions",
                    code="invalid_capture_photo_assets",
                    item_id=item_id,
                    section="capture",
                )
            imported = import_rows.get(asset_id, {})
            original_ref = imported.get("raw_ref") or original.get("reference")
            display_ref = imported.get("display_ref") or display.get("reference")
            original_sha = _sha256(
                imported.get("source_checksum") or original.get("sha256")
            )
            display_sha = _sha256(
                imported.get("derivative_checksum") or display.get("sha256")
            )
            capture_namespace = _opaque_identity(
                "capture",
                capture_id,
                asset_id,
            )
            original_id = f"{capture_namespace}:original"
            display_id = f"{capture_namespace}:display"
            canvas_id = capture_namespace
            original_observation = self._observe_resource(
                item_id,
                directory,
                original_ref,
                artifact_id=original_id,
                variant="original",
                declared_sha256=original_sha,
                declared_dimensions=(
                    _positive_integer(original.get("width")),
                    _positive_integer(original.get("height")),
                ),
                orientation=_orientation(original.get("orientation")),
                section="capture",
            )
            display_observation = self._observe_resource(
                item_id,
                directory,
                display_ref,
                artifact_id=display_id,
                variant="display",
                declared_sha256=display_sha,
                declared_dimensions=(
                    _positive_integer(display.get("width")),
                    _positive_integer(display.get("height")),
                ),
                orientation=(
                    1
                    if imported
                    else _orientation(display.get("orientation"))
                ),
                section="capture",
            )
            original_source = (
                RasterSourceRef(
                    "capture",
                    representation_revision,
                    canvas_id,
                    _digest_revision(
                        "canvas",
                        {
                            "asset_id": asset_id,
                            "rendition": "original",
                            "record_revision": original.get("revision"),
                            "content_sha256": (
                                original_observation.content_sha256
                            ),
                            "dimensions": (
                                original_observation.dimensions.as_dict()
                            ),
                        },
                    ),
                )
                if original_observation is not None
                else None
            )
            display_source = (
                RasterSourceRef(
                    "capture",
                    representation_revision,
                    canvas_id,
                    _digest_revision(
                        "canvas",
                        {
                            "asset_id": asset_id,
                            "rendition": "display",
                            "record_revision": display.get("revision"),
                            "content_sha256": (
                                display_observation.content_sha256
                            ),
                            "dimensions": (
                                display_observation.dimensions.as_dict()
                            ),
                            "source_revision": original.get("revision"),
                            "source_sha256": original_sha,
                        },
                    ),
                )
                if display_observation is not None
                else None
            )
            assignments = self._capture_assignments(
                item_id,
                raw.get("role"),
                asset_id=asset_id,
            )
            provenance = ArtifactProvenance(
                origin="capture",
                provider_id="android",
                model="bookcapture",
            )
            original_view = (
                self._capture_view(
                    item_id,
                    artifact_id=original_id,
                    kind="captured-image",
                    observation=original_observation,
                    source=original_source,
                    label=f"Capture {order} original",
                    freshness=self._capture_freshness(
                        raw,
                        original_observation,
                    ),
                    assignments=assignments,
                    provenance=provenance,
                    lineage=(),
                    extensions={
                        "capture_order": order,
                        "corrections_ui": {"annotation_frame": "canvas"},
                        "android": _unknown_fields(raw, _PHOTO_ASSET_FIELDS),
                        "rendition": _unknown_fields(
                            original,
                            _PHOTO_RENDITION_FIELDS,
                        ),
                    },
                )
                if original_observation is not None
                and original_source is not None
                else None
            )
            display_lineage = (
                (
                    RasterLineageRef(
                        original_id,
                        original_view.revision,
                        "derived_from",
                    ),
                )
                if original_view is not None
                else ()
            )
            recipe = _public_text(
                imported.get("recipe")
                or display.get("recipe")
                or "camera-original",
                maximum=256,
            )
            recipe_revision = _public_text(
                str(display.get("recipe_version") or "1"),
                maximum=512,
            )
            display_provenance = ArtifactProvenance(
                origin="transform" if recipe != "camera-original" else "capture",
                provider_id="desktop" if imported else "android",
                model=recipe,
                recipe_revision=(
                    recipe_revision
                    if recipe_revision
                    and all(
                        0x21 <= ord(character) <= 0x7E
                        and character not in {'"', "\\"}
                        for character in recipe_revision
                    )
                    else ""
                ),
            )
            display_view = (
                self._capture_view(
                    item_id,
                    artifact_id=display_id,
                    kind=(
                        "processed-image"
                        if (
                            imported
                            or recipe != "camera-original"
                            or (
                                original_observation is not None
                                and display_observation.content_sha256
                                != original_observation.content_sha256
                            )
                        )
                        else "captured-image"
                    ),
                    observation=display_observation,
                    source=display_source,
                    label=f"Capture {order} display",
                    freshness=self._capture_freshness(
                        raw,
                        display_observation,
                    ),
                    assignments=assignments,
                    provenance=display_provenance,
                    lineage=display_lineage,
                    extensions={
                        "capture_order": order,
                        "recipe": recipe,
                        "corrections_ui": {"annotation_frame": "canvas"},
                        "android": _unknown_fields(raw, _PHOTO_ASSET_FIELDS),
                        "rendition": _unknown_fields(
                            display,
                            _PHOTO_RENDITION_FIELDS,
                        ),
                    },
                )
                if display_observation is not None
                and display_source is not None
                else None
            )
            values.extend(
                view
                for view in (original_view, display_view)
                if view is not None
            )
            for view, observation in (
                (original_view, original_observation),
                (display_view, display_observation),
            ):
                if view is None or observation is None:
                    continue
                if view.resource is not None and observation.resolved is not None:
                    resources[
                        (
                            view.resource.resource_id,
                            view.resource.revision,
                            view.resource.variant,
                        )
                    ] = observation.resolved
            if (
                display_view is not None
                and display_observation is not None
                and display_source is not None
                and display_observation.state is ResourceState.AVAILABLE
                and (
                    not imported
                    or (
                        bool(_sha256(display.get("sha256")))
                        and display_observation.content_sha256
                        == _sha256(display.get("sha256"))
                    )
                )
            ):
                spatial.extend(
                    self._capture_geometry_annotations(
                        item_id,
                        capture_id=capture_id,
                        asset_id=asset_id,
                        raw_geometry=raw.get("geometry"),
                        original=original,
                        display=display,
                        original_sha256=_sha256(original.get("sha256")),
                        display_dimensions=display_observation.dimensions,
                        source=SpatialSourceRef(
                            display_source.representation_id,
                            display_source.representation_revision,
                            display_source.canvas_id,
                            display_source.canvas_revision,
                        ),
                        display_artifact_id=display_id,
                    )
                )
        return tuple(values), tuple(spatial), resources

    def _capture_geometry_annotations(
        self,
        item_id: str,
        *,
        capture_id: str,
        asset_id: str,
        raw_geometry: Any,
        original: Mapping[str, Any],
        display: Mapping[str, Any],
        original_sha256: str,
        display_dimensions: RasterDimensions,
        source: SpatialSourceRef,
        display_artifact_id: str,
    ) -> tuple[SpatialAnnotationView, ...]:
        if (
            isinstance(raw_geometry, (str, bytes))
            or not isinstance(raw_geometry, Sequence)
        ):
            return ()
        original_revision = _positive_integer(original.get("revision"))
        display_revision = _positive_integer(display.get("revision"))
        declared_display_width = _non_negative_integer(display.get("width")) or 0
        declared_display_height = _non_negative_integer(display.get("height")) or 0
        display_width = display_dimensions.width
        display_height = display_dimensions.height
        display_orientation = _orientation_degrees(
            display_dimensions.orientation
        )
        if original_revision is None or display_revision is None:
            return ()

        values: list[SpatialAnnotationView] = []
        seen_ids: set[str] = set()
        geometries = [
            geometry
            for geometry in raw_geometry[:_MAX_CAPTURE_GEOMETRIES_PER_ASSET]
            if isinstance(geometry, Mapping)
        ]
        geometries.sort(
            key=lambda geometry: (
                _public_text(geometry.get("engine"), maximum=80),
                _public_text(geometry.get("model"), maximum=120),
            )
        )
        for geometry_index, geometry in enumerate(geometries):
            geometry_width = _non_negative_integer(geometry.get("width")) or 0
            geometry_height = _non_negative_integer(geometry.get("height")) or 0
            geometry_sha256 = _sha256(geometry.get("source_sha256"))
            if (
                geometry.get("asset_id") != asset_id
                or geometry.get("coordinate_space") != "display_normalized"
                or _positive_integer(geometry.get("source_revision"))
                != original_revision
                or _positive_integer(geometry.get("display_revision"))
                != display_revision
                or (original_sha256 and geometry_sha256 != original_sha256)
                or (
                    declared_display_width
                    and geometry_width
                    and geometry_width != declared_display_width
                )
                or (
                    declared_display_height
                    and geometry_height
                    and geometry_height != declared_display_height
                )
                or (
                    display_width
                    and geometry_width
                    and geometry_width != display_width
                )
                or (
                    display_height
                    and geometry_height
                    and geometry_height != display_height
                )
                or geometry.get("orientation") != display_orientation
            ):
                continue
            regions = geometry.get("regions")
            if (
                isinstance(regions, (str, bytes))
                or not isinstance(regions, Sequence)
            ):
                continue
            engine = _public_text(geometry.get("engine"), maximum=80)
            model = _public_text(geometry.get("model"), maximum=120)
            engine_version = _public_text(
                geometry.get("engine_version"),
                maximum=80,
            )
            provenance = ArtifactProvenance(
                origin="ocr",
                provider_id=_public_provider_id(engine),
                model=model,
            )
            for region_index, region in enumerate(
                regions[:_MAX_CAPTURE_REGIONS_PER_GEOMETRY]
            ):
                if not isinstance(region, Mapping):
                    continue
                region_id = _public_text(region.get("id"), maximum=120).strip()
                polygon = region.get("polygon")
                if (
                    not region_id
                    or isinstance(polygon, (str, bytes))
                    or not isinstance(polygon, Sequence)
                    or not 3
                    <= len(polygon)
                    <= _MAX_CAPTURE_POLYGON_POINTS
                ):
                    continue
                try:
                    points = tuple(
                        NormalizedPoint(point[0], point[1])
                        for point in polygon
                        if (
                            isinstance(point, Sequence)
                            and not isinstance(point, (str, bytes))
                            and len(point) >= 2
                        )
                    )
                    if len(points) != len(polygon):
                        continue
                    selector = NormalizedPolygonSelector(
                        "display_normalized",
                        source.canvas_revision,
                        points,
                    )
                except ValidationError:
                    continue
                identity_payload = {
                    "capture_id": capture_id,
                    "asset_id": asset_id,
                    "coordinate_space": "display_normalized",
                    "engine": engine,
                    "model": model,
                    "region_id": region_id,
                }
                annotation_id = (
                    "capture-region:"
                    + hashlib.sha256(
                        _canonical_bytes(identity_payload)
                    ).hexdigest()[:40]
                )
                if annotation_id in seen_ids:
                    continue
                seen_ids.add(annotation_id)
                provider_type = _public_text(
                    region.get("type"),
                    maximum=80,
                )
                role = _capture_region_role(provider_type)
                confidence = _confidence(region.get("confidence"))
                text = _public_text(region.get("text"), maximum=500)
                extensions = _public_extensions(
                    {
                        "text": text,
                        "android_geometry": {
                            "region_id": region_id,
                            "provider_type": provider_type,
                            "engine_version": engine_version,
                            "source_revision": original_revision,
                            "display_revision": display_revision,
                        },
                    }
                )
                role_revision = (
                    _digest_revision(
                        "role",
                        {
                            "annotation_id": annotation_id,
                            "role": role,
                            "confidence": confidence,
                            "provider_type": provider_type,
                        },
                    )
                    if role is not None
                    else ""
                )
                revision_payload = {
                    "annotation_id": annotation_id,
                    "source": source.as_dict(),
                    "selector": selector.as_dict(),
                    "order": (
                        geometry_index * _MAX_CAPTURE_REGIONS_PER_GEOMETRY
                        + region_index
                    ),
                    "role": role,
                    "confidence": confidence,
                    "provenance": provenance.as_dict(),
                    "extensions": extensions,
                }
                values.append(
                    SpatialAnnotationView(
                        key=SpatialAnnotationKey(item_id, annotation_id),
                        revision=_digest_revision(
                            "annotation",
                            revision_payload,
                        ),
                        source=source,
                        selector=selector,
                        order=revision_payload["order"],
                        label=(text or provider_type or role or region_id)[:512],
                        freshness=ArtifactFreshness.CURRENT,
                        role_assignments=(
                            (
                                SpatialRoleAssignment(
                                    role,
                                    RoleAssignmentOrigin.MACHINE,
                                    role_revision,
                                    confidence=confidence,
                                    provenance=provenance,
                                ),
                            )
                            if role is not None
                            else ()
                        ),
                        linked_artifact_ids=(display_artifact_id,),
                        provenance=provenance,
                        extensions=extensions,
                    )
                )
        return tuple(values)

    def _capture_assignments(
        self,
        item_id: str,
        value: Any,
        *,
        asset_id: str,
    ) -> tuple[CategoryAssignment, ...]:
        if not isinstance(value, Mapping):
            return ()
        assignments: list[CategoryAssignment] = []
        suggested = value.get("suggested")
        confidence = _confidence(value.get("confidence"))
        if isinstance(suggested, str) and suggested in IMAGE_CATEGORIES:
            assignments.append(
                CategoryAssignment(
                    suggested,
                    AssignmentOrigin.SUGGESTED,
                    _digest_revision(
                        "category",
                        {
                            "asset_id": asset_id,
                            "origin": "suggested",
                            "category": suggested,
                            "confidence": confidence,
                            "algorithm": value.get("algorithm"),
                            "algorithm_version": value.get("algorithm_version"),
                        },
                    ),
                    confidence=confidence,
                    provenance=ArtifactProvenance(
                        origin="machine",
                        provider_id="android",
                        model=_public_text(
                            value.get("algorithm"),
                            maximum=256,
                        ),
                    ),
                )
            )
        manual = value.get("manual_override")
        manual_revision = _non_negative_integer(value.get("manual_revision"))
        if isinstance(manual, str) and manual in IMAGE_CATEGORIES:
            assignments.append(
                CategoryAssignment(
                    manual,
                    AssignmentOrigin.MANUAL,
                    _digest_revision(
                        "category",
                        {
                            "asset_id": asset_id,
                            "origin": "manual",
                            "category": manual,
                            "revision": manual_revision,
                            "updated_at": value.get("manual_updated_at"),
                        },
                    ),
                    provenance=ArtifactProvenance(
                        origin="manual",
                        provider_id="android",
                    ),
                )
            )
        return tuple(assignments)

    def _capture_freshness(
        self,
        raw: Mapping[str, Any],
        observation: _ResourceObservation,
    ) -> ArtifactFreshness:
        lifecycle = raw.get("lifecycle")
        state = (
            str(lifecycle.get("state") or "")
            if isinstance(lifecycle, Mapping)
            else ""
        )
        if observation.integrity_mismatch or state in {"failed", "cancelled"}:
            return ArtifactFreshness.STALE
        if observation.state is ResourceState.AVAILABLE:
            return ArtifactFreshness.CURRENT
        return ArtifactFreshness.UNTRACKED

    def _capture_view(
        self,
        item_id: str,
        *,
        artifact_id: str,
        kind: str,
        observation: _ResourceObservation,
        source: RasterSourceRef,
        label: str,
        freshness: ArtifactFreshness,
        assignments: tuple[CategoryAssignment, ...],
        provenance: ArtifactProvenance,
        lineage: tuple[RasterLineageRef, ...],
        extensions: Mapping[str, Any],
    ) -> RasterArtifactView:
        resource = self._resource_ref(
            item_id,
            artifact_id,
            observation,
        )
        public_extensions = _public_extensions(extensions)
        public_revision = _digest_revision(
            "artifact",
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "media_type": observation.media_type,
                "content_sha256": observation.content_sha256,
                "dimensions": observation.dimensions.as_dict(),
                "source": source.as_dict(),
                "resource_state": observation.state.value,
                "freshness": freshness.value,
                "lineage": [value.as_dict() for value in lineage],
                "assignments": [value.as_dict() for value in assignments],
                "provenance": provenance.as_dict(),
                "extensions": public_extensions,
            },
        )
        return RasterArtifactView(
            key=RasterArtifactKey(item_id, artifact_id),
            revision=public_revision,
            kind=kind,
            media_type=observation.media_type,
            content_sha256=observation.content_sha256,
            dimensions=observation.dimensions,
            source=source,
            resource_state=observation.state,
            resource=resource,
            label=label,
            freshness=freshness,
            lineage=lineage,
            category_assignments=assignments,
            provenance=provenance,
            extensions=public_extensions,
        )

    def _resource_ref(
        self,
        item_id: str,
        artifact_id: str,
        observation: _ResourceObservation,
    ) -> RasterResourceRef | None:
        resolved = observation.resolved
        if observation.state is not ResourceState.AVAILABLE or resolved is None:
            return None
        digest = hashlib.sha256(
            f"{item_id}\0{artifact_id}".encode("utf-8")
        ).hexdigest()
        # The public variant describes intent rather than a private file name.
        public_variant = (
            "original"
            if artifact_id.endswith(":original")
            else "display"
            if artifact_id.endswith(":display")
            else "full"
        )
        return RasterResourceRef(
            f"raster:{digest[:40]}",
            resolved.revision,
            public_variant,
        )

    def _observe_resource(
        self,
        item_id: str,
        directory: Path,
        reference: Any,
        *,
        artifact_id: str,
        variant: str,
        declared_sha256: str,
        declared_dimensions: tuple[int | None, int | None],
        orientation: int,
        section: str,
        fallback_dimensions: tuple[int, int] | None = None,
    ) -> _ResourceObservation | None:
        expected_media_type = _media_type(reference)
        width, height = declared_dimensions
        if (width is None or height is None) and fallback_dimensions is not None:
            fallback_width, fallback_height = fallback_dimensions
            width = width or _positive_integer(fallback_width)
            height = height or _positive_integer(fallback_height)
        safe_reference = (
            isinstance(reference, str)
            and _RESOURCE_LEAF_RE.fullmatch(reference) is not None
            and "/" not in reference
            and "\\" not in reference
            and reference not in {".", ".."}
        )
        if not safe_reference:
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                expected_media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
            )
        path = directory / reference
        try:
            authority = self._assert_safe_path(
                path,
                item_id=item_id,
                section=section,
            )
        except RepositoryError:
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                expected_media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
            )
        try:
            info = path.lstat()
        except FileNotFoundError:
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.MISSING,
                expected_media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
            )
        except OSError:
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                expected_media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
            )
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                expected_media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
            )
        descriptor = -1
        snapshot: BinaryIO | None = None
        try:
            snapshot = tempfile.TemporaryFile(mode="w+b")
            descriptor, opened = _open_verified_regular(
                path,
                info,
                authority=authority,
            )
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                snapshot.write(block)
            named_after = _finish_verified_regular(
                path,
                descriptor,
                named_before=info,
                opened_before=opened,
            )
            self._assert_safe_path(path, item_id=item_id, section=section)
            actual_sha256 = digest.hexdigest()
            verified = _verified_image_properties(snapshot)
        except (OSError, RepositoryError):
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                expected_media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if snapshot is not None:
                snapshot.close()
        if verified is None:
            if not declared_sha256 or width is None or height is None:
                return None
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                _UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256,
                RasterDimensions(width, height, orientation),
                None,
                integrity_mismatch=True,
            )
        # Persisted Android dimensions describe the sending device's
        # rendition. A desktop import may have normalized it again, so the
        # bytes actually being granted are authoritative here.
        width, height, actual_media_type = verified
        if width is None or height is None:
            return None
        content_sha256 = declared_sha256 or actual_sha256
        dimensions = RasterDimensions(width, height, orientation)
        if (
            not expected_media_type
            or actual_media_type != expected_media_type
        ):
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                actual_media_type,
                content_sha256,
                dimensions,
                None,
                integrity_mismatch=True,
            )
        if declared_sha256 and actual_sha256 != declared_sha256:
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                actual_media_type,
                declared_sha256,
                dimensions,
                None,
                integrity_mismatch=True,
            )
        revision = f"bytes:{content_sha256}"
        resolved = _ResolvedRasterCandidate(
            path=path,
            file_identity=_stable_stat_identity(named_after),
            section=section,
            media_type=actual_media_type,
            content_sha256=content_sha256,
            size=size,
            revision=revision,
        )
        return _ResourceObservation(
            ResourceState.AVAILABLE,
            actual_media_type,
            content_sha256,
            dimensions,
            resolved,
        )

    def _project_layout(
        self,
        item_id: str,
        entry_directory: Path,
        layout: Mapping[str, Any],
    ) -> tuple[
        tuple[RasterArtifactView, ...],
        tuple[SpatialAnnotationView, ...],
        Mapping[tuple[str, str, str], _ResolvedRasterCandidate],
    ]:
        regions = layout.get("regions", {})
        images = layout.get("images", {})
        if not isinstance(regions, Mapping) or not isinstance(images, Mapping):
            raise _repository_error(
                "the Mistral layout collections are invalid",
                code="invalid_mistral_layout",
                item_id=item_id,
                section="layout",
            )
        if len(regions) > _MAX_LAYOUT_PAGES or len(images) > _MAX_FIGURES:
            raise _repository_error(
                "the Mistral layout exceeds its collection limits",
                code="invalid_mistral_layout",
                item_id=item_id,
                section="layout",
            )

        representation_revisions: dict[str, str | None] = {}

        def source_revision(source: str) -> str | None:
            if source not in representation_revisions:
                representation_revisions[source] = (
                    self._live_representation_revision(item_id, source)
                )
            return representation_revisions[source]

        page_contexts: dict[
            tuple[str, int],
            tuple[SpatialSourceRef, tuple[int, int] | None],
        ] = {}
        for source_key, pages in regions.items():
            source = self._layout_source_id(item_id, source_key)
            if not isinstance(pages, Mapping):
                raise _repository_error(
                    "the Mistral region source is invalid",
                    code="invalid_mistral_layout",
                    item_id=item_id,
                    section="layout",
                )
            if len(pages) > _MAX_LAYOUT_PAGES:
                raise _repository_error(
                    "the Mistral region source has too many pages",
                    code="invalid_mistral_layout",
                    item_id=item_id,
                    section="layout",
                )
            rep_revision = source_revision(source)
            if rep_revision is None:
                continue
            for page_key, record in pages.items():
                page = self._page_number(page_key)
                if page is None or not isinstance(record, Mapping):
                    continue
                page_contexts[(source, page)] = self._page_context(
                    item_id,
                    source,
                    rep_revision,
                    page,
                    record.get("dims"),
                )

        for info in images.values():
            if not isinstance(info, Mapping):
                continue
            source = self._layout_source_id(
                item_id,
                info.get("src_key") or "primary",
            )
            page = self._page_number(info.get("page"))
            if page is None or (source, page) in page_contexts:
                continue
            rep_revision = source_revision(source)
            if rep_revision is None:
                continue
            page_contexts[(source, page)] = self._page_context(
                item_id,
                source,
                rep_revision,
                page,
                {},
            )

        figure_drafts = self._figure_drafts(
            item_id,
            entry_directory,
            images,
            page_contexts,
        )
        figure_ids = {
            draft.name: draft.artifact_id for draft in figure_drafts
        }
        figure_revisions = {
            draft.artifact_id: draft.revision for draft in figure_drafts
        }
        raster: list[RasterArtifactView] = []
        spatial: list[SpatialAnnotationView] = []
        resources: dict[tuple[str, str, str], _ResolvedRasterCandidate] = {}
        for draft in figure_drafts:
            lineage: tuple[RasterLineageRef, ...] = ()
            if draft.rework_of:
                parent_id = figure_ids.get(draft.rework_of)
                if parent_id and parent_id in figure_revisions:
                    lineage = (
                        RasterLineageRef(
                            parent_id,
                            figure_revisions[parent_id],
                            "rework_of",
                        ),
                    )
            resource = self._resource_ref(
                item_id,
                draft.artifact_id,
                draft.observation,
            )
            captions = (draft.caption,) if draft.caption is not None else ()
            figure_extensions = _public_extensions(
                {
                    "corrections_ui": {"annotation_frame": "crop"},
                    "extension_metadata": (
                        draft.info.get("ext")
                        if isinstance(draft.info.get("ext"), Mapping)
                        else {}
                    ),
                    "legacy": _unknown_fields(draft.info, _FIGURE_FIELDS),
                }
            )
            view = RasterArtifactView(
                key=RasterArtifactKey(item_id, draft.artifact_id),
                revision=draft.revision,
                kind="reworked-figure" if draft.rework_of else "extracted-figure",
                media_type=draft.observation.media_type,
                content_sha256=draft.observation.content_sha256,
                dimensions=draft.observation.dimensions,
                source=draft.source,
                resource_state=draft.observation.state,
                resource=resource,
                label=draft.name,
                freshness=(
                    ArtifactFreshness.STALE
                    if draft.observation.integrity_mismatch
                    else ArtifactFreshness.CURRENT
                    if draft.observation.state is ResourceState.AVAILABLE
                    else ArtifactFreshness.UNTRACKED
                ),
                lineage=lineage,
                caption_assertions=captions,
                provenance=ArtifactProvenance(
                    origin="ocr",
                    provider_id="mistral",
                ),
                extensions=figure_extensions,
            )
            raster.append(view)
            if view.resource is not None and draft.observation.resolved is not None:
                resources[
                    (
                        view.resource.resource_id,
                        view.resource.revision,
                        view.resource.variant,
                    )
                ] = draft.observation.resolved
            if draft.selector is not None:
                spatial.append(
                    SpatialAnnotationView(
                        key=SpatialAnnotationKey(
                            item_id,
                            draft.annotation_id,
                        ),
                        revision=draft.annotation_revision,
                        source=SpatialSourceRef(
                            draft.source.representation_id,
                            draft.source.representation_revision,
                            draft.source.canvas_id,
                            draft.source.canvas_revision,
                        ),
                        selector=draft.selector,
                        label=draft.name,
                        freshness=ArtifactFreshness.CURRENT,
                        role_assignments=(
                            SpatialRoleAssignment(
                                "figure",
                                RoleAssignmentOrigin.MACHINE,
                                _digest_revision(
                                    "role",
                                    {
                                        "annotation_id": draft.annotation_id,
                                        "role": "figure",
                                    },
                                ),
                                provenance=ArtifactProvenance(
                                    origin="ocr",
                                    provider_id="mistral",
                                ),
                            ),
                        ),
                        caption_assertions=captions,
                        linked_artifact_ids=(draft.artifact_id,),
                        provenance=ArtifactProvenance(
                            origin="ocr",
                            provider_id="mistral",
                        ),
                    )
                )

        spatial.extend(
            self._region_annotations(
                item_id,
                regions,
                page_contexts,
                figure_ids,
            )
        )
        return tuple(raster), tuple(spatial), resources

    def _layout_source_id(self, item_id: str, value: Any) -> str:
        return _persisted_token(
            value,
            item_id=item_id,
            field="source_representation_id",
            code="invalid_mistral_layout",
        )

    @staticmethod
    def _page_number(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.isdigit():
            result = int(value)
            return result if result > 0 else None
        return None

    def _page_context(
        self,
        item_id: str,
        source: str,
        representation_revision: str,
        page: int,
        dims_value: Any,
    ) -> tuple[SpatialSourceRef, tuple[int, int] | None]:
        dims = dims_value if isinstance(dims_value, Mapping) else {}
        # Replica's canonical sidecar uses w/h/dpi. width/height remain a
        # read-only compatibility fallback for pre-contract provider output.
        width = _positive_integer(dims.get("w")) or _positive_integer(
            dims.get("width")
        )
        height = _positive_integer(dims.get("h")) or _positive_integer(
            dims.get("height")
        )
        canvas_id = _composite_identity(
            "page",
            str(page),
            item_id=item_id,
            field="canvas_id",
            code="invalid_mistral_layout",
        )
        canvas_revision = _digest_revision(
            "canvas",
            {
                "representation_revision": representation_revision,
                "canvas_id": canvas_id,
                "width": width,
                "height": height,
            },
        )
        return (
            SpatialSourceRef(
                source,
                representation_revision,
                canvas_id,
                canvas_revision,
            ),
            (width, height) if width and height else None,
        )

    def _figure_drafts(
        self,
        item_id: str,
        entry_directory: Path,
        images: Mapping[Any, Any],
        page_contexts: Mapping[
            tuple[str, int],
            tuple[SpatialSourceRef, tuple[int, int] | None],
        ],
    ) -> tuple[_FigureDraft, ...]:
        directory = entry_directory / "ocr" / "images"
        drafts: list[_FigureDraft] = []
        for name_value, value in images.items():
            if not isinstance(value, Mapping):
                continue
            name = _figure_name(name_value, item_id=item_id)
            source_name = self._layout_source_id(
                item_id,
                value.get("src_key") or "primary",
            )
            page = self._page_number(value.get("page"))
            context = page_contexts.get((source_name, page or 0))
            if page is None or context is None:
                continue
            spatial_source, page_dimensions = context
            artifact_id = _opaque_identity("figure", name)
            annotation_id = _opaque_identity(
                "figure-box",
                source_name,
                page,
                name,
            )
            selector = self._selector(
                value,
                coordinate_space_revision=spatial_source.canvas_revision,
                canvas_dimensions=page_dimensions,
            )
            fallback_dimensions = None
            if selector is not None and page_dimensions is not None:
                xs = [float(point.x) for point in selector.points]
                ys = [float(point.y) for point in selector.points]
                fallback_dimensions = (
                    max(1, round((max(xs) - min(xs)) * page_dimensions[0])),
                    max(1, round((max(ys) - min(ys)) * page_dimensions[1])),
                )
            observation = self._observe_resource(
                item_id,
                directory,
                name,
                artifact_id=artifact_id,
                variant="full",
                declared_sha256=_sha256(value.get("sha256")),
                declared_dimensions=(
                    _positive_integer(value.get("width")),
                    _positive_integer(value.get("height")),
                ),
                orientation=1,
                section="layout",
                fallback_dimensions=fallback_dimensions,
            )
            if observation is None:
                continue
            caption = self._figure_caption(
                value,
                annotation_id=annotation_id,
            )
            public_extensions = _public_extensions(
                {
                    "corrections_ui": {"annotation_frame": "crop"},
                    "extension_metadata": (
                        value.get("ext")
                        if isinstance(value.get("ext"), Mapping)
                        else {}
                    ),
                    "legacy": _unknown_fields(value, _FIGURE_FIELDS),
                }
            )
            revision_payload = {
                "artifact_id": artifact_id,
                "source": spatial_source.as_dict(),
                "media_type": observation.media_type,
                "content_sha256": observation.content_sha256,
                "dimensions": observation.dimensions.as_dict(),
                "resource_state": observation.state.value,
                "selector": selector.as_dict() if selector else None,
                "caption": caption.as_dict() if caption else None,
                "rework_of": value.get("rework_of"),
                "extensions": public_extensions,
            }
            drafts.append(
                _FigureDraft(
                    name=name,
                    artifact_id=artifact_id,
                    revision=_digest_revision("artifact", revision_payload),
                    source=RasterSourceRef(
                        spatial_source.representation_id,
                        spatial_source.representation_revision,
                        spatial_source.canvas_id,
                        spatial_source.canvas_revision,
                    ),
                    observation=observation,
                    info=value,
                    selector=selector,
                    annotation_id=annotation_id,
                    annotation_revision=_digest_revision(
                        "annotation",
                        {
                            "annotation_id": annotation_id,
                            "selector": selector.as_dict() if selector else None,
                            "caption": caption.as_dict() if caption else None,
                        },
                    ),
                    caption=caption,
                    rework_of=(
                        str(value.get("rework_of"))
                        if isinstance(value.get("rework_of"), str)
                        else ""
                    ),
                )
            )
        return tuple(drafts)

    def _figure_caption(
        self,
        info: Mapping[str, Any],
        *,
        annotation_id: str,
    ) -> CaptionAssertion | None:
        text = info.get("caption")
        if not isinstance(text, str) and isinstance(info.get("ext"), Mapping):
            text = info["ext"].get("caption")
        if not isinstance(text, str) or not text.strip():
            return None
        bounded = _public_text(text, maximum=16_384).strip()
        if not bounded:
            return None
        return CaptionAssertion(
            bounded,
            CaptionOrigin.IMPORTED,
            _digest_revision(
                "caption",
                {"annotation_id": annotation_id, "text": bounded},
            ),
            source_annotation_id=annotation_id,
            provenance=ArtifactProvenance(
                origin="ocr",
                provider_id="mistral",
            ),
        )

    def _region_annotations(
        self,
        item_id: str,
        regions: Mapping[Any, Any],
        page_contexts: Mapping[
            tuple[str, int],
            tuple[SpatialSourceRef, tuple[int, int] | None],
        ],
        figure_ids: Mapping[str, str],
    ) -> tuple[SpatialAnnotationView, ...]:
        values: list[SpatialAnnotationView] = []
        for source_value, pages in regions.items():
            source = self._layout_source_id(item_id, source_value)
            if not isinstance(pages, Mapping):
                continue
            for page_value, record in pages.items():
                page = self._page_number(page_value)
                context = page_contexts.get((source, page or 0))
                if page is None or context is None or not isinstance(record, Mapping):
                    continue
                spatial_source, page_dimensions = context
                items = record.get("items")
                if (
                    isinstance(items, (str, bytes))
                    or not isinstance(items, Sequence)
                    or len(items) > _MAX_PAGE_REGIONS
                ):
                    raise _repository_error(
                        "a Mistral page has an invalid region list",
                        code="invalid_mistral_layout",
                        item_id=item_id,
                        section="layout",
                    )
                for index, raw in enumerate(items):
                    if not isinstance(raw, Mapping):
                        continue
                    # ``id`` is a regenerated display index (r0, r1, ...).
                    # Only ``rid`` survives reorder/save round trips. Never
                    # mint or derive one during a read: legacy anonymous rows
                    # remain unaddressable until a canonical writer migrates
                    # them.
                    persisted_id = raw.get("rid")
                    if not isinstance(persisted_id, str) or not persisted_id:
                        continue
                    region_id = _persisted_token(
                        persisted_id,
                        item_id=item_id,
                        field="region_rid",
                        code="invalid_mistral_layout",
                    )
                    annotation_id = _opaque_identity("region", region_id)
                    selector = self._selector(
                        raw.get("box"),
                        coordinate_space_revision=spatial_source.canvas_revision,
                        canvas_dimensions=page_dimensions,
                    )
                    role = raw.get("role")
                    if selector is None or not isinstance(role, str) or not role:
                        continue
                    public_role = _capture_region_role(role)
                    origin = (
                        RoleAssignmentOrigin.MACHINE
                        if record.get("origin") == "machine"
                        else RoleAssignmentOrigin.IMPORTED
                    )
                    role_revision = (
                        _digest_revision(
                            "role",
                            {
                                "annotation_id": annotation_id,
                                "role": public_role,
                                "confidence": raw.get("confidence"),
                                "origin": origin.value,
                            },
                        )
                        if public_role is not None
                        else ""
                    )
                    linked = self._linked_figures(raw.get("text"), figure_ids)
                    caption = None
                    caption_text = _public_text(
                        raw.get("caption"),
                        maximum=16_384,
                    ).strip()
                    if caption_text:
                        caption = CaptionAssertion(
                            caption_text,
                            CaptionOrigin.IMPORTED,
                            _digest_revision(
                                "caption",
                                {
                                    "annotation_id": annotation_id,
                                    "text": caption_text,
                                },
                            ),
                            source_annotation_id=annotation_id,
                            provenance=ArtifactProvenance(
                                origin="ocr",
                                provider_id="mistral",
                            ),
                        )
                    extensions = _public_extensions(
                        {
                            "document": _public_text(
                                record.get("doc"),
                                maximum=512,
                            ),
                            "text": _public_text(
                                raw.get("text"),
                                maximum=8192,
                            ),
                            "normalized_text": _public_text(
                                raw.get("norm"),
                                maximum=8192,
                            ),
                            "legacy": _unknown_fields(raw, _REGION_FIELDS),
                        }
                    )
                    revision_payload = {
                        "annotation_id": annotation_id,
                        "selector": selector.as_dict(),
                        "role": public_role,
                        "order": raw.get("order"),
                        "caption": caption.as_dict() if caption else None,
                        "linked": linked,
                        "extensions": extensions,
                    }
                    order = _non_negative_integer(raw.get("order"))
                    annotation = SpatialAnnotationView(
                        key=SpatialAnnotationKey(item_id, annotation_id),
                        revision=_digest_revision(
                            "annotation",
                            revision_payload,
                        ),
                        source=spatial_source,
                        selector=selector,
                        order=index if order is None else order,
                        label=(
                            _public_text(raw.get("text"), maximum=512)
                            or _public_text(role, maximum=512)
                        ),
                        freshness=(
                            ArtifactFreshness.STALE
                            if record.get("stale")
                            else ArtifactFreshness.CURRENT
                        ),
                        role_assignments=(
                            (
                                SpatialRoleAssignment(
                                    public_role,
                                    origin,
                                    role_revision,
                                    confidence=_confidence(
                                        raw.get("confidence")
                                    ),
                                    provenance=ArtifactProvenance(
                                        origin="ocr",
                                        provider_id="mistral",
                                    ),
                                ),
                            )
                            if public_role is not None
                            else ()
                        ),
                        caption_assertions=(caption,) if caption else (),
                        linked_artifact_ids=linked,
                        provenance=ArtifactProvenance(
                            origin="ocr",
                            provider_id="mistral",
                        ),
                        extensions=extensions,
                    )
                    values.append(annotation)
        return tuple(values)

    @staticmethod
    def _linked_figures(
        text: Any,
        figure_ids: Mapping[str, str],
    ) -> tuple[str, ...]:
        if not isinstance(text, str):
            return ()
        found: list[str] = []
        seen: set[str] = set()
        for reference in _FIGURE_REFERENCE_RE.findall(text):
            artifact_id = figure_ids.get(reference)
            if artifact_id and artifact_id not in seen:
                found.append(artifact_id)
                seen.add(artifact_id)
                # SpatialAnnotationView intentionally bounds its public graph.
                # Preserve the first references in source-text order so a
                # valid legacy region cannot invalidate the whole projection.
                if len(found) == 64:
                    break
        return tuple(found)

    def _selector(
        self,
        value: Any,
        *,
        coordinate_space_revision: str,
        canvas_dimensions: tuple[int, int] | None,
    ) -> NormalizedPolygonSelector | None:
        if not isinstance(value, Mapping):
            return None
        rectangle = {
            "x": value.get("x"),
            "y": value.get("y"),
            "w": value.get("w", value.get("width")),
            "h": value.get("h", value.get("height")),
        }
        numbers = tuple(rectangle.values())
        if any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(float(number))
            for number in numbers
        ):
            return None
        pixel_coordinates = any(float(number) > 1 for number in numbers)
        kwargs: dict[str, Any] = {}
        if pixel_coordinates:
            if canvas_dimensions is None:
                return None
            kwargs = {
                "canvas_width": canvas_dimensions[0],
                "canvas_height": canvas_dimensions[1],
            }
        try:
            projected = project_legacy_rectangle_annotation(
                item_id="projection",
                annotation_id="projection",
                annotation_revision="projection-r1",
                source=SpatialSourceRef(
                    "projection",
                    "projection-r1",
                    "projection",
                    coordinate_space_revision,
                ),
                rectangle=rectangle,
                **kwargs,
            )
        except ValidationError:
            return None
        return projected.selector


__all__ = [
    "FilesystemCorrectionsArtifactRepository",
    "FilesystemRasterResourceResolverPort",
    "MISTRAL_LAYOUT_RELATIVE",
    "PHOTO_ASSETS_NAME",
    "PHOTO_ASSETS_SCHEMA",
    "PHOTO_ASSETS_VERSION",
    "ResolvedRasterResource",
]
