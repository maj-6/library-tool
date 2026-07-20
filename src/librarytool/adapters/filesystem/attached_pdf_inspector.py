"""Exact, path-private PDF inspection for canvas preparation.

The canvas preparation repository deliberately receives only a representation
identity and revision.  This adapter joins that safe snapshot to a host-owned
attachment authority which must return the exact tracked bytes for that
revision.  A mandatory content digest and size make a stale path or replaced
reference observable before any canvas identity is allocated.

The source is copied once into a bounded, private temporary snapshot while it
is hashed.  PDF parsing then uses only that immutable snapshot.  Consequently
page evidence cannot describe one version of a file while the asset digest
describes another, even when the referenced path changes during inspection.
Absolute attachment paths never enter a canvas observation or public view.

External references intentionally may resolve through parent-directory aliases
or links outside the workspace.  Digest, size, open-file, and path signatures
guarantee which bytes were inspected; they do not promise parent containment
or a no-redirect filesystem policy.  Rejecting only a final symlink would give
a false guarantee because any parent component can redirect too.

The byte and declared-page limits below are cheap in-process policy checks.
They are not hostile-parser isolation: constructing ``PdfReader`` and walking
a malformed page tree can consume resources before a trustworthy count exists.
A later production boundary for untrusted PDFs must run parsing in a killable,
resource-limited worker process.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from pathlib import Path
from typing import Any, BinaryIO, TypeAlias

from ...engine.canvas_commands import CanvasPreparationRepresentationSnapshot
from ...engine.canvases import CanvasExtent
from ...engine.errors import (
    ConflictError,
    EngineError,
    RepositoryError,
    ValidationError,
)
from .canvas_preparation_repository import (
    FilesystemCanvasEvidence,
    FilesystemCanvasInspection,
    FilesystemCanvasObservation,
)


# Fixed algorithm/profile identity.  It records snapshot-bound geometry under
# the controlled pypdf 6 API family; it is deliberately not assembled from the
# installed package's mutable full version string.
ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE = (
    "attached-pdf-snapshot-geometry-v1-pypdf6"
)
ATTACHED_PDF_PARSER_ISOLATION = "in-process-not-hostile-isolated"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MAX_PAGES = 100_000
_DEFAULT_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_DIMENSION_MPT = 2_147_483_647
_MAX_DIMENSION_MPT_DECIMAL = Decimal(_MAX_DIMENSION_MPT)
_MILLIPOINTS_PER_POINT = Decimal(1000)
_MIN_USER_UNIT = Decimal("0.000001")
_MAX_USER_UNIT = Decimal(75_000)
_MAX_ABSOLUTE_ROTATION = Decimal(360_000)
_MAX_DECIMAL_TOKEN_CHARACTERS = 128
_CONTROLLED_PYPDF_MAJOR = 6
_MAX_PDF_OBJECT_RESOLUTION_HOPS = 8


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


def _revision(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(character.isspace() for character in value)
        or any(
            ord(character) == 127
            or ord(character) < 32
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise ValueError("representation_revision is not a valid revision")
    return value


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, Path):
        raise TypeError("path must be a pathlib.Path")
    raw = os.fspath(value)
    if (
        not value.is_absolute()
        or not raw
        or len(raw) > 32_767
        or any(
            ord(character) == 127
            or ord(character) < 32
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in raw
        )
    ):
        raise ValueError("path must be a safe absolute path")
    return value


@dataclass(frozen=True, slots=True)
class FilesystemAttachedPdfAssetSnapshot:
    """Private tracked attachment selected for one exact representation.

    ``path`` is an adapter-only locator.  The digest and size are required;
    untracked legacy references must be explicitly attached or refreshed
    before they can become an authoritative canvas source.
    """

    item_id: str
    representation_id: str
    representation_revision: str
    path: Path = field(repr=False)
    content_sha256: str = field(repr=False)
    size: int
    media_type: str = "application/pdf"

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _identifier(self.representation_id, "representation_id")
        _revision(self.representation_revision)
        object.__setattr__(self, "path", _absolute_path(self.path))
        if (
            not isinstance(self.content_sha256, str)
            or not _SHA256_RE.fullmatch(self.content_sha256)
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("size must be a non-negative integer")
        if self.media_type != "application/pdf":
            raise ValueError("media_type must be application/pdf")


AttachedPdfAssetLookup: TypeAlias = Callable[
    [str, str, str], FilesystemAttachedPdfAssetSnapshot | None
]


@dataclass(frozen=True, slots=True)
class _FileSignature:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    # ``os.path.samestat`` deliberately compares only the platform's file
    # identity fields.  Keep the friendlier names used by this adapter while
    # exposing the stat-result protocol it expects.
    @property
    def st_dev(self) -> int:
        return self.device

    @property
    def st_ino(self) -> int:
        return self.inode


def _signature(value: os.stat_result) -> _FileSignature:
    return _FileSignature(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        mode=int(value.st_mode),
        size=int(value.st_size),
        modified_ns=int(value.st_mtime_ns),
        changed_ns=int(value.st_ctime_ns),
    )


def _path_signature(path: Path) -> _FileSignature:
    return _signature(path.stat())


def _stream_signature(stream: BinaryIO) -> _FileSignature:
    return _signature(os.fstat(stream.fileno()))


def _copy_source(
    source: BinaryIO,
    snapshot: BinaryIO,
    *,
    maximum_bytes: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(_COPY_CHUNK_BYTES)
        if not chunk:
            break
        size += len(chunk)
        if size > maximum_bytes:
            raise ValidationError(
                "the attached PDF exceeds the inspection limit",
                code="canvas_pdf_asset_too_large",
                details={"maximum_bytes": maximum_bytes},
            )
        digest.update(chunk)
        snapshot.write(chunk)
    return digest.hexdigest(), size


def _decimal_number(value: Any, *, field_name: str) -> Decimal:
    try:
        token = str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not numeric") from exc
    if not token or len(token) > _MAX_DECIMAL_TOKEN_CHARACTERS:
        raise ValueError(f"{field_name} is not numeric")
    try:
        number = Decimal(token)
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is not numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} is not finite")
    return number


def _page_user_unit(page: Any) -> Decimal:
    value = _decimal_number(
        page.get("/UserUnit", 1),
        field_name="page user unit",
    )
    if value < _MIN_USER_UNIT or value > _MAX_USER_UNIT:
        raise ValueError("page user unit is outside the supported range")
    return value


def _millipoints(
    value: Any,
    *,
    user_unit: Decimal,
    field_name: str,
) -> int:
    points = _decimal_number(value, field_name=field_name)
    if points <= 0:
        raise ValueError(f"{field_name} is outside the supported range")
    # Check Decimal bounds before multiplication or integer construction.  A
    # compact hostile token such as ``1e1000000000`` therefore fails by one
    # comparison instead of attempting to allocate a billion-digit integer.
    maximum_points = _MAX_DIMENSION_MPT_DECIMAL / (
        _MILLIPOINTS_PER_POINT * user_unit
    )
    if points > maximum_points:
        raise ValueError(f"{field_name} is outside the supported range")
    scaled = points * user_unit * _MILLIPOINTS_PER_POINT
    rounded = scaled.to_integral_value(rounding=ROUND_HALF_UP)
    if rounded <= 0 or rounded > _MAX_DIMENSION_MPT_DECIMAL:
        raise ValueError(f"{field_name} is outside the supported range")
    return int(rounded)


def _page_rotation(page: Any) -> int:
    number = _decimal_number(
        page.get("/Rotate", 0),
        field_name="page rotation",
    )
    if number < -_MAX_ABSOLUTE_ROTATION or number > _MAX_ABSOLUTE_ROTATION:
        raise ValueError("page rotation is outside the supported range")
    integral = number.to_integral_value()
    if number != integral:
        raise ValueError("page rotation is invalid")
    # ``number`` is bounded above, so this conversion cannot construct a
    # pathological giant integer.
    rotation = int(integral) % 360
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("page rotation is invalid")
    return rotation


def _resolved_pdf_object(value: Any, *, field_name: str) -> Any:
    """Resolve a short indirect-object chain without trusting recursion."""

    current = value
    seen: set[int] = set()
    for _hop in range(_MAX_PDF_OBJECT_RESOLUTION_HOPS + 1):
        identity = id(current)
        if identity in seen:
            raise ValueError(f"{field_name} contains an indirect-object cycle")
        seen.add(identity)
        resolver = getattr(current, "get_object", None)
        if not callable(resolver):
            return current
        try:
            resolved = resolver()
        except Exception as exc:
            raise ValueError(f"{field_name} cannot be resolved") from exc
        if resolved is current:
            return current
        current = resolved
    raise ValueError(f"{field_name} has too many levels of indirection")


def _declared_page_count(reader: Any) -> int:
    """Read the root page-tree count without materializing every page.

    This is only a cheap preflight.  Resolving the trailer/catalog/page-tree
    root still invokes pypdf, and a dishonest count is checked again after the
    ordinary page collection is materialized.
    """

    root = _resolved_pdf_object(
        reader.trailer.get("/Root"),
        field_name="PDF catalog",
    )
    try:
        raw_pages = root.get("/Pages")
    except Exception as exc:
        raise ValueError("PDF page tree is invalid") from exc
    pages = _resolved_pdf_object(raw_pages, field_name="PDF page tree")
    try:
        raw_count = pages.get("/Count")
    except Exception as exc:
        raise ValueError("declared PDF page count is invalid") from exc
    count = _resolved_pdf_object(
        raw_count,
        field_name="declared PDF page count",
    )
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("declared PDF page count is invalid")
    return count


def _materialized_page_count(reader: Any) -> int:
    # Kept separate so tests can prove the declared-count preflight runs first.
    # This call is not resource-isolated and may traverse a hostile page tree.
    return len(reader.pages)


def _controlled_pdf_reader() -> type[Any]:
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover - required dependency
        raise RepositoryError(
            "PDF inspection support is unavailable",
            code="canvas_pdf_inspector_unavailable",
        ) from exc
    version = getattr(pypdf, "__version__", "")
    match = re.fullmatch(r"([0-9]+)(?:\..*)?", version)
    if match is None or int(match.group(1)) != _CONTROLLED_PYPDF_MAJOR:
        raise RepositoryError(
            "the installed PDF inspector version is unsupported",
            code="canvas_pdf_inspector_version_unsupported",
            details={"required_major": _CONTROLLED_PYPDF_MAJOR},
        )
    return pypdf.PdfReader


def _page_evidence_digest(
    *,
    asset_sha256: str,
    position: int,
    width_mpt: int,
    height_mpt: int,
    rotation: int,
) -> str:
    # This is snapshot-specific evidence, not a page-content reconciliation
    # fingerprint.  The asset digest binds it to exact immutable bytes, while
    # position and normalized geometry distinguish observations only within
    # that snapshot.  None of these values are durable canvas identity.
    value = "\0".join(
        (
            ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE,
            asset_sha256,
            str(position),
            str(width_mpt),
            str(height_mpt),
            str(rotation),
        )
    ).encode("ascii")
    return hashlib.sha256(value).hexdigest()


class FilesystemAttachedPdfInspector:
    """Produce ordered canvas observations from a tracked attached PDF.

    This adapter runs pypdf in-process.  Its byte and page limits reject common
    oversized inputs but do not bound parser construction or page-tree
    traversal for hostile files.  Use it only for locally trusted attachments
    until composition gains a killable, resource-limited parser worker.
    """

    def __init__(
        self,
        asset_snapshot_for: AttachedPdfAssetLookup,
        *,
        max_asset_bytes: int = _DEFAULT_MAX_ASSET_BYTES,
        max_pages: int = _DEFAULT_MAX_PAGES,
        spool_memory_bytes: int = _DEFAULT_SPOOL_MEMORY_BYTES,
    ) -> None:
        if not callable(asset_snapshot_for):
            raise TypeError("asset_snapshot_for must be callable")
        for value, name in (
            (max_asset_bytes, "max_asset_bytes"),
            (max_pages, "max_pages"),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if max_pages > _DEFAULT_MAX_PAGES:
            raise ValueError(
                f"max_pages cannot exceed {_DEFAULT_MAX_PAGES}"
            )
        if type(spool_memory_bytes) is not int or spool_memory_bytes < 1:
            raise ValueError("spool_memory_bytes must be a positive integer")
        self._asset_snapshot_for = asset_snapshot_for
        self._max_asset_bytes = max_asset_bytes
        self._max_pages = max_pages
        self._spool_memory_bytes = spool_memory_bytes

    def __call__(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
        entry_directory: Path,
    ) -> FilesystemCanvasInspection:
        if not isinstance(
            representation, CanvasPreparationRepresentationSnapshot
        ):
            raise RepositoryError(
                "the PDF inspector received an invalid representation snapshot",
                code="invalid_canvas_pdf_representation_snapshot",
            )
        if not isinstance(entry_directory, Path) or not entry_directory.is_absolute():
            raise RepositoryError(
                "the PDF inspector received an invalid entry directory",
                code="unsafe_canvas_pdf_entry_directory",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                },
            )
        asset = self._asset_for(representation)
        if asset.size > self._max_asset_bytes:
            raise ValidationError(
                "the attached PDF exceeds the inspection limit",
                code="canvas_pdf_asset_too_large",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "maximum_bytes": self._max_asset_bytes,
                },
            )
        return self._snapshot_and_inspect(representation, asset)

    def _asset_for(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> FilesystemAttachedPdfAssetSnapshot:
        try:
            asset = self._asset_snapshot_for(
                representation.item_id,
                representation.representation_id,
                representation.revision,
            )
        except Exception as exc:
            raise RepositoryError(
                "the attached PDF authority is unavailable",
                code="canvas_pdf_asset_authority_unavailable",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc
        if asset is None:
            raise RepositoryError(
                "the representation has no tracked local PDF asset",
                code="canvas_pdf_asset_unavailable",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                },
            )
        if not isinstance(asset, FilesystemAttachedPdfAssetSnapshot):
            raise RepositoryError(
                "the attached PDF authority returned invalid state",
                code="invalid_canvas_pdf_asset_snapshot",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                },
            )
        try:
            asset = FilesystemAttachedPdfAssetSnapshot(
                item_id=asset.item_id,
                representation_id=asset.representation_id,
                representation_revision=asset.representation_revision,
                path=asset.path,
                content_sha256=asset.content_sha256,
                size=asset.size,
                media_type=asset.media_type,
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "the attached PDF authority returned invalid state",
                code="invalid_canvas_pdf_asset_snapshot",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                },
            ) from exc
        if (
            asset.item_id != representation.item_id
            or asset.representation_id != representation.representation_id
            or asset.representation_revision != representation.revision
        ):
            raise RepositoryError(
                "the attached PDF authority returned state for another revision",
                code="canvas_pdf_asset_scope_mismatch",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "representation_revision": representation.revision,
                },
            )
        return asset

    def _snapshot_and_inspect(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
        asset: FilesystemAttachedPdfAssetSnapshot,
    ) -> FilesystemCanvasInspection:
        try:
            path_before = _path_signature(asset.path)
            if not stat.S_ISREG(path_before.mode):
                raise OSError("attachment is not a regular file")
            with asset.path.open("rb") as source:
                opened_before = _stream_signature(source)
                if not stat.S_ISREG(opened_before.mode):
                    raise OSError("attachment is not a regular file")
                # Do not compare a path stat and a descriptor stat field for
                # field.  On Windows their ctime values can legitimately
                # disagree for the same file.  Same-interface snapshots below
                # still protect metadata/content stability during the copy.
                if not os.path.samestat(path_before, opened_before):
                    raise ConflictError(
                        "the attached PDF changed while inspection began",
                        code="canvas_pdf_asset_changed",
                        details={
                            "item_id": representation.item_id,
                            "representation_id": representation.representation_id,
                        },
                        retryable=True,
                    )
                if opened_before.size != asset.size:
                    raise ConflictError(
                        "the attached PDF size no longer matches its revision",
                        code="canvas_pdf_asset_size_mismatch",
                        details={
                            "item_id": representation.item_id,
                            "representation_id": representation.representation_id,
                        },
                    )
                with tempfile.SpooledTemporaryFile(
                    max_size=self._spool_memory_bytes,
                    mode="w+b",
                ) as snapshot:
                    digest, copied_size = _copy_source(
                        source,
                        snapshot,
                        maximum_bytes=self._max_asset_bytes,
                    )
                    opened_after = _stream_signature(source)
                    if opened_before != opened_after:
                        raise ConflictError(
                            "the attached PDF changed during inspection",
                            code="canvas_pdf_asset_changed",
                            details={
                                "item_id": representation.item_id,
                                "representation_id": (
                                    representation.representation_id
                                ),
                            },
                            retryable=True,
                        )
                    path_after = _path_signature(asset.path)
                    if path_before != path_after:
                        raise ConflictError(
                            "the attached PDF path changed during inspection",
                            code="canvas_pdf_asset_changed",
                            details={
                                "item_id": representation.item_id,
                                "representation_id": (
                                    representation.representation_id
                                ),
                            },
                            retryable=True,
                        )
                    if copied_size != asset.size:
                        raise ConflictError(
                            "the attached PDF size no longer matches its revision",
                            code="canvas_pdf_asset_size_mismatch",
                            details={
                                "item_id": representation.item_id,
                                "representation_id": (
                                    representation.representation_id
                                ),
                            },
                        )
                    if digest != asset.content_sha256:
                        raise ConflictError(
                            "the attached PDF digest no longer matches its revision",
                            code="canvas_pdf_asset_digest_mismatch",
                            details={
                                "item_id": representation.item_id,
                                "representation_id": (
                                    representation.representation_id
                                ),
                            },
                        )
                    snapshot.seek(0)
                    return self._inspect_snapshot(
                        representation,
                        snapshot,
                        asset_sha256=digest,
                        asset_size=copied_size,
                    )
        except EngineError:
            raise
        except (OSError, OverflowError) as exc:
            raise RepositoryError(
                "the attached PDF could not be snapshotted",
                code="canvas_pdf_asset_unavailable",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "cause_type": type(exc).__name__,
                },
                retryable=True,
            ) from exc

    def _inspect_snapshot(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
        snapshot: BinaryIO,
        *,
        asset_sha256: str,
        asset_size: int,
    ) -> FilesystemCanvasInspection:
        try:
            if snapshot.read(5) != b"%PDF-":
                raise ValueError("PDF header is missing")
            snapshot.seek(0)
            PdfReader = _controlled_pdf_reader()
            reader = PdfReader(snapshot, strict=False)
            if reader.is_encrypted:
                raise ValueError("encrypted PDFs are unsupported")
            declared_page_count = _declared_page_count(reader)
            if declared_page_count > self._max_pages:
                raise ValidationError(
                    "the attached PDF has too many pages",
                    code="canvas_pdf_page_limit_exceeded",
                    details={
                        "item_id": representation.item_id,
                        "representation_id": representation.representation_id,
                        "maximum_pages": self._max_pages,
                    },
                )
            page_count = _materialized_page_count(reader)
            if page_count < 1:
                raise ValueError("the PDF has no pages")
            if page_count > self._max_pages:
                raise ValidationError(
                    "the attached PDF has too many pages",
                    code="canvas_pdf_page_limit_exceeded",
                    details={
                        "item_id": representation.item_id,
                        "representation_id": representation.representation_id,
                        "maximum_pages": self._max_pages,
                    },
                )
            observations = tuple(
                self._observation(
                    reader.pages[position],
                    position=position,
                    asset_sha256=asset_sha256,
                )
                for position in range(page_count)
            )
        except EngineError:
            raise
        except (KeyError, RecursionError, TypeError, ValueError) as exc:
            raise ValidationError(
                "the attached PDF is malformed or unsupported",
                code="invalid_canvas_pdf_asset",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        except Exception as exc:
            # pypdf exposes several version-specific parse exceptions.  Keep
            # their types and messages out of the engine boundary.
            raise ValidationError(
                "the attached PDF is malformed or unsupported",
                code="invalid_canvas_pdf_asset",
                details={
                    "item_id": representation.item_id,
                    "representation_id": representation.representation_id,
                    "cause_type": type(exc).__name__,
                },
            ) from exc
        return FilesystemCanvasInspection(
            media_type="application/pdf",
            asset_sha256=asset_sha256,
            asset_size=asset_size,
            observations=observations,
        )

    @staticmethod
    def _observation(
        page: Any,
        *,
        position: int,
        asset_sha256: str,
    ) -> FilesystemCanvasObservation:
        rotation = _page_rotation(page)
        user_unit = _page_user_unit(page)
        width_mpt = _millipoints(
            page.cropbox.width,
            user_unit=user_unit,
            field_name="page width",
        )
        height_mpt = _millipoints(
            page.cropbox.height,
            user_unit=user_unit,
            field_name="page height",
        )
        display_width = width_mpt
        display_height = height_mpt
        if rotation in {90, 270}:
            display_width, display_height = display_height, display_width
        return FilesystemCanvasObservation(
            source_position=position,
            # Resource delivery is a separate engine slice.  In particular,
            # never persist an absolute attachment path in the canvas index.
            source_path="",
            evidence=FilesystemCanvasEvidence(
                profile=ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE,
                width_mpt=width_mpt,
                height_mpt=height_mpt,
                rotation=rotation,
                strong_sha256=_page_evidence_digest(
                    asset_sha256=asset_sha256,
                    position=position,
                    width_mpt=width_mpt,
                    height_mpt=height_mpt,
                    rotation=rotation,
                ),
            ),
            label=f"Page {position + 1}",
            extent=CanvasExtent(display_width, display_height, "mpt"),
            available=True,
        )


__all__ = [
    "ATTACHED_PDF_PARSER_ISOLATION",
    "ATTACHED_PDF_SNAPSHOT_EVIDENCE_PROFILE",
    "AttachedPdfAssetLookup",
    "FilesystemAttachedPdfAssetSnapshot",
    "FilesystemAttachedPdfInspector",
]
