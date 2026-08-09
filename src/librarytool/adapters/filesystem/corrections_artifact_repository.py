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

import functools
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import warnings
from collections import OrderedDict
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
from ...engine.corrections import CORRECTION_TARGET_AUTHORITY_EXTENSION
from ...engine.raster_artifacts import (
    IMAGE_CATEGORIES,
    ArtifactMetadataAssertion,
    ArtifactFreshness,
    ArtifactProvenance,
    AssignmentOrigin,
    CaptionAssertion,
    CaptionOrigin,
    CategoryAssignment,
    MAX_METADATA_ASSERTIONS,
    MetadataAssertionOrigin,
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
_MAX_RASTER_RESOURCE_BYTES = 100 * 1024 * 1024
_MAX_CAPTURE_ASSETS = 4096
_MAX_PROJECTION_CACHE_ENTRIES = 1024
_MAX_CACHED_PROJECTION_TARGETS = 512
_MAX_RESOURCE_CANDIDATE_CACHE_ENTRIES = 4096
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
_LEGACY_CAPTURE_IMAGE_RE = re.compile(
    r"^(orig|photo)_([1-9][0-9]{0,5})\.(?:bmp|gif|jpe?g|png|tiff?|webp)$",
    re.IGNORECASE,
)
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


@functools.lru_cache(maxsize=1)
def _kernel32() -> Any:
    """Bind kernel32 once.

    Every authority proof calls into kernel32 a dozen times or more, and
    rebuilding the ``WinDLL`` wrapper per call showed up as measurable load
    while building a Corrections index over a thousand captures.
    """

    import ctypes
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
    return kernel32


def _windows_descriptor_path(descriptor: int) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    get_final_path = _kernel32().GetFinalPathNameByHandleW
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
    open_existing = 3
    flag_backup_semantics = 0x02000000
    flag_open_reparse_point = 0x00200000
    kernel32 = _kernel32()
    create_file = kernel32.CreateFileW
    close_handle = kernel32.CloseHandle
    handle = create_file(
        str(path),
        file_read_attributes,
        # Keep guarded components read-only while the authority proof
        # revalidates the canonical root-to-parent handle chain. Denying
        # write/delete sharing also prevents concurrent junction retargeting.
        share_read,
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


def _public_metadata_assertions(
    value: Mapping[str, Any],
    *,
    artifact_id: str,
) -> tuple[ArtifactMetadataAssertion, ...]:
    public = _public_extensions(value)
    if set(public) == {"quarantine"} and "quarantine" not in value:
        return ()
    assertions: list[ArtifactMetadataAssertion] = []
    for name in sorted(public):
        if len(assertions) >= MAX_METADATA_ASSERTIONS // 2:
            break
        if name == "caption" or not _IDENTIFIER_RE.fullmatch(name):
            continue
        try:
            assertions.append(
                ArtifactMetadataAssertion(
                    name,
                    public[name],
                    MetadataAssertionOrigin.IMPORTED,
                    _digest_revision(
                        "metadata",
                        {
                            "artifact_id": artifact_id,
                            "name": name,
                            "value": public[name],
                            "origin": "imported",
                        },
                    ),
                    provenance=ArtifactProvenance(
                        origin="ocr",
                        provider_id="mistral",
                    ),
                )
            )
        except ValidationError:
            continue
    return tuple(assertions)


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

    def resolve_capture_preview(
        self,
        item_id: str,
        artifact_id: str,
    ) -> ResolvedRasterResource | None: ...


@dataclass(frozen=True, slots=True)
class _ResourceObservation:
    state: ResourceState
    media_type: str
    content_sha256: str
    dimensions: RasterDimensions
    resolved: _ResolvedRasterCandidate | None
    integrity_mismatch: bool = False
    diagnostic_code: str = ""


@dataclass(frozen=True, slots=True)
class _Projection:
    raster_artifacts: tuple[RasterArtifactView, ...]
    spatial_annotations: tuple[SpatialAnnotationView, ...]
    resources: Mapping[tuple[str, str, str], _ResolvedRasterCandidate]


@dataclass(frozen=True, slots=True)
class _CaptureAssetRecord:
    raw: Mapping[str, Any]
    asset_id: str
    order: int
    original: Mapping[str, Any]
    display: Mapping[str, Any]
    imported: Mapping[str, Any]
    namespace: str
    original_id: str
    display_id: str
    original_valid: bool
    display_valid: bool
    imported_at: str


@dataclass(frozen=True, slots=True)
class _CachedResourceCandidate:
    capture_id: str
    entry_directory: Path
    candidate: _ResolvedRasterCandidate
    watched_paths: tuple[tuple[Path, tuple[int, ...] | None], ...]
    representation_revisions: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class _CachedProjection:
    projection: _Projection
    capture_id: str
    entry_directory: Path
    watched_paths: tuple[tuple[Path, tuple[int, ...] | None], ...]
    representation_revisions: tuple[tuple[str, str | None], ...]


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
    metadata_assertions: tuple[ArtifactMetadataAssertion, ...]
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
        # Projection verifies and hashes image bytes. Corrections may ask for
        # the same immutable projection through the index, several artifact
        # groups, detail, annotations, and finally resource resolution. Keep a
        # bounded process-local result keyed by cheap authoritative stat
        # identities so one interaction does not reread gigabytes of captures.
        # Resource delivery still opens, hashes, and compares the live bytes in
        # ``resolve_raster_resource`` before granting a stream.
        self._projection_cache: OrderedDict[str, _CachedProjection] = (
            OrderedDict()
        )
        self._projection_cache_lock = threading.RLock()
        self._projection_locks = tuple(threading.RLock() for _ in range(64))
        self._resource_candidate_cache: OrderedDict[
            tuple[str, str, str, str], _CachedResourceCandidate
        ] = OrderedDict()

    def list_raster_artifacts(
        self,
        item_id: str,
    ) -> tuple[RasterArtifactView, ...]:
        return self._project(item_id).raster_artifacts

    def assert_item_exists(self, item_id: str) -> None:
        """Check catalogue membership without projecting any artifacts."""

        item = _identifier(
            item_id,
            item_id=str(item_id or ""),
            field="item_id",
        )
        with self._write_set.workspace_lease():
            with self._lock_context_for():
                if not self._live_item_exists(item):
                    raise NotFoundError(
                        "the item does not exist",
                        code="item_not_found",
                        details={"item_id": item},
                    )

    def get_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be RasterArtifactKey")
        return self._project_raster_key(key)

    def get_capture_raster_artifact(
        self,
        key: RasterArtifactKey,
    ) -> RasterArtifactView | None:
        """Project a key only when it belongs to current capture authority."""

        if not isinstance(key, RasterArtifactKey):
            raise TypeError("key must be RasterArtifactKey")
        return self._project_raster_key(key, capture_only=True)

    def list_capture_import_marks(
        self,
        item_ids: Sequence[str],
    ) -> tuple[Mapping[str, Any], ...]:
        """Report how many captures each item has and when they were imported.

        The Corrections list needs those two facts for every book — one to
        decide membership of the Captures view, one to order it — but needs the
        capture rows themselves only for the handful of rows it draws. This
        stops right after the manifest is parsed, so it does none of the
        per-asset filesystem work that makes the full hint read expensive.

        One workspace lease and one lock for the whole walk: taking them per
        item would cost more than the reads. Deliberately uncached — the
        identity a cache would key on is not a content identity, and the two
        staleness regressions in the test suite exist because that was tried.
        """

        marks: list[Mapping[str, Any]] = []
        with self._write_set.workspace_lease():
            with self._lock_context_for():
                for raw_item_id in item_ids:
                    item = _identifier(
                        raw_item_id,
                        item_id=str(raw_item_id or ""),
                        field="item_id",
                    )
                    if not self._live_item_exists(item):
                        continue
                    capture_id = self._live_capture_id(item)
                    if not capture_id:
                        continue
                    marks.append(
                        self._capture_import_mark(item, capture_id)
                    )
        return tuple(marks)

    def _capture_import_mark(
        self,
        item_id: str,
        capture_id: str,
    ) -> Mapping[str, Any]:
        """Summarise one capture directory without touching its assets."""

        # A manifest that cannot be read still means one capture the list must
        # show, matching the single synthetic row the index projects for it.
        unavailable = {
            "item_id": item_id,
            "capture_count": 1,
            "imported_at": "",
            "legacy": False,
        }
        try:
            directory = self._managed_directory(
                self._capture_directory_for,
                capture_id,
                item_id=item_id,
                section="capture",
                authority_root=self._capture_authority_root,
            )
            manifest = self._read_json(
                directory / PHOTO_ASSETS_NAME,
                item_id=item_id,
                section="capture",
                maximum_bytes=_MAX_PHOTO_MANIFEST_BYTES,
            )
            if manifest is None:
                manifest = self._legacy_capture_manifest(
                    item_id,
                    capture_id=capture_id,
                    directory=directory,
                )
            if manifest is None:
                return unavailable
            records, _representation_revision, legacy = (
                self._capture_manifest_records(
                    item_id,
                    capture_id,
                    manifest,
                )
            )
        except RepositoryError as error:
            if not self._recoverable_capture_manifest_error(error):
                raise
            return unavailable
        imported_at = ""
        desktop_import = manifest.get("desktop_import")
        if isinstance(desktop_import, Mapping):
            imported_at = _public_text(
                desktop_import.get("imported_at"),
                maximum=64,
            ).strip()
        return {
            "item_id": item_id,
            "capture_count": len(records),
            "imported_at": imported_at,
            # Reported, not applied. Whether a legacy manifest's timestamp is
            # suppressed depends on which path the index would have taken for
            # this item, and only the caller knows that.
            "legacy": legacy,
        }

    def list_capture_index_hints(
        self,
        item_id: str,
    ) -> tuple[Mapping[str, Any], ...]:
        """Return bounded capture navigation hints without reading image bytes."""

        snapshot = self.capture_index_hint_snapshot(item_id)
        return tuple(snapshot["hints"])

    def capture_index_hint_snapshot(
        self,
        item_id: str,
    ) -> Mapping[str, Any]:
        """Return navigation hints plus private manifest-only display pins.

        These hints are deliberately not raster artifact views and their
        ``index:`` revisions must never be used as mutation preconditions.
        Selecting a hint loads the authoritative artifact detail, which hashes
        and verifies that one capture before any edit or resource grant.
        """

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
                    capture_id = self._live_capture_id(item)
                    if not capture_id:
                        return {"hints": (), "authorities": {}}
                    directory = self._managed_directory(
                        self._capture_directory_for,
                        capture_id,
                        item_id=item,
                        section="capture",
                        authority_root=self._capture_authority_root,
                    )
                    try:
                        manifest = self._read_json(
                            directory / PHOTO_ASSETS_NAME,
                            item_id=item,
                            section="capture",
                            maximum_bytes=_MAX_PHOTO_MANIFEST_BYTES,
                        )
                        if manifest is None:
                            manifest = self._legacy_capture_manifest(
                                item,
                                capture_id=capture_id,
                                directory=directory,
                            )
                        if manifest is None:
                            return {
                                "hints": (
                                    self._capture_inventory_index_hint(
                                        capture_id,
                                        state=ResourceState.MISSING,
                                        diagnostic_code="capture_manifest_missing",
                                    ),
                                ),
                                "authorities": {},
                            }
                        authorities: dict[str, Mapping[str, Any]] = {}
                        hints = self._capture_index_hints(
                            item,
                            capture_id=capture_id,
                            directory=directory,
                            manifest=manifest,
                            authority_hints=authorities,
                        )
                        return {
                            "hints": hints,
                            "authorities": authorities,
                        }
                    except RepositoryError as error:
                        if not self._recoverable_capture_manifest_error(error):
                            raise
                        return {
                            "hints": (
                                self._capture_inventory_index_hint(
                                    capture_id,
                                    state=ResourceState.UNAVAILABLE,
                                    diagnostic_code=error.code,
                                ),
                            ),
                            "authorities": {},
                        }
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the Corrections capture index is unavailable",
                code="corrections_artifact_repository_unavailable",
                item_id=item,
                cause_type=type(exc).__name__,
            ) from exc

    def resolve_capture_preview(
        self,
        item_id: str,
        artifact_id: str,
    ) -> ResolvedRasterResource | None:
        """Snapshot one navigation-hint display without projecting siblings.

        This read intentionally returns bytes only. It does not manufacture a
        public artifact revision and therefore cannot authorize a mutation.
        """

        key = RasterArtifactKey(item_id, artifact_id)
        item = key.item_id
        try:
            with self._write_set.workspace_lease():
                with self._lock_context_for():
                    if not self._live_item_exists(item):
                        raise NotFoundError(
                            "the item does not exist",
                            code="item_not_found",
                            details={"item_id": item},
                        )
                    capture_id = self._live_capture_id(item)
                    if not capture_id:
                        return None
                    directory = self._managed_directory(
                        self._capture_directory_for,
                        capture_id,
                        item_id=item,
                        section="capture",
                        authority_root=self._capture_authority_root,
                    )
                    manifest = self._read_json(
                        directory / PHOTO_ASSETS_NAME,
                        item_id=item,
                        section="capture",
                        maximum_bytes=_MAX_PHOTO_MANIFEST_BYTES,
                    )
                    if manifest is None:
                        manifest = self._legacy_capture_manifest(
                            item,
                            capture_id=capture_id,
                            directory=directory,
                        )
                    if manifest is None:
                        return None
                    records, _revision_value, _legacy = (
                        self._capture_manifest_records(
                            item,
                            capture_id,
                            manifest,
                        )
                    )
                    selected = next(
                        (
                            record
                            for record in records
                            if record.display_id == key.artifact_id
                        ),
                        None,
                    )
                    if selected is None or not selected.display_valid:
                        return None
                    reference = (
                        selected.imported.get("display_ref")
                        or selected.display.get("reference")
                    )
                    declared_sha256 = _sha256(
                        selected.imported.get("derivative_checksum")
                        or selected.display.get("sha256")
                    )
                    return self._snapshot_capture_preview(
                        item,
                        directory,
                        reference,
                        declared_sha256=declared_sha256,
                    )
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the Corrections capture preview is unavailable",
                code="corrections_artifact_repository_unavailable",
                item_id=item,
                cause_type=type(exc).__name__,
            ) from exc

    def _snapshot_capture_preview(
        self,
        item_id: str,
        directory: Path,
        reference: Any,
        *,
        declared_sha256: str,
    ) -> ResolvedRasterResource | None:
        expected_media_type = _media_type(reference)
        if (
            not isinstance(reference, str)
            or _RESOURCE_LEAF_RE.fullmatch(reference) is None
            or "/" in reference
            or "\\" in reference
            or reference in {".", ".."}
            or not expected_media_type
        ):
            return None
        path = directory / reference
        descriptor = -1
        snapshot: BinaryIO | None = None
        granted = False
        try:
            authority = self._assert_safe_path(
                path,
                item_id=item_id,
                section="capture",
            )
            named = path.lstat()
            if (
                _is_redirecting_path(path)
                or not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or named.st_size < 1
                or named.st_size > _MAX_RASTER_RESOURCE_BYTES
            ):
                return None
            snapshot = tempfile.TemporaryFile(mode="w+b")
            descriptor, opened = _open_verified_regular(
                path,
                named,
                authority=authority,
            )
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                size += len(block)
                if size > _MAX_RASTER_RESOURCE_BYTES:
                    return None
                digest.update(block)
                snapshot.write(block)
            _finish_verified_regular(
                path,
                descriptor,
                named_before=named,
                opened_before=opened,
            )
            self._assert_safe_path(
                path,
                item_id=item_id,
                section="capture",
            )
            # Recheck once more after the named-path validation. A hostile
            # ancestor replacement performed as that validation returns must
            # not turn the verified snapshot into an authority grant.
            self._assert_safe_path(
                path,
                item_id=item_id,
                section="capture",
            )
            actual_sha256 = digest.hexdigest()
            verified = _verified_image_properties(snapshot)
            if verified is None or verified[2] != expected_media_type:
                return None
            if declared_sha256 and actual_sha256 != declared_sha256:
                return None
            snapshot.seek(0)
            result = ResolvedRasterResource(
                stream=snapshot,
                media_type=verified[2],
                content_sha256=actual_sha256,
                size=size,
                revision=f"bytes:{actual_sha256}",
            )
            granted = True
            return result
        except (OSError, RepositoryError):
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if snapshot is not None and not granted:
                snapshot.close()

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
        candidate = self._resource_candidate(item_id, resource)
        if candidate is None:
            return None
        # A cached projection deliberately avoids reopening every unchanged
        # image. Before granting one selected resource, pin its named identity
        # once independently of the streaming descriptor. This preserves the
        # original rename/ancestor race checks while keeping repeated catalogue
        # and detail projections cheap.
        probe = -1
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
            probe, opened = _open_verified_regular(
                candidate.path,
                named,
                authority=authority,
            )
            _finish_verified_regular(
                candidate.path,
                probe,
                named_before=named,
                opened_before=opened,
            )
        except (OSError, RepositoryError):
            return None
        finally:
            if probe >= 0:
                os.close(probe)
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
            # Recheck once more after the named-path validation. A hostile
            # ancestor replacement performed as that validation returns must
            # not turn the verified descriptor into an authority grant.
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

    def _resource_candidate(
        self,
        item_id: str,
        resource: RasterResourceRef,
    ) -> _ResolvedRasterCandidate | None:
        item = _identifier(
            item_id,
            item_id=str(item_id or ""),
            field="item_id",
        )
        cache_key = (
            item,
            resource.resource_id,
            resource.revision,
            resource.variant,
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
                    capture_id = self._live_capture_id(item)
                    entry_directory = self._managed_directory(
                        self._entry_directory_for,
                        item,
                        item_id=item,
                        section="entry",
                        authority_root=self._write_set.root,
                    )
                    with self._projection_cache_lock:
                        cached = self._resource_candidate_cache.get(cache_key)
                        if cached is not None:
                            if (
                                cached.capture_id == capture_id
                                and cached.entry_directory == entry_directory
                                and all(
                                    self._path_stamp(path) == expected
                                    for path, expected in cached.watched_paths
                                )
                                and all(
                                    self._live_representation_revision(
                                        item,
                                        representation_id,
                                    )
                                    == expected
                                    for representation_id, expected
                                    in cached.representation_revisions
                                )
                            ):
                                self._resource_candidate_cache.move_to_end(
                                    cache_key
                                )
                                return cached.candidate
                            self._resource_candidate_cache.pop(cache_key, None)
                            # This reference was issued by a keyed projection,
                            # so an authority change is a conflict rather than
                            # a cache miss. Falling through to a fresh full
                            # projection could re-authorize the stale reference
                            # when the underlying bytes happen to be identical.
                            return None
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the Corrections resource authority is unavailable",
                code="corrections_artifact_repository_unavailable",
                item_id=item,
                cause_type=type(exc).__name__,
            ) from exc
        projection = self._project(item)
        return projection.resources.get(
            (resource.resource_id, resource.revision, resource.variant)
        )

    def _remember_resource_candidates(
        self,
        item_id: str,
        capture_id: str,
        entry_directory: Path,
        resources: Mapping[
            tuple[str, str, str], _ResolvedRasterCandidate
        ],
        *,
        watched_paths: tuple[
            tuple[Path, tuple[int, ...] | None], ...
        ],
        representation_revisions: tuple[
            tuple[str, str | None], ...
        ] = (),
    ) -> None:
        if not resources:
            return
        with self._projection_cache_lock:
            if any(
                self._path_stamp(path) != expected
                for path, expected in watched_paths
            ):
                return
            if any(
                self._live_representation_revision(
                    item_id,
                    representation_id,
                )
                != expected
                for representation_id, expected
                in representation_revisions
            ):
                return
            for resource_key, candidate in resources.items():
                cache_key = (item_id, *resource_key)
                self._resource_candidate_cache[cache_key] = (
                    _CachedResourceCandidate(
                        capture_id,
                        entry_directory,
                        candidate,
                        watched_paths,
                        representation_revisions,
                    )
                )
                self._resource_candidate_cache.move_to_end(cache_key)
            while (
                len(self._resource_candidate_cache)
                > _MAX_RESOURCE_CANDIDATE_CACHE_ENTRIES
            ):
                self._resource_candidate_cache.popitem(last=False)

    def _project_raster_key(
        self,
        key: RasterArtifactKey,
        *,
        capture_only: bool = False,
    ) -> RasterArtifactView | None:
        item = _identifier(
            key.item_id,
            item_id=str(key.item_id or ""),
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
                    capture_id = self._live_capture_id(item)
                    entry_directory = self._managed_directory(
                        self._entry_directory_for,
                        item,
                        item_id=item,
                        section="entry",
                        authority_root=self._write_set.root,
                    )
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
                    lock_index = hashlib.sha256(
                        item.encode("utf-8")
                    ).digest()[0] % len(self._projection_locks)
                    with self._projection_locks[lock_index]:
                        cached = self._cached_projection(
                            item,
                            capture_id=capture_id,
                            entry_directory=entry_directory,
                        )
                        if cached is not None:
                            selected = next(
                                (
                                    value
                                    for value in cached.raster_artifacts
                                    if value.key == key
                                ),
                                None,
                            )
                            if (
                                capture_only
                                and selected is not None
                                and selected.source.representation_id
                                != "capture"
                            ):
                                return None
                            return selected
                        if capture_directory is not None:
                            capture_watches = tuple(
                                (path, self._path_stamp(path))
                                for path in (
                                    capture_directory,
                                    capture_directory / PHOTO_ASSETS_NAME,
                                )
                            )
                            manifest = self._read_json(
                                capture_directory / PHOTO_ASSETS_NAME,
                                item_id=item,
                                section="capture",
                                maximum_bytes=_MAX_PHOTO_MANIFEST_BYTES,
                            )
                            if manifest is None:
                                manifest = self._legacy_capture_manifest(
                                    item,
                                    capture_id=capture_id,
                                    directory=capture_directory,
                                )
                            if manifest is not None:
                                raster, _spatial, resources = (
                                    self._project_capture(
                                        item,
                                        capture_id,
                                        capture_directory,
                                        manifest,
                                        artifact_id=key.artifact_id,
                                    )
                                )
                                selected = next(
                                    (
                                        value
                                        for value in raster
                                        if value.key == key
                                    ),
                                    None,
                                )
                                if selected is not None:
                                    self._remember_resource_candidates(
                                        item,
                                        capture_id,
                                        entry_directory,
                                        resources,
                                        watched_paths=capture_watches,
                                    )
                                    return selected
                        if capture_only:
                            return None
                        # Non-capture artifacts preserve the existing layout
                        # projection semantics without paying to re-observe
                        # every capture rendition first.
                        layout_watches = tuple(
                            (path, self._path_stamp(path))
                            for path in (
                                entry_directory,
                                entry_directory / "ocr",
                                entry_directory.joinpath(
                                    *MISTRAL_LAYOUT_RELATIVE
                                ),
                                entry_directory / "ocr" / "images",
                            )
                        )
                        layout = self._read_json(
                            entry_directory.joinpath(*MISTRAL_LAYOUT_RELATIVE),
                            item_id=item,
                            section="layout",
                            maximum_bytes=_MAX_LAYOUT_BYTES,
                        )
                        if layout is None:
                            return None
                        raster, _spatial, resources = self._project_layout(
                            item,
                            entry_directory,
                            layout,
                        )
                        selected = next(
                            (
                                value
                                for value in raster
                                if value.key == key
                            ),
                            None,
                        )
                        if selected is not None:
                            selected_resources: Mapping[
                                tuple[str, str, str],
                                _ResolvedRasterCandidate,
                            ] = {}
                            selected_revisions: tuple[
                                tuple[str, str | None], ...
                            ] = ()
                            if selected.resource is not None:
                                selected_key = (
                                    selected.resource.resource_id,
                                    selected.resource.revision,
                                    selected.resource.variant,
                                )
                                selected_resources = {
                                    selected_key: resources[selected_key]
                                }
                            if (
                                selected.source.representation_id
                                and selected.source.representation_id
                                != "capture"
                            ):
                                selected_revisions = ((
                                    selected.source.representation_id,
                                    selected.source.representation_revision,
                                ),)
                            self._remember_resource_candidates(
                                item,
                                capture_id,
                                entry_directory,
                                selected_resources,
                                watched_paths=layout_watches,
                                representation_revisions=selected_revisions,
                            )
                        return selected
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the Corrections raster artifact is unavailable",
                code="corrections_artifact_repository_unavailable",
                item_id=item,
                cause_type=type(exc).__name__,
            ) from exc

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
                    lock_index = hashlib.sha256(
                        item.encode("utf-8")
                    ).digest()[0] % len(self._projection_locks)
                    with self._projection_locks[lock_index]:
                        # A second request can arrive after the first cache
                        # check but before projection completes. Rechecking
                        # inside the item stripe ensures the expensive byte
                        # verification happens once per unchanged item.
                        cached = self._cached_projection(
                            item,
                            capture_id=capture_id,
                            entry_directory=entry_directory,
                        )
                        if cached is not None:
                            return cached
                        authority_paths = self._projection_authority_paths(
                            entry_directory,
                            capture_directory,
                        )
                        authority_before = tuple(
                            (path, self._path_stamp(path))
                            for path in authority_paths
                        )
                        projection = self._project_locked(
                            item,
                            entry_directory=entry_directory,
                            capture_id=capture_id,
                            capture_directory=capture_directory,
                        )
                        self._remember_projection(
                            item,
                            projection,
                            capture_id=capture_id,
                            entry_directory=entry_directory,
                            authority_before=authority_before,
                        )
                        return projection
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the Corrections artifact repository is unavailable",
                code="corrections_artifact_repository_unavailable",
                item_id=item,
                cause_type=type(exc).__name__,
            ) from exc

    @staticmethod
    def _path_stamp(path: Path) -> tuple[int, ...] | None:
        try:
            return _stable_stat_identity(path.lstat())
        except FileNotFoundError:
            return None
        except OSError:
            # An unreadable path must invalidate the cache and fall through to
            # the repository's ordinary diagnosable error path.
            return ()

    @staticmethod
    def _projection_authority_paths(
        entry_directory: Path,
        capture_directory: Path | None,
    ) -> tuple[Path, ...]:
        paths = {
            entry_directory,
            entry_directory / "ocr",
            entry_directory.joinpath(*MISTRAL_LAYOUT_RELATIVE),
            entry_directory / "ocr" / "images",
        }
        if capture_directory is not None:
            paths.update(
                {
                    capture_directory,
                    capture_directory / PHOTO_ASSETS_NAME,
                }
            )
        return tuple(sorted(paths, key=lambda value: str(value)))

    def _cached_projection(
        self,
        item_id: str,
        *,
        capture_id: str,
        entry_directory: Path,
    ) -> _Projection | None:
        with self._projection_cache_lock:
            cached = self._projection_cache.get(item_id)
            if cached is None:
                return None
            if (
                cached.capture_id != capture_id
                or cached.entry_directory != entry_directory
            ):
                self._projection_cache.pop(item_id, None)
                return None
            if any(
                self._path_stamp(path) != expected
                for path, expected in cached.watched_paths
            ):
                self._projection_cache.pop(item_id, None)
                return None
            for representation_id, expected in cached.representation_revisions:
                if (
                    self._live_representation_revision(
                        item_id,
                        representation_id,
                    )
                    != expected
                ):
                    self._projection_cache.pop(item_id, None)
                    return None
            self._projection_cache.move_to_end(item_id)
            return cached.projection

    def _remember_projection(
        self,
        item_id: str,
        projection: _Projection,
        *,
        capture_id: str,
        entry_directory: Path,
        authority_before: tuple[tuple[Path, tuple[int, ...] | None], ...],
    ) -> None:
        target_count = (
            len(projection.raster_artifacts)
            + len(projection.spatial_annotations)
        )
        if target_count > _MAX_CACHED_PROJECTION_TARGETS:
            return
        # Missing/unavailable files have no read-time candidate identity. Do
        # not cache those projections: an in-place repair could otherwise be
        # invisible to directory stamps on some filesystems.
        if any(
            value.resource_state is not ResourceState.AVAILABLE
            for value in projection.raster_artifacts
        ):
            return
        # Preserve the exact identities that surrounded projection. Never
        # restamp here: doing so could pair old projected content with a file
        # replaced between the validation stamp and cache publication.
        expected_stamps: dict[Path, tuple[int, ...] | None] = {}
        for path, expected in authority_before:
            prior = expected_stamps.setdefault(path, expected)
            if prior != expected:
                return
        for candidate in projection.resources.values():
            prior = expected_stamps.setdefault(
                candidate.path,
                candidate.file_identity,
            )
            if prior != candidate.file_identity:
                return
        watched = tuple(
            sorted(expected_stamps.items(), key=lambda value: str(value[0]))
        )
        source_revisions: dict[str, str] = {}
        for value in (
            *projection.raster_artifacts,
            *projection.spatial_annotations,
        ):
            representation_id = value.source.representation_id
            if not representation_id or representation_id == "capture":
                continue
            expected = value.source.representation_revision
            prior = source_revisions.setdefault(representation_id, expected)
            if prior != expected:
                return
        revisions = tuple(sorted(source_revisions.items()))
        if any(
            self._live_representation_revision(item_id, representation_id)
            != expected
            for representation_id, expected in revisions
        ):
            return
        if self._live_capture_id(item_id) != capture_id:
            return
        cached = _CachedProjection(
            projection,
            capture_id,
            entry_directory,
            watched,
            revisions,
        )
        with self._projection_cache_lock:
            # Close the publication window after all potentially slow live
            # callbacks. A subsequent filesystem change is harmless because
            # every cache read revalidates these same stamps.
            if any(
                self._path_stamp(path) != expected
                for path, expected in watched
            ):
                return
            self._projection_cache[item_id] = cached
            self._projection_cache.move_to_end(item_id)
            while len(self._projection_cache) > _MAX_PROJECTION_CACHE_ENTRIES:
                self._projection_cache.popitem(last=False)

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
            if part in {"", ".", ".."}:
                # Traversal is refused by name, as the sibling stores do in
                # their own _safe_target. Do NOT read this walk as a complete
                # containment proof: `relative_to` above compares
                # case-insensitively on Windows, so a sibling directory that
                # differs only by case — or by a character that lowercases onto
                # one, such as U+212A KELVIN SIGN against "k" — satisfies it
                # while being a different directory on disk. Resolving the path
                # here did not close that either, because it canonicalises and
                # then compares the same way.
                #
                # What actually proves containment is the guarded descriptor
                # chain in _open_verified_regular: it compares
                # GetFinalPathNameByHandle output case-exactly, per read. Every
                # caller that opens a file goes through it. The two that do not
                # — _managed_directory below, and _capture_index_resource_state
                # — are safe only because the identity resolvers upstream feed
                # a single regex-validated ASCII component. Loosen one of those
                # and this walk will not save you.
                raise _repository_error(
                    "a Corrections store path escapes the workspace",
                    code="unsafe_corrections_store_path",
                    item_id=item_id,
                    section=section,
                )
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
                failure_kind=(
                    "read" if isinstance(exc, OSError) else "decode"
                ),
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
            try:
                photo_assets = self._read_json(
                    capture_directory / PHOTO_ASSETS_NAME,
                    item_id=item_id,
                    section="capture",
                    maximum_bytes=_MAX_PHOTO_MANIFEST_BYTES,
                )
                if photo_assets is None:
                    photo_assets = self._legacy_capture_manifest(
                        item_id,
                        capture_id=capture_id,
                        directory=capture_directory,
                    )
                if photo_assets is None:
                    (
                        capture_views,
                        capture_spatial,
                        capture_resources,
                    ) = (
                        (
                            self._capture_inventory_placeholder(
                                item_id,
                                capture_id=capture_id,
                                state=ResourceState.MISSING,
                                diagnostic_code="capture_manifest_missing",
                            ),
                        ),
                        (),
                        {},
                    )
                else:
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
            except RepositoryError as error:
                if not self._recoverable_capture_manifest_error(error):
                    raise
                capture_views, capture_spatial, capture_resources = (
                    (
                        self._capture_inventory_placeholder(
                            item_id,
                            capture_id=capture_id,
                            state=ResourceState.UNAVAILABLE,
                            diagnostic_code=error.code,
                        ),
                    ),
                    (),
                    {},
                )
            raster.extend(capture_views)
            spatial.extend(capture_spatial)
            resources.update(capture_resources)

        layout_path = entry_directory.joinpath(*MISTRAL_LAYOUT_RELATIVE)
        try:
            layout = self._read_json(
                layout_path,
                item_id=item_id,
                section="layout",
                maximum_bytes=_MAX_LAYOUT_BYTES,
            )
            if layout is not None:
                (
                    layout_raster,
                    layout_spatial,
                    layout_resources,
                ) = self._project_layout(
                    item_id,
                    entry_directory,
                    layout,
                )
                raster.extend(layout_raster)
                spatial.extend(layout_spatial)
                resources.update(layout_resources)
        except RepositoryError as error:
            if not self._recoverable_layout_error(error):
                raise
            raster.append(
                self._layout_inventory_placeholder(
                    item_id,
                    diagnostic_code=error.code,
                )
            )

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

    @staticmethod
    def _recoverable_capture_manifest_error(error: RepositoryError) -> bool:
        """Separate malformed persisted data from authority read failures."""

        if error.details.get("section") != "capture" or error.code not in {
            "invalid_capture_photo_assets",
            "unsupported_capture_photo_assets",
        }:
            return False
        # A failed verified read can surface through the same decode boundary.
        # It remains a repository failure: never turn a concurrent authority
        # replacement into a benign unavailable placeholder.
        cause_type = error.details.get("cause_type")
        return cause_type is None or error.details.get(
            "failure_kind"
        ) == "decode"

    @staticmethod
    def _recoverable_layout_error(error: RepositoryError) -> bool:
        """Recover malformed provider data, never failed authority reads."""

        if error.details.get("section") != "layout" or error.code not in {
            "invalid_mistral_layout",
            "unsupported_mistral_layout",
        }:
            return False
        cause_type = error.details.get("cause_type")
        return cause_type is None or error.details.get(
            "failure_kind"
        ) == "decode"

    def _capture_inventory_placeholder(
        self,
        item_id: str,
        *,
        capture_id: str,
        state: ResourceState,
        diagnostic_code: str,
    ) -> RasterArtifactView:
        """Represent an unreadable capture inventory without inventing files."""

        if state not in {ResourceState.MISSING, ResourceState.UNAVAILABLE}:
            raise TypeError("capture inventory placeholder state is invalid")
        identity = _opaque_identity(
            "capture",
            capture_id,
            "inventory-placeholder",
        )
        artifact_id = f"{identity}:display"
        source = RasterSourceRef(
            "capture",
            _digest_revision(
                "capture",
                {
                    "capture_id": capture_id,
                    "inventory_state": state.value,
                },
            ),
            identity,
            _digest_revision(
                "canvas",
                {
                    "capture_id": capture_id,
                    "inventory_state": state.value,
                },
            ),
        )
        extensions = _public_extensions(
            {
                "capture_order": 0,
                "capture_inventory": {
                    "state": state.value,
                    "diagnostic_code": diagnostic_code,
                },
                CORRECTION_TARGET_AUTHORITY_EXTENSION: {
                    "state": "missing",
                },
                "corrections_ui": {"annotation_frame": "canvas"},
            }
        )
        return RasterArtifactView(
            key=RasterArtifactKey(item_id, artifact_id),
            revision=_digest_revision(
                "artifact",
                {
                    "artifact_id": artifact_id,
                    "capture_id": capture_id,
                    "resource_state": state.value,
                    "diagnostic_code": diagnostic_code,
                },
            ),
            kind="captured-image",
            media_type=_UNKNOWN_IMAGE_MEDIA_TYPE,
            content_sha256=hashlib.sha256(
                f"capture-inventory\0{capture_id}\0{state.value}".encode(
                    "utf-8"
                )
            ).hexdigest(),
            dimensions=RasterDimensions(1, 1),
            source=source,
            resource_state=state,
            resource=None,
            label=(
                "Captured image manifest missing"
                if state is ResourceState.MISSING
                else "Captured images unavailable"
            ),
            freshness=ArtifactFreshness.UNTRACKED,
            provenance=ArtifactProvenance(
                origin="capture",
                provider_id="android",
                model="bookcapture",
            ),
            extensions=extensions,
        )

    def _layout_inventory_placeholder(
        self,
        item_id: str,
        *,
        diagnostic_code: str,
    ) -> RasterArtifactView:
        """Keep one malformed layout visible without granting its bytes."""

        identity = _opaque_identity(
            "mistral-layout",
            item_id,
            "inventory-placeholder",
        )
        artifact_id = f"{identity}:display"
        source = RasterSourceRef(
            "mistral-layout",
            _digest_revision(
                "layout",
                {
                    "item_id": item_id,
                    "inventory_state": ResourceState.UNAVAILABLE.value,
                },
            ),
            identity,
            _digest_revision(
                "canvas",
                {
                    "item_id": item_id,
                    "inventory_state": ResourceState.UNAVAILABLE.value,
                },
            ),
        )
        diagnostics = (
            {
                "scope": "mistral_layout",
                "code": diagnostic_code,
                "state": ResourceState.UNAVAILABLE.value,
            },
        )
        extensions = _public_extensions(
            {
                "artifact_diagnostics": diagnostics,
                "corrections_ui": {"annotation_frame": "canvas"},
            }
        )
        return RasterArtifactView(
            key=RasterArtifactKey(item_id, artifact_id),
            revision=_digest_revision(
                "artifact",
                {
                    "artifact_id": artifact_id,
                    "resource_state": ResourceState.UNAVAILABLE.value,
                    "diagnostics": diagnostics,
                },
            ),
            kind="artifact-diagnostic",
            media_type=_UNKNOWN_IMAGE_MEDIA_TYPE,
            content_sha256=hashlib.sha256(
                f"mistral-layout\0{item_id}\0{diagnostic_code}".encode(
                    "utf-8"
                )
            ).hexdigest(),
            dimensions=RasterDimensions(1, 1),
            source=source,
            resource_state=ResourceState.UNAVAILABLE,
            resource=None,
            label="Mistral artifacts unavailable",
            freshness=ArtifactFreshness.UNTRACKED,
            provenance=ArtifactProvenance(
                origin="ocr",
                provider_id="mistral",
            ),
            extensions=extensions,
        )

    def _legacy_capture_manifest(
        self,
        item_id: str,
        *,
        capture_id: str,
        directory: Path,
    ) -> Mapping[str, Any] | None:
        """Adapt pre-photo-contract capture generations without writing them.

        Desktop ingest has always published a private flat generation with
        ``orig_N`` and ``photo_N`` image pairs. Older generations predate
        ``photo_assets.json``. Their persisted dense numeric identity is enough
        to create stable read-only artifact identities; image bytes remain
        subject to the same safe-path, media verification, hashing, and
        resource revalidation as contract-backed captures.
        """

        originals: dict[int, str] = {}
        displays: dict[int, str] = {}
        try:
            children = list(directory.iterdir())
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _repository_error(
                "the legacy capture generation cannot be listed",
                code="corrections_artifact_repository_unavailable",
                item_id=item_id,
                section="capture",
                cause_type=type(exc).__name__,
            ) from exc
        if len(children) > _MAX_CAPTURE_ASSETS * 3 + 32:
            raise _repository_error(
                "the legacy capture generation exceeds its item limit",
                code="invalid_capture_photo_assets",
                item_id=item_id,
                section="capture",
            )
        for child in children:
            match = _LEGACY_CAPTURE_IMAGE_RE.fullmatch(child.name)
            if match is None:
                continue
            index = int(match.group(2))
            target = originals if match.group(1).casefold() == "orig" else displays
            if index in target:
                raise _repository_error(
                    "the legacy capture sequence aliases an image identity",
                    code="invalid_capture_photo_assets",
                    item_id=item_id,
                    section="capture",
                )
            target[index] = child.name
        if not displays:
            return None
        if len(displays) > _MAX_CAPTURE_ASSETS:
            raise _repository_error(
                "the legacy capture generation exceeds its image limit",
                code="invalid_capture_photo_assets",
                item_id=item_id,
                section="capture",
            )
        expected = set(range(1, len(displays) + 1))
        if set(displays) != expected or not set(originals).issubset(expected):
            raise _repository_error(
                "the legacy capture sequence is not dense and one-based",
                code="invalid_capture_photo_assets",
                item_id=item_id,
                section="capture",
            )
        assets = []
        for index in sorted(displays):
            display_name = displays[index]
            original_name = originals.get(index, display_name)
            assets.append(
                {
                    "asset_id": f"legacy-{index}",
                    "capture_order": index,
                    "capture_file": display_name,
                    "original": {
                        "reference": original_name,
                        "revision": 1,
                        "orientation": 1,
                    },
                    "display": {
                        "reference": display_name,
                        "revision": 1,
                        "orientation": 1,
                        "recipe": "legacy-desktop-import",
                        "recipe_version": "1",
                    },
                    "lifecycle": {"state": "completed"},
                    "role": {},
                    "geometry": [],
                    "processing_request": {},
                }
            )
        return {
            "schema": PHOTO_ASSETS_SCHEMA,
            "version": PHOTO_ASSETS_VERSION,
            "capture_id": capture_id,
            "legacy_fallback": True,
            "assets": assets,
            "selections": {},
            "transport": {"representation": "legacy-desktop", "version": 1},
        }

    def _capture_index_resource_state(
        self,
        item_id: str,
        directory: Path,
        reference: Any,
    ) -> tuple[ResourceState, tuple[int, ...] | None]:
        if (
            not isinstance(reference, str)
            or _RESOURCE_LEAF_RE.fullmatch(reference) is None
            or "/" in reference
            or "\\" in reference
            or reference in {".", ".."}
            or not _media_type(reference)
        ):
            return ResourceState.UNAVAILABLE, None
        path = directory / reference
        try:
            self._assert_safe_path(
                path,
                item_id=item_id,
                section="capture",
            )
            info = path.lstat()
        except FileNotFoundError:
            return ResourceState.MISSING, None
        except (OSError, RepositoryError):
            return ResourceState.UNAVAILABLE, None
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 1
            or info.st_size > _MAX_RASTER_RESOURCE_BYTES
        ):
            return ResourceState.UNAVAILABLE, None
        return ResourceState.AVAILABLE, _stable_stat_identity(info)

    @staticmethod
    def _capture_index_geometry_is_incomplete(
        record: _CaptureAssetRecord,
    ) -> bool:
        """Validate geometry metadata without decoding its display image."""

        raw_geometry = record.raw.get("geometry")
        if raw_geometry is None or raw_geometry == []:
            return False
        if (
            isinstance(raw_geometry, (str, bytes))
            or not isinstance(raw_geometry, Sequence)
            or len(raw_geometry) > _MAX_CAPTURE_GEOMETRIES_PER_ASSET
        ):
            return True
        original_revision = _positive_integer(
            record.original.get("revision")
        )
        display_revision = _positive_integer(record.display.get("revision"))
        if original_revision is None or display_revision is None:
            return True
        original_sha256 = _sha256(
            record.imported.get("source_checksum")
            or record.original.get("sha256")
        )
        display_sha256 = _sha256(
            record.imported.get("derivative_checksum")
            or record.display.get("sha256")
        )
        phone_display_sha256 = _sha256(record.display.get("sha256"))
        display_width = _non_negative_integer(record.display.get("width")) or 0
        display_height = (
            _non_negative_integer(record.display.get("height")) or 0
        )
        display_orientation = _orientation_degrees(
            1
            if record.imported
            else _orientation(record.display.get("orientation"))
        )
        geometries = tuple(raw_geometry)
        if any(not isinstance(geometry, Mapping) for geometry in geometries):
            return True
        matching_pins = tuple(
            geometry
            for geometry in geometries
            if (
                display_sha256
                and _sha256(geometry.get("display_sha256"))
                == display_sha256
            )
        )
        phone_frame_records = tuple(
            geometry
            for geometry in geometries
            if not _sha256(geometry.get("display_sha256"))
        )
        if record.imported:
            if matching_pins:
                candidates = matching_pins
            elif (
                display_sha256
                and phone_display_sha256 == display_sha256
            ):
                candidates = phone_frame_records
            else:
                candidates = ()
        else:
            candidates = (*matching_pins, *phone_frame_records)
        if not candidates:
            return True
        seen_regions: set[tuple[str, str, str]] = set()
        for geometry in candidates:
            geometry_width = _non_negative_integer(geometry.get("width")) or 0
            geometry_height = (
                _non_negative_integer(geometry.get("height")) or 0
            )
            geometry_display_sha256 = _sha256(
                geometry.get("display_sha256")
            )
            if (
                geometry.get("asset_id") != record.asset_id
                or geometry.get("coordinate_space") != "display_normalized"
                or (
                    not geometry_display_sha256
                    and _positive_integer(geometry.get("source_revision"))
                    != original_revision
                )
                or (
                    not geometry_display_sha256
                    and _positive_integer(geometry.get("display_revision"))
                    != display_revision
                )
                or (
                    original_sha256
                    and _sha256(geometry.get("source_sha256"))
                    != original_sha256
                )
                or (
                    (not geometry_display_sha256 or not record.imported)
                    and display_width
                    and geometry_width
                    and geometry_width != display_width
                )
                or (
                    (not geometry_display_sha256 or not record.imported)
                    and display_height
                    and geometry_height
                    and geometry_height != display_height
                )
                or geometry.get("orientation") != display_orientation
            ):
                return True
            regions = geometry.get("regions")
            if (
                isinstance(regions, (str, bytes))
                or not isinstance(regions, Sequence)
                or len(regions) > _MAX_CAPTURE_REGIONS_PER_GEOMETRY
            ):
                return True
            engine = _public_text(geometry.get("engine"), maximum=80)
            model = _public_text(geometry.get("model"), maximum=120)
            for region in regions:
                if not isinstance(region, Mapping):
                    return True
                region_id = _public_text(
                    region.get("id"),
                    maximum=120,
                ).strip()
                polygon = region.get("polygon")
                if (
                    not region_id
                    or isinstance(polygon, (str, bytes))
                    or not isinstance(polygon, Sequence)
                    or not 3 <= len(polygon) <= _MAX_CAPTURE_POLYGON_POINTS
                ):
                    return True
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
                except ValidationError:
                    return True
                if len(points) != len(polygon):
                    return True
                identity = (engine, model, region_id)
                if identity in seen_regions:
                    return True
                seen_regions.add(identity)
        return False

    @staticmethod
    def _capture_inventory_index_hint(
        capture_id: str,
        *,
        state: ResourceState,
        diagnostic_code: str,
        diagnostic_scopes: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        if state not in {ResourceState.MISSING, ResourceState.UNAVAILABLE}:
            raise TypeError("capture inventory hint state is invalid")
        namespace = _opaque_identity(
            "capture",
            capture_id,
            "inventory-placeholder",
        )
        return {
            "artifact_id": f"{namespace}:display",
            "revision": _digest_revision(
                "index",
                {
                    "capture_id": capture_id,
                    "inventory_state": state.value,
                    "diagnostic_code": diagnostic_code,
                },
            ),
            "capture_order": 0,
            "label": (
                "Captured image manifest missing"
                if state is ResourceState.MISSING
                else "Captured images unavailable"
            ),
            "representation_id": "capture",
            "canvas_id": namespace,
            "effective_category": "other",
            "resource_state": state.value,
            "import_state": state.value,
            "freshness": ArtifactFreshness.UNTRACKED.value,
            "imported_at": "",
            "diagnostic_scopes": tuple(sorted(set(diagnostic_scopes))),
        }

    def _capture_index_hints(
        self,
        item_id: str,
        *,
        capture_id: str,
        directory: Path,
        manifest: Mapping[str, Any],
        authority_hints: dict[str, Mapping[str, Any]] | None = None,
    ) -> tuple[Mapping[str, Any], ...]:
        records, representation_revision, legacy = (
            self._capture_manifest_records(item_id, capture_id, manifest)
        )
        hints: list[Mapping[str, Any]] = []
        for record in records:
            raw = record.raw
            asset_id = record.asset_id
            order = record.order
            original = record.original
            display = record.display
            imported = record.imported
            original_ref = imported.get("raw_ref") or original.get("reference")
            display_ref = (
                imported.get("display_ref")
                or display.get("reference")
            )
            original_state, original_identity = (
                self._capture_index_resource_state(
                    item_id,
                    directory,
                    original_ref,
                )
                if record.original_valid
                else (ResourceState.UNAVAILABLE, None)
            )
            resource_state, display_identity = (
                self._capture_index_resource_state(
                    item_id,
                    directory,
                    display_ref,
                )
                if record.display_valid
                else (ResourceState.UNAVAILABLE, None)
            )
            rendition_states = tuple(
                (
                    "missing"
                    if state is ResourceState.MISSING
                    else "unavailable"
                    if state is ResourceState.UNAVAILABLE
                    else "legacy"
                    if legacy
                    else "ready"
                )
                for state in (original_state, resource_state)
            )
            record_diagnostic_scopes: set[str] = set()
            if self._capture_index_geometry_is_incomplete(record):
                record_diagnostic_scopes.add("capture_geometry")
            if (
                not record.original_valid
                or not record.display_valid
                or original_state is not ResourceState.AVAILABLE
                or resource_state is not ResourceState.AVAILABLE
            ):
                record_diagnostic_scopes.add("capture_rendition")
            if all(state == "missing" for state in rendition_states):
                import_state = "missing"
            elif all(state == "unavailable" for state in rendition_states):
                import_state = "unavailable"
            elif record_diagnostic_scopes & {
                "capture_geometry",
                "capture_rendition",
            }:
                import_state = "partial"
            elif all(state == "legacy" for state in rendition_states):
                import_state = "legacy"
            elif any(state != "ready" for state in rendition_states):
                import_state = "partial"
            else:
                import_state = "ready"
            namespace = record.namespace
            artifact_id = record.display_id
            display_sha256 = _sha256(
                imported.get("derivative_checksum")
                or display.get("sha256")
            )
            if authority_hints is not None and display_sha256:
                authority_hints[artifact_id.casefold()] = {
                    "artifact_id": artifact_id,
                    "source_revision": f"bytes:{display_sha256}",
                    "source_sha256": display_sha256,
                    "representation_id": "capture",
                    "representation_revision": representation_revision,
                    "canvas_id": namespace,
                }
            assignments = self._capture_assignments(
                item_id,
                raw.get("role"),
                asset_id=asset_id,
            )
            effective_category = "other"
            for origin in (AssignmentOrigin.MANUAL, AssignmentOrigin.SUGGESTED):
                assignment = next(
                    (
                        value
                        for value in assignments
                        if value.origin is origin
                    ),
                    None,
                )
                if assignment is not None:
                    effective_category = assignment.category
                    break
            lifecycle = raw.get("lifecycle")
            lifecycle_state = (
                str(lifecycle.get("state") or "")
                if isinstance(lifecycle, Mapping)
                else ""
            )
            freshness = (
                ArtifactFreshness.STALE
                if lifecycle_state in {"failed", "cancelled"}
                else ArtifactFreshness.CURRENT
                if resource_state is ResourceState.AVAILABLE
                else ArtifactFreshness.UNTRACKED
            )
            revision = _digest_revision(
                "index",
                {
                    "capture_id": capture_id,
                    "asset_id": asset_id,
                    "capture_order": order,
                    "original": original,
                    "display": display,
                    "import": imported,
                    "role": raw.get("role"),
                    "lifecycle": raw.get("lifecycle"),
                    "geometry": raw.get("geometry"),
                    "original_valid": record.original_valid,
                    "display_valid": record.display_valid,
                    "original_file_identity": original_identity,
                    "display_file_identity": display_identity,
                    "imported_at": record.imported_at,
                    "diagnostic_scopes": tuple(
                        sorted(record_diagnostic_scopes)
                    ),
                },
            )
            hints.append(
                {
                    "artifact_id": artifact_id,
                    "revision": revision,
                    "capture_order": order,
                    "label": f"Capture {order} display",
                    "representation_id": "capture",
                    "canvas_id": namespace,
                    "effective_category": effective_category,
                    "resource_state": resource_state.value,
                    "import_state": import_state,
                    "freshness": freshness.value,
                    "imported_at": "" if legacy else record.imported_at,
                    "diagnostic_scopes": tuple(
                        sorted(record_diagnostic_scopes)
                    ),
                }
            )
        return tuple(
            sorted(
                hints,
                key=lambda value: (
                    value["capture_order"],
                    value["artifact_id"],
                ),
            )
        )

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

    def _capture_manifest_records(
        self,
        item_id: str,
        capture_id: str,
        manifest: Mapping[str, Any],
    ) -> tuple[
        tuple[_CaptureAssetRecord, ...],
        str,
        bool,
    ]:
        """Validate the complete identity catalogue without reading rasters."""

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
        imported_at = ""
        desktop_import = manifest.get("desktop_import")
        if isinstance(desktop_import, Mapping):
            imported_at = _public_text(
                desktop_import.get("imported_at"),
                maximum=64,
            ).strip()
            rows = desktop_import.get("assets")
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                for row in rows:
                    if not isinstance(row, Mapping):
                        continue
                    imported_id = row.get("asset_id")
                    if (
                        isinstance(imported_id, str)
                        and imported_id not in import_rows
                    ):
                        import_rows[imported_id] = row

        records: list[_CaptureAssetRecord] = []
        seen_assets: set[str] = set()
        seen_orders: set[int] = set()
        manifest_source: list[Mapping[str, Any]] = []
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
            original_value = raw.get("original")
            display_value = raw.get("display")
            original_valid = isinstance(original_value, Mapping)
            display_valid = isinstance(display_value, Mapping)
            original = original_value if original_valid else {}
            display = display_value if display_valid else {}
            seen_assets.add(asset_id)
            seen_orders.add(order)
            namespace = _opaque_identity("capture", capture_id, asset_id)
            records.append(
                _CaptureAssetRecord(
                    raw=raw,
                    asset_id=asset_id,
                    order=order,
                    original=original,
                    display=display,
                    imported=import_rows.get(asset_id, {}),
                    namespace=namespace,
                    original_id=f"{namespace}:original",
                    display_id=f"{namespace}:display",
                    original_valid=original_valid,
                    display_valid=display_valid,
                    imported_at=imported_at,
                )
            )
            manifest_source.append(
                {
                    "asset_id": asset_id,
                    "sha256": original.get("sha256"),
                    "revision": original.get("revision"),
                }
            )
        return (
            tuple(records),
            _digest_revision(
                "capture",
                {"capture_id": capture_id, "originals": manifest_source},
            ),
            manifest.get("legacy_fallback") is True,
        )

    def _project_capture(
        self,
        item_id: str,
        capture_id: str,
        directory: Path,
        manifest: Mapping[str, Any],
        *,
        artifact_id: str = "",
    ) -> tuple[
        tuple[RasterArtifactView, ...],
        tuple[SpatialAnnotationView, ...],
        Mapping[tuple[str, str, str], _ResolvedRasterCandidate],
    ]:
        records, representation_revision, legacy_capture = (
            self._capture_manifest_records(item_id, capture_id, manifest)
        )
        if artifact_id:
            records = tuple(
                record
                for record in records
                if artifact_id in {record.original_id, record.display_id}
            )
        values: list[RasterArtifactView] = []
        spatial: list[SpatialAnnotationView] = []
        resources: dict[tuple[str, str, str], _ResolvedRasterCandidate] = {}
        for record in records:
            raw = record.raw
            asset_id = record.asset_id
            order = record.order
            original = record.original
            display = record.display
            imported = record.imported
            original_valid = record.original_valid
            display_valid = record.display_valid
            imported_at = record.imported_at
            original_ref = imported.get("raw_ref") or original.get("reference")
            display_ref = imported.get("display_ref") or display.get("reference")
            original_sha = _sha256(
                imported.get("source_checksum") or original.get("sha256")
            )
            display_sha = _sha256(
                imported.get("derivative_checksum") or display.get("sha256")
            )
            original_id = record.original_id
            display_id = record.display_id
            canvas_id = record.namespace
            original_observation = (
                self._observe_resource(
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
                if original_valid
                else self._placeholder_observation(
                    artifact_id=original_id,
                    state=ResourceState.UNAVAILABLE,
                    diagnostic_code="capture_rendition_invalid",
                    media_type=_media_type(original_ref),
                    declared_sha256=original_sha,
                    width=None,
                    height=None,
                    orientation=1,
                )
            )
            display_observation = (
                self._observe_resource(
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
                if display_valid
                else self._placeholder_observation(
                    artifact_id=display_id,
                    state=ResourceState.UNAVAILABLE,
                    diagnostic_code="capture_rendition_invalid",
                    media_type=_media_type(display_ref),
                    declared_sha256=display_sha,
                    width=None,
                    height=None,
                    orientation=1,
                )
            )
            original_source = RasterSourceRef(
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
            display_source = RasterSourceRef(
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
            original_view = self._capture_view(
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
                    "legacy_capture": legacy_capture,
                    **({"imported_at": imported_at} if imported_at else {}),
                    "corrections_ui": {"annotation_frame": "canvas"},
                    "android": _unknown_fields(raw, _PHOTO_ASSET_FIELDS),
                    "rendition": _unknown_fields(
                        original,
                        _PHOTO_RENDITION_FIELDS,
                    ),
                },
            )
            display_lineage = (
                (
                    RasterLineageRef(
                        original_id,
                        original_view.revision,
                        "derived_from",
                    ),
                )
                if original_observation.state is ResourceState.AVAILABLE
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
            raw_geometry = raw.get("geometry")
            geometry_values: tuple[SpatialAnnotationView, ...] = ()
            geometry_codes: tuple[str, ...] = ()
            has_geometry = (
                raw_geometry is not None
                and (
                    not isinstance(raw_geometry, Sequence)
                    or isinstance(raw_geometry, (str, bytes))
                    or bool(raw_geometry)
                )
            )
            # The projected display is the import derivative whenever a
            # desktop import ran (display_ref/display_sha prefer the import
            # row above), so geometry readiness must pin against the same
            # bytes â€” not the phone's own display rendition, which the
            # desktop derivation replaced.
            geometry_source_ready = (
                display_observation.state is ResourceState.AVAILABLE
                and (
                    not imported
                    or (
                        bool(display_sha)
                        and display_observation.content_sha256 == display_sha
                    )
                )
            )
            if geometry_source_ready:
                (
                    geometry_values,
                    geometry_codes,
                ) = self._capture_geometry_annotations(
                    item_id,
                    capture_id=capture_id,
                    asset_id=asset_id,
                    raw_geometry=raw_geometry,
                    original=original,
                    display=display,
                    original_sha256=_sha256(original.get("sha256")),
                    display_sha256=display_sha,
                    imported=bool(imported),
                    display_dimensions=display_observation.dimensions,
                    source=SpatialSourceRef(
                        display_source.representation_id,
                        display_source.representation_revision,
                        display_source.canvas_id,
                        display_source.canvas_revision,
                    ),
                    display_artifact_id=display_id,
                )
            elif has_geometry:
                geometry_codes = ("capture_geometry_unavailable",)
            geometry_diagnostics = [
                {
                    "scope": "capture_geometry",
                    "code": code,
                    "state": ResourceState.UNAVAILABLE.value,
                    "component": "display",
                }
                for code in geometry_codes
            ]
            display_view = self._capture_view(
                item_id,
                artifact_id=display_id,
                # The display is the stable, user-facing capture slot. Its
                # recipe/provenance may advance, but moving it between public
                # buckets would invalidate navigation and editor deep links.
                kind="captured-image",
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
                    "legacy_capture": legacy_capture,
                    **({"imported_at": imported_at} if imported_at else {}),
                    "recipe": recipe,
                    "corrections_ui": {"annotation_frame": "canvas"},
                    "android": _unknown_fields(raw, _PHOTO_ASSET_FIELDS),
                    "rendition": _unknown_fields(
                        display,
                        _PHOTO_RENDITION_FIELDS,
                    ),
                    **(
                        {"artifact_diagnostics": geometry_diagnostics}
                        if geometry_diagnostics
                        else {}
                    ),
                },
            )
            values.extend((original_view, display_view))
            for view, observation in (
                (original_view, original_observation),
                (display_view, display_observation),
            ):
                if view.resource is not None and observation.resolved is not None:
                    resources[
                        (
                            view.resource.resource_id,
                            view.resource.revision,
                            view.resource.variant,
                        )
                    ] = observation.resolved
            spatial.extend(geometry_values)
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
        display_sha256: str = "",
        imported: bool = False,
        display_dimensions: RasterDimensions,
        source: SpatialSourceRef,
        display_artifact_id: str,
    ) -> tuple[
        tuple[SpatialAnnotationView, ...],
        tuple[str, ...],
    ]:
        if raw_geometry is None:
            return (), ()
        if (
            isinstance(raw_geometry, (str, bytes))
            or not isinstance(raw_geometry, Sequence)
        ):
            return (), ("capture_geometry_invalid",)
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
            return (
                (),
                ("capture_geometry_invalid",) if raw_geometry else (),
            )

        values: list[SpatialAnnotationView] = []
        seen_ids: set[str] = set()
        diagnostics: set[str] = set()
        if len(raw_geometry) > _MAX_CAPTURE_GEOMETRIES_PER_ASSET:
            diagnostics.add("capture_geometry_partial")
        if any(
            not isinstance(geometry, Mapping)
            for geometry in raw_geometry[
                :_MAX_CAPTURE_GEOMETRIES_PER_ASSET
            ]
        ):
            diagnostics.add("capture_geometry_invalid")
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
        # Desktop-remapped records pin themselves to a display derivative by
        # content hash; process them first so a phone-frame record that no
        # longer describes the projected display can be skipped without a
        # misleading staleness diagnostic.
        geometries.sort(
            key=lambda geometry: 0 if _sha256(
                geometry.get("display_sha256")) else 1,
        )
        accepted_pinned = False
        unprojected_phone_frame = False
        for geometry_index, geometry in enumerate(geometries):
            geometry_width = _non_negative_integer(geometry.get("width")) or 0
            geometry_height = _non_negative_integer(geometry.get("height")) or 0
            geometry_sha256 = _sha256(geometry.get("source_sha256"))
            record_display_sha = _sha256(geometry.get("display_sha256"))
            if record_display_sha:
                # Pinned to a specific derivative. A different hash means the
                # record describes another rendition generation â€” not ours,
                # and not an integrity problem.
                if (
                    not display_sha256
                    or record_display_sha != display_sha256
                ):
                    continue
                if (
                    geometry.get("asset_id") != asset_id
                    or geometry.get("coordinate_space")
                    != "display_normalized"
                    or (original_sha256 and geometry_sha256 != original_sha256)
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
                    diagnostics.add("capture_geometry_stale")
                    continue
                accepted_pinned = True
            elif imported:
                # A phone-frame record describes the phone's own rendition.
                # After a desktop import it projects only when the granted
                # display provably IS that rendition (content hash match) â€”
                # dimensions alone cannot distinguish a re-encode from a
                # same-sized warp. Otherwise a desktop-pinned remap record
                # owns this display; when none exists the geometry is real
                # but unprojectable, which the unavailable diagnostic below
                # reports.
                if accepted_pinned:
                    continue
                if (
                    not display_sha256
                    or _sha256(display.get("sha256")) != display_sha256
                ):
                    unprojected_phone_frame = True
                    continue
                if (
                    geometry.get("asset_id") != asset_id
                    or geometry.get("coordinate_space")
                    != "display_normalized"
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
                    diagnostics.add("capture_geometry_stale")
                    continue
            elif (
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
                diagnostics.add("capture_geometry_stale")
                continue
            regions = geometry.get("regions")
            if (
                isinstance(regions, (str, bytes))
                or not isinstance(regions, Sequence)
            ):
                diagnostics.add("capture_geometry_invalid")
                continue
            if len(regions) > _MAX_CAPTURE_REGIONS_PER_GEOMETRY:
                diagnostics.add("capture_geometry_partial")
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
                    diagnostics.add("capture_geometry_invalid")
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
                    diagnostics.add("capture_geometry_invalid")
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
                        diagnostics.add("capture_geometry_invalid")
                        continue
                    selector = NormalizedPolygonSelector(
                        "display_normalized",
                        source.canvas_revision,
                        points,
                    )
                except ValidationError:
                    diagnostics.add("capture_geometry_invalid")
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
                    diagnostics.add("capture_geometry_invalid")
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
        if unprojected_phone_frame and not values:
            # Real phone geometry exists but no record describes the display
            # actually granted (the desktop derivative). The remap backfill
            # (tools/backfill_capture_geometry.py) or a re-import resolves
            # this.
            diagnostics.add("capture_geometry_unavailable")
        return tuple(values), tuple(sorted(diagnostics))

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
        extension_values = dict(extensions)
        diagnostics = extension_values.get("artifact_diagnostics")
        diagnostic_values = (
            list(diagnostics)
            if (
                isinstance(diagnostics, Sequence)
                and not isinstance(diagnostics, (str, bytes))
            )
            else []
        )
        if observation.diagnostic_code:
            diagnostic_values.append(
                {
                    "scope": "capture_rendition",
                    "code": observation.diagnostic_code,
                    "state": observation.state.value,
                    "component": (
                        "original"
                        if artifact_id.endswith(":original")
                        else "display"
                    ),
                }
            )
        if diagnostic_values:
            extension_values["artifact_diagnostics"] = diagnostic_values
        public_extensions = _public_extensions(extension_values)
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

    @staticmethod
    def _resource_diagnostic_code(section: str, reason: str) -> str:
        prefix = (
            "capture_rendition"
            if section == "capture"
            else "mistral_figure"
        )
        return f"{prefix}_{reason}"

    def _placeholder_observation(
        self,
        *,
        artifact_id: str,
        state: ResourceState,
        diagnostic_code: str,
        media_type: str,
        declared_sha256: str,
        width: int | None,
        height: int | None,
        orientation: int,
        integrity_mismatch: bool = False,
    ) -> _ResourceObservation:
        if state not in {ResourceState.MISSING, ResourceState.UNAVAILABLE}:
            raise TypeError("placeholder observation state is invalid")
        content_sha256 = declared_sha256 or hashlib.sha256(
            f"{artifact_id}\0{diagnostic_code}".encode("utf-8")
        ).hexdigest()
        return _ResourceObservation(
            state,
            media_type or _UNKNOWN_IMAGE_MEDIA_TYPE,
            content_sha256,
            RasterDimensions(width or 1, height or 1, orientation),
            None,
            integrity_mismatch=integrity_mismatch,
            diagnostic_code=diagnostic_code,
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
    ) -> _ResourceObservation:
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
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.UNAVAILABLE,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "invalid",
                ),
                media_type=expected_media_type,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
            )
        path = directory / reference
        try:
            authority = self._assert_safe_path(
                path,
                item_id=item_id,
                section=section,
            )
        except RepositoryError:
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.UNAVAILABLE,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "unavailable",
                ),
                media_type=expected_media_type,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
            )
        try:
            info = path.lstat()
        except FileNotFoundError:
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.MISSING,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "missing",
                ),
                media_type=expected_media_type,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
            )
        except OSError:
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.UNAVAILABLE,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "unavailable",
                ),
                media_type=expected_media_type,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
            )
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size < 1
            or info.st_size > _MAX_RASTER_RESOURCE_BYTES
        ):
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.UNAVAILABLE,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "unavailable",
                ),
                media_type=expected_media_type,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
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
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.UNAVAILABLE,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "unavailable",
                ),
                media_type=expected_media_type,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if snapshot is not None:
                snapshot.close()
        if verified is None:
            return self._placeholder_observation(
                artifact_id=artifact_id,
                state=ResourceState.UNAVAILABLE,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "invalid",
                ),
                media_type=_UNKNOWN_IMAGE_MEDIA_TYPE,
                declared_sha256=declared_sha256,
                width=width,
                height=height,
                orientation=orientation,
                integrity_mismatch=True,
            )
        # Persisted Android dimensions describe the sending device's
        # rendition. A desktop import may have normalized it again, so the
        # bytes actually being granted are authoritative here.
        width, height, actual_media_type = verified
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
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "invalid",
                ),
            )
        if declared_sha256 and actual_sha256 != declared_sha256:
            return _ResourceObservation(
                ResourceState.UNAVAILABLE,
                actual_media_type,
                declared_sha256,
                dimensions,
                None,
                integrity_mismatch=True,
                diagnostic_code=self._resource_diagnostic_code(
                    section,
                    "invalid",
                ),
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
                    **(
                        {
                            "artifact_diagnostics": [
                                {
                                    "scope": "mistral_figure",
                                    "code": (
                                        draft.observation.diagnostic_code
                                    ),
                                    "state": (
                                        draft.observation.state.value
                                    ),
                                }
                            ]
                        }
                        if draft.observation.diagnostic_code
                        else {}
                    ),
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
                metadata_assertions=draft.metadata_assertions,
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
            metadata_assertions = _public_metadata_assertions(
                value.get("ext") if isinstance(value.get("ext"), Mapping) else {},
                artifact_id=artifact_id,
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
                "metadata_assertions": [
                    assertion.as_dict() for assertion in metadata_assertions
                ],
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
                    metadata_assertions=metadata_assertions,
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
