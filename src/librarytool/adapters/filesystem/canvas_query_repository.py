"""Strict read-only canvas indexes stored beneath managed item trees.

The adapter consumes one private, versioned index per item at
``.librarytool/canvases.json``.  Source positions remain adapter data: only
the public canvas fields are projected through the mapping-shaped
``CanvasQueryRepositoryPort``.  Reads never create, repair, or infer an index
and canvas identities are always taken verbatim from persisted data.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.canvases import CanvasExtent, CanvasKey, CanvasView
from ...engine.errors import EngineError, NotFoundError, RepositoryError, ValidationError
from .recoverable_write_set import RecoverableWriteSet, _is_redirecting_path


ItemExists: TypeAlias = Callable[[str], bool]
RepresentationRevisionLookup: TypeAlias = Callable[[str, str], str | None]
EntryDirectoryResolver: TypeAlias = Callable[[str], Path]
LockContextFactory: TypeAlias = Callable[[], ContextManager[Any]]

CANVAS_INDEX_SCHEMA = "librarytool.canvas-index"
CANVAS_INDEX_VERSION = 1
CANVAS_INDEX_RELATIVE = PurePosixPath(".librarytool/canvases.json")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_INDEX_BYTES = 16 * 1024 * 1024
_TOP_LEVEL_FIELDS = frozenset({"schema", "version", "item_id", "sequences"})
_SEQUENCE_FIELDS = frozenset(
    {"representation_id", "representation_revision", "canvases"}
)
_CANVAS_FIELDS = frozenset(
    {
        "canvas_id",
        "revision",
        "order",
        "label",
        "extent",
        "available",
        "resource_kinds",
        "metadata",
        "source",
    }
)
_SOURCE_FIELDS = frozenset({"position", "path"})
_EXTENT_FIELDS = frozenset({"width", "height", "unit", "duration"})
_RESERVED_SOURCE_PARTS = frozenset({".engine", ".librarytool", ".transactions"})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _repository_error(
    message: str,
    *,
    code: str,
    item_id: str,
    representation_id: str = "",
    **details: Any,
) -> RepositoryError:
    scope: dict[str, Any] = {"item_id": item_id}
    if representation_id:
        scope["representation_id"] = representation_id
    scope.update(details)
    return RepositoryError(message, code=code, details=scope)


def _identifier(value: Any, *, field: str, item_id: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _repository_error(
            "the canvas index contains an invalid identity",
            code="invalid_canvas_index",
            item_id=item_id,
            field=field,
        )
    return value


def _revision(
    value: Any,
    *,
    field: str,
    item_id: str,
    code: str = "invalid_canvas_index",
) -> str:
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
        raise _repository_error(
            "a canvas revision is invalid",
            code=code,
            item_id=item_id,
            field=field,
        )
    return value


def _object_fields(
    value: Any,
    expected: frozenset[str],
    *,
    section: str,
    item_id: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise _repository_error(
            "the canvas index has an invalid object shape",
            code="invalid_canvas_index",
            item_id=item_id,
            section=section,
        )
    return value


class FilesystemCanvasQueryRepository:
    """Project a private item-local canvas index through the query port.

    ``item_exists`` and ``representation_revision_for`` must query the live,
    authoritative catalogue while ``lock_context_for`` is held.  The latter
    must perform an exact (case-sensitive) identity lookup.  Lock order is the
    recoverable workspace lease followed by the injected broad host lock.

    The index schema is intentionally closed.  Shape changes require a new
    integer version instead of making existing readers guess at semantics.
    Every stored sequence is structurally validated, but live availability
    and revision binding are checked for the requested representation only;
    a stale optional representation must not hide an otherwise usable one.
    A producer owns each persisted canvas ``revision`` and must advance it
    whenever the backing content or private source position/path changes.  The
    query service incorporates that opaque token into its public revision hash.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_exists: ItemExists,
        representation_revision_for: RepresentationRevisionLookup,
        entry_directory_for: EntryDirectoryResolver,
        lock_context_for: LockContextFactory,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (item_exists, "item_exists"),
            (representation_revision_for, "representation_revision_for"),
            (entry_directory_for, "entry_directory_for"),
            (lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._write_set = write_set
        self._item_exists = item_exists
        self._representation_revision_for = representation_revision_for
        self._entry_directory_for = entry_directory_for
        self._lock_context_for = lock_context_for

    def get_sequence_record(
        self,
        item_id: str,
        representation_id: str,
    ) -> Mapping[str, Any] | None:
        """Return one detached public sequence record, or ``None`` if absent."""

        item = _identifier(item_id, field="item_id", item_id=str(item_id or ""))
        representation = _identifier(
            representation_id,
            field="representation_id",
            item_id=item,
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
                    requested_revision = self._live_representation_revision(
                        item,
                        representation,
                    )
                    if requested_revision is None:
                        raise NotFoundError(
                            "the representation does not exist",
                            code="representation_not_found",
                            details={
                                "item_id": item,
                                "representation_id": representation,
                            },
                        )
                    entry_directory, index_path = self._index_path(item)
                    if not self._index_exists(index_path, item_id=item):
                        return None
                    raw = self._read_index(index_path, item_id=item)
                    return self._sequence_record(
                        raw,
                        item_id=item,
                        representation_id=representation,
                        requested_revision=requested_revision,
                        entry_directory=entry_directory,
                    )
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the canvas repository is unavailable",
                code="canvas_repository_unavailable",
                item_id=item,
                representation_id=representation,
                cause_type=type(exc).__name__,
            ) from exc

    def _live_item_exists(self, item_id: str) -> bool:
        try:
            exists = self._item_exists(item_id)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the live item catalogue could not be queried",
                code="canvas_repository_unavailable",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if not isinstance(exists, bool):
            raise _repository_error(
                "the live item catalogue returned invalid state",
                code="invalid_canvas_authority_snapshot",
                item_id=item_id,
                field="item_exists",
            )
        return exists

    def _live_representation_revision(
        self,
        item_id: str,
        representation_id: str,
    ) -> str | None:
        try:
            value = self._representation_revision_for(item_id, representation_id)
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the live representation catalogue could not be queried",
                code="canvas_repository_unavailable",
                item_id=item_id,
                representation_id=representation_id,
                cause_type=type(exc).__name__,
            ) from exc
        if value is None:
            return None
        return _revision(
            value,
            field="authoritative_representation_revision",
            item_id=item_id,
            code="invalid_canvas_authority_snapshot",
        )

    def _index_path(self, item_id: str) -> tuple[Path, Path]:
        try:
            configured = Path(self._entry_directory_for(item_id))
        except EngineError:
            raise
        except Exception as exc:
            raise _repository_error(
                "the item entry directory is invalid",
                code="unsafe_canvas_index_path",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            not configured.parts
            or any(part in {"", ".", ".."} for part in configured.parts)
        ):
            raise _repository_error(
                "the item entry directory is invalid",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            )
        candidate = (
            configured
            if configured.is_absolute()
            else self._write_set.root / configured
        )
        lexical = Path(os.path.abspath(candidate))
        try:
            relative = lexical.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                "the item entry directory escapes the workspace",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            ) from exc
        if (
            not relative.parts
            or relative.parts[0].casefold()
            in {".engine", ".librarytool", ".transactions"}
        ):
            raise _repository_error(
                "the item entry directory uses a reserved workspace path",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            )
        index_path = lexical.joinpath(*CANVAS_INDEX_RELATIVE.parts)
        self._assert_safe_components(
            index_path,
            item_id=item_id,
            message="the canvas index path is unsafe",
        )
        if lexical.exists() and not lexical.is_dir():
            raise _repository_error(
                "the item entry path is not a directory",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            )
        index_parent = index_path.parent
        if index_parent.exists() and not index_parent.is_dir():
            raise _repository_error(
                "the canvas index parent is not a directory",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            )
        return lexical, index_path

    def _assert_safe_components(
        self,
        path: Path,
        *,
        item_id: str,
        message: str,
        code: str = "unsafe_canvas_index_path",
    ) -> None:
        try:
            relative = path.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                message,
                code=code,
                item_id=item_id,
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    message,
                    code=code,
                    item_id=item_id,
                )
        try:
            path.resolve(strict=False).relative_to(self._write_set.root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                message,
                code=code,
                item_id=item_id,
            ) from exc

    def _index_exists(self, path: Path, *, item_id: str) -> bool:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _repository_error(
                "the canvas index cannot be inspected",
                code="canvas_repository_unavailable",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting_path(path) or not stat.S_ISREG(info.st_mode):
            raise _repository_error(
                "the canvas index is not a private regular file",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            )
        return True

    def _read_index(self, path: Path, *, item_id: str) -> Any:
        if _is_redirecting_path(path):
            raise _repository_error(
                "the canvas index is not a private regular file",
                code="unsafe_canvas_index_path",
                item_id=item_id,
            )
        try:
            with path.open("rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise _repository_error(
                        "the canvas index is not a private regular file",
                        code="unsafe_canvas_index_path",
                        item_id=item_id,
                    )
                encoded = stream.read(_MAX_INDEX_BYTES + 1)
            if len(encoded) > _MAX_INDEX_BYTES:
                raise _repository_error(
                    "the canvas index exceeds its size limit",
                    code="invalid_canvas_index",
                    item_id=item_id,
                    maximum_bytes=_MAX_INDEX_BYTES,
                )
            payload = encoded.decode("utf-8")
            return json.loads(
                payload,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except EngineError:
            raise
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise _repository_error(
                "the canvas index cannot be decoded",
                code="invalid_canvas_index",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc

    def _sequence_record(
        self,
        raw: Any,
        *,
        item_id: str,
        representation_id: str,
        requested_revision: str,
        entry_directory: Path,
    ) -> Mapping[str, Any] | None:
        index = _object_fields(
            raw,
            _TOP_LEVEL_FIELDS,
            section="index",
            item_id=item_id,
        )
        version = index["version"]
        if (
            index["schema"] != CANVAS_INDEX_SCHEMA
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version != CANVAS_INDEX_VERSION
        ):
            raise _repository_error(
                "the canvas index version is unsupported",
                code="unsupported_canvas_index_version",
                item_id=item_id,
            )
        if index["item_id"] != item_id:
            raise _repository_error(
                "the canvas index belongs to another item",
                code="canvas_index_scope_mismatch",
                item_id=item_id,
                actual_item_id=(
                    index["item_id"] if isinstance(index["item_id"], str) else ""
                ),
            )
        sequences = index["sequences"]
        if isinstance(sequences, (str, bytes)) or not isinstance(sequences, Sequence):
            raise _repository_error(
                "the canvas index sequences are invalid",
                code="invalid_canvas_index",
                item_id=item_id,
                section="sequences",
            )

        aliases: dict[str, str] = {}
        result: Mapping[str, Any] | None = None
        for index_position, raw_sequence in enumerate(sequences):
            sequence = _object_fields(
                raw_sequence,
                _SEQUENCE_FIELDS,
                section=f"sequences[{index_position}]",
                item_id=item_id,
            )
            sequence_id = _identifier(
                sequence["representation_id"],
                field="representation_id",
                item_id=item_id,
            )
            folded = sequence_id.casefold()
            if folded in aliases:
                raise _repository_error(
                    "the canvas index contains aliased representation identities",
                    code="duplicate_canvas_representation_identity",
                    item_id=item_id,
                    representation_ids=sorted(
                        (aliases[folded], sequence_id),
                        key=lambda value: (value.casefold(), value),
                    ),
                )
            aliases[folded] = sequence_id
            if (
                sequence_id != representation_id
                and folded == representation_id.casefold()
            ):
                raise _repository_error(
                    "the canvas index aliases the requested representation",
                    code="canvas_index_representation_alias",
                    item_id=item_id,
                    representation_id=representation_id,
                    stored_representation_id=sequence_id,
                )
            stored_revision = _revision(
                sequence["representation_revision"],
                field="representation_revision",
                item_id=item_id,
            )
            if (
                sequence_id == representation_id
                and stored_revision != requested_revision
            ):
                raise _repository_error(
                    "the canvas index is bound to an obsolete representation",
                    code="canvas_representation_revision_drift",
                    item_id=item_id,
                    representation_id=sequence_id,
                    indexed_revision=stored_revision,
                    authoritative_revision=requested_revision,
                )
            public_canvases = self._public_canvases(
                sequence["canvases"],
                item_id=item_id,
                representation_id=sequence_id,
                entry_directory=entry_directory,
            )
            public_sequence: Mapping[str, Any] = {
                "item_id": item_id,
                "representation_id": sequence_id,
                "representation_revision": stored_revision,
                "canvases": public_canvases,
            }
            if sequence_id == representation_id:
                result = public_sequence
        return result

    def _public_canvases(
        self,
        raw: Any,
        *,
        item_id: str,
        representation_id: str,
        entry_directory: Path,
    ) -> list[dict[str, Any]]:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise _repository_error(
                "a canvas sequence is invalid",
                code="invalid_canvas_index",
                item_id=item_id,
                representation_id=representation_id,
                section="canvases",
            )
        canvases: list[tuple[CanvasView, str]] = []
        aliases: dict[str, str] = {}
        orders: set[int] = set()
        for position, raw_canvas in enumerate(raw):
            canvas = _object_fields(
                raw_canvas,
                _CANVAS_FIELDS,
                section=f"canvases[{position}]",
                item_id=item_id,
            )
            canvas_id = _identifier(
                canvas["canvas_id"],
                field="canvas_id",
                item_id=item_id,
            )
            folded = canvas_id.casefold()
            if folded in aliases:
                raise _repository_error(
                    "a canvas sequence contains aliased identities",
                    code="duplicate_canvas_identity",
                    item_id=item_id,
                    representation_id=representation_id,
                    canvas_ids=sorted(
                        (aliases[folded], canvas_id),
                        key=lambda value: (value.casefold(), value),
                    ),
                )
            aliases[folded] = canvas_id
            order = canvas["order"]
            if isinstance(order, bool) or not isinstance(order, int) or order < 0:
                raise _repository_error(
                    "a canvas order is invalid",
                    code="invalid_canvas_index",
                    item_id=item_id,
                    representation_id=representation_id,
                    field="order",
                )
            if order in orders:
                raise _repository_error(
                    "a canvas sequence contains duplicate order values",
                    code="duplicate_canvas_order",
                    item_id=item_id,
                    representation_id=representation_id,
                    orders=[order],
                )
            orders.add(order)
            source_revision = _revision(
                canvas["revision"],
                field="canvas.revision",
                item_id=item_id,
            )
            source = _object_fields(
                canvas["source"],
                _SOURCE_FIELDS,
                section=f"canvases[{position}].source",
                item_id=item_id,
            )
            source_position = source["position"]
            if (
                isinstance(source_position, bool)
                or not isinstance(source_position, int)
                or source_position < 0
            ):
                raise _repository_error(
                    "a canvas source position is invalid",
                    code="invalid_canvas_index",
                    item_id=item_id,
                    representation_id=representation_id,
                    field="source.position",
                )
            self._validate_source_path(
                source["path"],
                entry_directory=entry_directory,
                item_id=item_id,
                representation_id=representation_id,
            )
            extent = canvas["extent"]
            if not isinstance(extent, dict) or not set(extent).issubset(
                _EXTENT_FIELDS
            ):
                raise _repository_error(
                    "a canvas extent is invalid",
                    code="invalid_canvas_index",
                    item_id=item_id,
                    representation_id=representation_id,
                    field="extent",
                )
            try:
                view = CanvasView(
                    key=CanvasKey(item_id, representation_id, canvas_id),
                    revision=source_revision,
                    order=order,
                    label=canvas["label"],
                    extent=CanvasExtent(
                        width=extent.get("width"),
                        height=extent.get("height"),
                        unit=extent.get("unit", ""),
                        duration=extent.get("duration"),
                    ),
                    available=canvas["available"],
                    resource_kinds=canvas["resource_kinds"],
                    metadata=canvas["metadata"],
                )
            except ValidationError as exc:
                raise _repository_error(
                    "a canvas record is invalid",
                    code="invalid_canvas_index",
                    item_id=item_id,
                    representation_id=representation_id,
                    field=str(exc.details.get("field") or "canvas"),
                ) from exc
            canvases.append((view, source_revision))

        public: list[dict[str, Any]] = []
        for view, source_revision in sorted(canvases, key=lambda pair: pair[0].order):
            serialized = view.as_dict()
            public.append(
                {
                    "canvas_id": view.key.canvas_id,
                    "revision": source_revision,
                    "order": view.order,
                    "label": view.label,
                    "extent": serialized["extent"],
                    "available": view.available,
                    "resource_kinds": serialized["resource_kinds"],
                    "metadata": serialized["metadata"],
                }
            )
        return public

    def _validate_source_path(
        self,
        value: Any,
        *,
        entry_directory: Path,
        item_id: str,
        representation_id: str,
    ) -> None:
        if (
            not isinstance(value, str)
            or len(value) > 4096
            or any(
                ord(character) == 127
                or ord(character) < 32
                or 0xD800 <= ord(character) <= 0xDFFF
                for character in value
            )
        ):
            raise _repository_error(
                "a canvas source path is invalid",
                code="unsafe_canvas_source_path",
                item_id=item_id,
                representation_id=representation_id,
            )
        if not value:
            return
        pure = PurePosixPath(value)
        if (
            pure.is_absolute()
            or not pure.parts
            or pure.as_posix() != value
            or any(
                part in {"", ".", ".."}
                or "\\" in part
                or ":" in part
                or part.casefold() in _RESERVED_SOURCE_PARTS
                for part in pure.parts
            )
        ):
            raise _repository_error(
                "a canvas source path is unsafe",
                code="unsafe_canvas_source_path",
                item_id=item_id,
                representation_id=representation_id,
            )
        target = entry_directory.joinpath(*pure.parts)
        self._assert_safe_components(
            target,
            item_id=item_id,
            message="a canvas source path is unsafe",
            code="unsafe_canvas_source_path",
        )


__all__ = [
    "CANVAS_INDEX_RELATIVE",
    "CANVAS_INDEX_SCHEMA",
    "CANVAS_INDEX_VERSION",
    "FilesystemCanvasQueryRepository",
]
