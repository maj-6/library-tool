"""Recoverable native storage for revisioned text-layer aggregates.

Text-layer documents live below each managed item's private ``.librarytool``
namespace.  Durable replay envelopes live below the workspace-private
``.engine`` namespace instead: a retry must remain answerable after the live
item and its entry directory have been deleted or moved.

The adapter never projects legacy ``ocr/*.txt`` files.  Selectors are opaque
engine values and are persisted exactly as supplied by the aggregate service.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, ContextManager, TypeAlias

from ...engine.errors import RepositoryError
from ...engine.text_layer_aggregate import (
    MAX_TEXT_LAYERS_PER_ITEM,
    MAX_TEXT_LAYER_UNITS,
    TextLayerDocumentSnapshot,
    TextLayerDraft,
    TextLayerSourcePin,
    TextLayerSourceSnapshot,
    TextLayerStoredMutationReceipt,
    TextLayerUnitDraft,
    TextLayerUnitMutationReceipt,
    TextLayerUnitSnapshot,
)
from .recoverable_write_set import (
    RecoverableWriteSet,
    WriteSetError,
    _is_redirecting_path,
)


ItemMembershipLookup: TypeAlias = Callable[[str], bool]
EntryDirectoryResolver: TypeAlias = Callable[[str], Path]
TextLayerSourceLookup: TypeAlias = Callable[
    [str, str], TextLayerSourceSnapshot | None
]
TextLayerLockFactory: TypeAlias = Callable[[], ContextManager[Any]]
TextLayerIdFactory: TypeAlias = Callable[[], str]

TEXT_LAYER_DOCUMENT_SCHEMA = "librarytool.text-layer-document"
TEXT_LAYER_DOCUMENT_VERSION = 1
TEXT_LAYER_REPLAY_SCHEMA = "librarytool.text-layer-replay-envelope"
TEXT_LAYER_REPLAY_VERSION = 1

TEXT_LAYER_DOCUMENT_ROOT = PurePosixPath(
    ".librarytool/text-layer-aggregates-v1/documents"
)
TEXT_LAYER_REPLAY_ROOT = PurePosixPath(
    ".engine/receipts/text-layer-aggregates-v1"
)

# The engine caps text at 256 MiB and aggregate provenance at 16 MiB.  Keep a
# finite allowance for JSON structure, labels, revisions, and unit records.
MAX_TEXT_LAYER_DOCUMENT_BYTES = 384 * 1024 * 1024
MAX_TEXT_LAYER_REPLAY_BYTES = 8 * 1024 * 1024
MAX_TEXT_LAYER_ID_ALLOCATION_ATTEMPTS = 128

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FILE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_RESERVED_WORKSPACE_ROOTS = frozenset(
    {".engine", ".librarytool", ".transactions"}
)


def _repository_error(message: str, *, code: str, **details: Any) -> RepositoryError:
    safe = {key: value for key, value in details.items() if value not in {"", None}}
    return RepositoryError(message, code=code, details=safe)


def _identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise _repository_error(
            f"{field} is invalid",
            code="invalid_text_layer_storage_identity",
            field=field,
        )
    return value


def _file_identifier(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not _FILE_IDENTIFIER_RE.fullmatch(value)
        or value.endswith(".")
        or value.split(".", 1)[0].casefold() in _WINDOWS_DEVICE_NAMES
    ):
        raise _repository_error(
            f"{field} is not a portable file identity",
            code="invalid_text_layer_storage_identity",
            field=field,
        )
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _repository_error(
            "text-layer storage cannot be serialized",
            code="invalid_text_layer_storage",
            cause_type=type(exc).__name__,
        ) from exc


def _bounded_json_bytes(value: Any, *, maximum: int, artifact: str) -> bytes:
    payload = _canonical_json(value)
    if not payload or len(payload) > maximum:
        raise _repository_error(
            "a text-layer artifact exceeds its storage limit",
            code="text_layer_storage_limit_exceeded",
            artifact=artifact,
        )
    return payload


def _stable_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000))),
        int(getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))),
    )


def _changed_units(
    before: TextLayerDocumentSnapshot,
    after: TextLayerDocumentSnapshot,
) -> tuple[TextLayerUnitMutationReceipt, ...]:
    before_by_selector = {value.selector: value for value in before.units}
    after_by_selector = {value.selector: value for value in after.units}
    if set(before_by_selector) != set(after_by_selector):
        raise _repository_error(
            "a staged replacement changed the text-layer unit identities",
            code="text_layer_repository_content_mismatch",
        )
    changed: list[TextLayerUnitMutationReceipt] = []
    for selector in sorted(before_by_selector):
        old = before_by_selector[selector]
        new = after_by_selector[selector]
        if old.unit_revision == new.unit_revision:
            continue
        changed.append(
            TextLayerUnitMutationReceipt(
                selector=selector,
                before_unit_revision=old.unit_revision,
                after_unit_revision=new.unit_revision,
                before_content_revision=old.content_revision,
                after_content_revision=new.content_revision,
            )
        )
    return tuple(changed)


class FilesystemTextLayerAggregateRepository:
    """Open snapshot-consistent reads and recoverable mutation units.

    Lock order is always the recoverable write set's workspace lease followed
    by the injected host mutation lock.  The operation-scoped unit does not
    consult any live callback before its first durable receipt lookup.

    ``recover=True`` is the safe standalone default, matching the other
    filesystem adapters.  A composed host which performs one workspace-wide
    recovery while holding its broad legacy lock must pass ``recover=False``
    to every adapter so recovery is not repeated under narrower lock scopes.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_exists_for: ItemMembershipLookup,
        entry_directory_for: EntryDirectoryResolver,
        source_snapshot_for: TextLayerSourceLookup,
        lock_context_for: TextLayerLockFactory,
        layer_id_factory: TextLayerIdFactory | None = None,
        recover: bool = True,
    ) -> None:
        if not isinstance(write_set, RecoverableWriteSet):
            raise TypeError("write_set must be a RecoverableWriteSet")
        for callback, name in (
            (item_exists_for, "item_exists_for"),
            (entry_directory_for, "entry_directory_for"),
            (source_snapshot_for, "source_snapshot_for"),
            (lock_context_for, "lock_context_for"),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        if layer_id_factory is not None and not callable(layer_id_factory):
            raise TypeError("layer_id_factory must be callable")
        self._write_set = write_set
        self._item_exists_for = item_exists_for
        self._entry_directory_for = entry_directory_for
        self._source_snapshot_for = source_snapshot_for
        self._lock_context_for = lock_context_for
        self._layer_id_factory = layer_id_factory or (
            lambda: "tl-" + secrets.token_hex(16)
        )
        if recover:
            try:
                with self._write_set.recovery_lease():
                    with self._lock_context_for():
                        self._write_set.recover_all()
            except Exception as exc:
                raise _repository_error(
                    "the text-layer repository could not recover",
                    code="text_layer_recovery_failed",
                    cause_type=type(exc).__name__,
                ) from exc

    @contextmanager
    def snapshot(
        self, item_id: str
    ) -> Iterator["FilesystemTextLayerAggregateSession"]:
        identifier = _identifier(item_id, field="item_id")
        with self._write_set.workspace_lease():
            with self._lock_context_for():
                session = FilesystemTextLayerAggregateSession(
                    self._write_set,
                    item_exists_for=self._item_exists_for,
                    entry_directory_for=self._entry_directory_for,
                    source_snapshot_for=self._source_snapshot_for,
                    fixed_item_id=identifier,
                )
                try:
                    yield session
                finally:
                    session.close()

    @contextmanager
    def unit_of_work(
        self, *, operation_id: str
    ) -> Iterator["FilesystemTextLayerAggregateUnitOfWork"]:
        operation = _identifier(operation_id, field="operation_id")
        with self._write_set.workspace_lease():
            with self._lock_context_for():
                unit = FilesystemTextLayerAggregateUnitOfWork(
                    self._write_set,
                    operation_id=operation,
                    item_exists_for=self._item_exists_for,
                    entry_directory_for=self._entry_directory_for,
                    source_snapshot_for=self._source_snapshot_for,
                    layer_id_factory=self._layer_id_factory,
                )
                try:
                    yield unit
                finally:
                    unit.close()


class FilesystemTextLayerAggregateSession:
    """One lease-bound, coherent view of documents and source revisions."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_exists_for: ItemMembershipLookup,
        entry_directory_for: EntryDirectoryResolver,
        source_snapshot_for: TextLayerSourceLookup,
        fixed_item_id: str | None = None,
    ) -> None:
        self._write_set = write_set
        self._item_exists_for = item_exists_for
        self._entry_directory_for = entry_directory_for
        self._source_snapshot_for = source_snapshot_for
        self._item_id = fixed_item_id
        self._item_exists: bool | None = None
        self._entry_directory: Path | None = None
        self._documents: dict[str, TextLayerDocumentSnapshot] | None = None
        self._sources: dict[str, TextLayerSourceSnapshot | None] = {}
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._documents = None
        self._sources.clear()

    def item_exists(self, item_id: str) -> bool:
        self._ensure_open()
        identifier = self._bind_item(item_id)
        if self._item_exists is None:
            try:
                exists = self._item_exists_for(identifier)
            except Exception as exc:
                raise _repository_error(
                    "the authoritative item membership could not be read",
                    code="text_layer_authority_unavailable",
                    item_id=identifier,
                    cause_type=type(exc).__name__,
                ) from exc
            if not isinstance(exists, bool):
                raise _repository_error(
                    "the authoritative item membership is invalid",
                    code="invalid_text_layer_authority_snapshot",
                    item_id=identifier,
                )
            self._item_exists = exists
        return self._item_exists

    def list(self, item_id: str) -> Sequence[TextLayerDocumentSnapshot]:
        self._ensure_open()
        identifier = self._bind_item(item_id)
        if not self.item_exists(identifier):
            return ()
        documents = self._load_documents()
        return tuple(
            documents[layer_id]
            for layer_id in sorted(
                documents, key=lambda value: (value.casefold(), value)
            )
        )

    def get(
        self, item_id: str, layer_id: str
    ) -> TextLayerDocumentSnapshot | None:
        self._ensure_open()
        identifier = self._bind_item(item_id)
        layer = _identifier(layer_id, field="layer_id")
        if not self.item_exists(identifier):
            return None
        return self._load_documents().get(layer)

    def source(
        self, item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot | None:
        self._ensure_open()
        identifier = self._bind_item(item_id)
        representation = _identifier(
            representation_id, field="representation_id"
        )
        if not self.item_exists(identifier):
            return None
        if representation not in self._sources:
            self._sources[representation] = self._read_source(
                identifier, representation
            )
        return self._sources[representation]

    def _read_source(
        self, item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot | None:
        try:
            source = self._source_snapshot_for(item_id, representation_id)
        except Exception as exc:
            raise _repository_error(
                "the authoritative text-layer source could not be read",
                code="text_layer_authority_unavailable",
                item_id=item_id,
                representation_id=representation_id,
                cause_type=type(exc).__name__,
            ) from exc
        if source is None:
            return None
        if (
            not isinstance(source, TextLayerSourceSnapshot)
            or source.item_id != item_id
            or source.representation_id != representation_id
        ):
            raise _repository_error(
                "the authoritative text-layer source is invalid",
                code="invalid_text_layer_authority_snapshot",
                item_id=item_id,
                representation_id=representation_id,
            )
        return source

    def _load_documents(
        self, *, refresh: bool = False
    ) -> dict[str, TextLayerDocumentSnapshot]:
        if self._documents is not None and not refresh:
            return dict(self._documents)
        item_id = self._require_bound_item()
        directory = self._document_directory(item_id)
        if not self._path_exists(directory):
            documents: dict[str, TextLayerDocumentSnapshot] = {}
        else:
            self._require_directory(directory, artifact="text_layer_documents")
            try:
                entries = tuple(
                    islice(directory.iterdir(), MAX_TEXT_LAYERS_PER_ITEM + 1)
                )
            except OSError as exc:
                raise _repository_error(
                    "the text-layer document store cannot be enumerated",
                    code="text_layer_repository_unavailable",
                    item_id=item_id,
                    cause_type=type(exc).__name__,
                ) from exc
            if len(entries) > MAX_TEXT_LAYERS_PER_ITEM:
                raise _repository_error(
                    "the item has too many stored text layers",
                    code="text_layer_collection_too_large",
                    item_id=item_id,
                )
            documents = {}
            aliases: dict[str, str] = {}
            for path in entries:
                if path.suffix != ".json":
                    raise _repository_error(
                        "the text-layer document store contains an unknown entry",
                        code="invalid_text_layer_storage",
                        item_id=item_id,
                    )
                layer_id = _file_identifier(path.stem, field="stored_layer_id")
                if path.name != f"{layer_id}.json":
                    raise _repository_error(
                        "a text-layer document name is not canonical",
                        code="invalid_text_layer_storage",
                        item_id=item_id,
                    )
                folded = layer_id.casefold()
                if folded in aliases:
                    raise _repository_error(
                        "the text-layer store contains aliased identities",
                        code="duplicate_text_layer_identity",
                        item_id=item_id,
                    )
                aliases[folded] = layer_id
                raw = self._read_json(
                    path,
                    maximum=MAX_TEXT_LAYER_DOCUMENT_BYTES,
                    artifact="text_layer_document",
                )
                document = self._decode_document(raw)
                if document.item_id != item_id or document.layer_id != layer_id:
                    raise _repository_error(
                        "a text-layer document has inconsistent scope",
                        code="text_layer_document_scope_mismatch",
                        item_id=item_id,
                    )
                documents[layer_id] = document
        self._documents = dict(documents)
        return documents

    def _decode_document(self, value: Any) -> TextLayerDocumentSnapshot:
        item_id = self._require_bound_item()
        if not isinstance(value, dict):
            raise _repository_error(
                "a text-layer document is not an object",
                code="invalid_text_layer_storage",
                item_id=item_id,
            )
        version = value.get("version")
        if type(version) is int and version > TEXT_LAYER_DOCUMENT_VERSION:
            raise _repository_error(
                "the text-layer document schema is newer than this adapter",
                code="text_layer_storage_newer_schema",
                item_id=item_id,
            )
        if (
            set(value) != {"schema", "version", "document"}
            or value.get("schema") != TEXT_LAYER_DOCUMENT_SCHEMA
            or type(version) is not int
            or version != TEXT_LAYER_DOCUMENT_VERSION
        ):
            raise _repository_error(
                "a text-layer document has an unsupported schema",
                code="invalid_text_layer_storage",
                item_id=item_id,
            )
        raw = value["document"]
        fields = {
            "item_id",
            "layer_id",
            "label",
            "kind",
            "language",
            "source",
            "preamble",
            "units",
            "document_revision",
            "content_revision",
        }
        try:
            if not isinstance(raw, dict) or set(raw) != fields:
                raise ValueError("document fields do not match the schema")
            units_raw = raw["units"]
            if not isinstance(units_raw, list) or len(units_raw) > MAX_TEXT_LAYER_UNITS:
                raise ValueError("document units are invalid")
            units: list[TextLayerUnitSnapshot] = []
            for unit_raw in units_raw:
                if not isinstance(unit_raw, dict) or set(unit_raw) != {
                    "selector",
                    "order",
                    "label",
                    "text",
                    "provenance",
                    "content_revision",
                    "unit_revision",
                }:
                    raise ValueError("unit fields do not match the schema")
                draft = TextLayerUnitDraft.from_dict(
                    {
                        name: unit_raw[name]
                        for name in (
                            "selector",
                            "order",
                            "label",
                            "text",
                            "provenance",
                        )
                    }
                )
                units.append(
                    TextLayerUnitSnapshot(
                        selector=draft.selector,
                        order=draft.order,
                        label=draft.label,
                        text=draft.text,
                        provenance=draft.provenance,
                        content_revision=unit_raw["content_revision"],
                        unit_revision=unit_raw["unit_revision"],
                    )
                )
            document = TextLayerDocumentSnapshot(
                item_id=raw["item_id"],
                layer_id=raw["layer_id"],
                source=TextLayerSourcePin.from_dict(raw["source"]),
                units=tuple(units),
                document_revision=raw["document_revision"],
                content_revision=raw["content_revision"],
                label=raw["label"],
                kind=raw["kind"],
                language=raw["language"],
                preamble=raw["preamble"],
            )
            # Keep this explicit even though the engine snapshot constructors
            # independently verify every supplied revision.  Storage decoding
            # must visibly derive identity from canonical content rather than
            # treating persisted revision strings as authority.
            derived = TextLayerDocumentSnapshot.build(
                document.item_id,
                document.layer_id,
                document.as_draft(),
            )
            if _canonical_json(derived.as_dict()) != _canonical_json(
                document.as_dict()
            ):
                raise ValueError("document revisions are not canonical")
            if _canonical_json(document.as_dict()) != _canonical_json(raw):
                raise ValueError("document is not in canonical value form")
            return document
        except RepositoryError:
            raise
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise _repository_error(
                "a text-layer document is invalid",
                code="invalid_text_layer_storage",
                item_id=item_id,
                cause_type=type(exc).__name__,
            ) from exc

    def _bind_item(self, item_id: str) -> str:
        identifier = _identifier(item_id, field="item_id")
        if self._item_id is None:
            self._item_id = identifier
        elif self._item_id != identifier:
            raise _repository_error(
                "the text-layer session was used for another item",
                code="text_layer_repository_scope_mismatch",
            )
        return identifier

    def _require_bound_item(self) -> str:
        if self._item_id is None:
            raise _repository_error(
                "the text-layer session has no item scope",
                code="text_layer_repository_scope_mismatch",
            )
        return self._item_id

    def _document_directory(self, item_id: str) -> Path:
        if self._entry_directory is None:
            try:
                configured = Path(self._entry_directory_for(item_id))
            except Exception as exc:
                raise _repository_error(
                    "the managed item directory could not be resolved",
                    code="unsafe_text_layer_storage_path",
                    item_id=item_id,
                    cause_type=type(exc).__name__,
                ) from exc
            if not configured.parts or any(
                part in {"", ".", ".."} for part in configured.parts
            ):
                raise _repository_error(
                    "the managed item directory is invalid",
                    code="unsafe_text_layer_storage_path",
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
                    "the managed item directory escapes the workspace",
                    code="unsafe_text_layer_storage_path",
                    item_id=item_id,
                ) from exc
            if (
                not relative.parts
                or relative.parts[0].casefold() in _RESERVED_WORKSPACE_ROOTS
            ):
                raise _repository_error(
                    "the managed item directory uses a reserved namespace",
                    code="unsafe_text_layer_storage_path",
                    item_id=item_id,
                )
            self._assert_safe_components(lexical)
            if self._path_exists(lexical):
                self._require_directory(lexical, artifact="item_directory")
            self._entry_directory = lexical
        directory = self._entry_directory.joinpath(
            *TEXT_LAYER_DOCUMENT_ROOT.parts
        )
        self._assert_safe_components(directory)
        return directory

    def _assert_safe_components(self, path: Path) -> None:
        try:
            relative = path.relative_to(self._write_set.root)
        except ValueError as exc:
            raise _repository_error(
                "a text-layer storage path escapes the workspace",
                code="unsafe_text_layer_storage_path",
            ) from exc
        current = self._write_set.root
        for part in relative.parts:
            current /= part
            if _is_redirecting_path(current):
                raise _repository_error(
                    "a text-layer storage path redirects outside its namespace",
                    code="unsafe_text_layer_storage_path",
                )
            if current != path:
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise _repository_error(
                        "a text-layer storage parent cannot be inspected",
                        code="text_layer_repository_unavailable",
                        cause_type=type(exc).__name__,
                    ) from exc
                if not stat.S_ISDIR(info.st_mode):
                    raise _repository_error(
                        "a text-layer storage parent is not a directory",
                        code="unsafe_text_layer_storage_path",
                    )
        try:
            path.resolve(strict=False).relative_to(self._write_set.root)
        except (OSError, ValueError) as exc:
            raise _repository_error(
                "a text-layer storage path escapes the workspace",
                code="unsafe_text_layer_storage_path",
            ) from exc

    def _path_exists(self, path: Path) -> bool:
        self._assert_safe_components(path)
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _repository_error(
                "a text-layer storage path cannot be inspected",
                code="text_layer_repository_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        return True

    def _require_directory(self, path: Path, *, artifact: str) -> None:
        self._assert_safe_components(path)
        try:
            info = path.lstat()
        except OSError as exc:
            raise _repository_error(
                "a text-layer directory cannot be inspected",
                code="text_layer_repository_unavailable",
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc
        if _is_redirecting_path(path) or not stat.S_ISDIR(info.st_mode):
            raise _repository_error(
                "a text-layer namespace is not a private directory",
                code="unsafe_text_layer_storage_path",
                artifact=artifact,
            )

    def _read_json(self, path: Path, *, maximum: int, artifact: str) -> Any:
        payload = self._read_bytes(path, maximum=maximum, artifact=artifact)
        try:
            return json.loads(
                payload.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise _repository_error(
                "a text-layer artifact cannot be decoded",
                code="invalid_text_layer_storage",
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc

    def _read_bytes(self, path: Path, *, maximum: int, artifact: str) -> bytes:
        self._assert_safe_components(path)
        try:
            named_before = path.lstat()
        except OSError as exc:
            raise _repository_error(
                "a text-layer artifact cannot be inspected",
                code="text_layer_repository_unavailable",
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            _is_redirecting_path(path)
            or not stat.S_ISREG(named_before.st_mode)
            or named_before.st_nlink != 1
        ):
            raise _repository_error(
                "a text-layer artifact is not a private regular file",
                code="unsafe_text_layer_storage_path",
                artifact=artifact,
            )
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise _repository_error(
                "a text-layer artifact cannot be opened",
                code="text_layer_repository_unavailable",
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc
        try:
            opened_before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_before.st_mode)
                or opened_before.st_nlink != 1
                or not os.path.samestat(opened_before, named_before)
            ):
                raise OSError("artifact identity changed")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(1 << 20, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            opened_after = os.fstat(descriptor)
        except OSError as exc:
            raise _repository_error(
                "a text-layer artifact changed while it was read",
                code="text_layer_repository_unavailable",
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc
        finally:
            os.close(descriptor)
        try:
            named_after = path.lstat()
        except OSError as exc:
            raise _repository_error(
                "a text-layer artifact changed while it was read",
                code="text_layer_repository_unavailable",
                artifact=artifact,
                cause_type=type(exc).__name__,
            ) from exc
        if (
            len(payload) > maximum
            or _is_redirecting_path(path)
            or not os.path.samestat(opened_after, named_after)
            or _stable_identity(opened_before) != _stable_identity(opened_after)
            or _stable_identity(opened_before) != _stable_identity(named_after)
        ):
            raise _repository_error(
                "a text-layer artifact changed or exceeded its storage limit",
                code="invalid_text_layer_storage",
                artifact=artifact,
            )
        return payload

    def _relative(self, path: Path) -> str:
        self._assert_safe_components(path)
        try:
            return path.relative_to(self._write_set.root).as_posix()
        except ValueError as exc:
            raise _repository_error(
                "a text-layer target escapes the workspace",
                code="unsafe_text_layer_storage_path",
            ) from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise _repository_error(
                "the text-layer session is closed",
                code="text_layer_session_closed",
            )


class FilesystemTextLayerAggregateUnitOfWork(
    FilesystemTextLayerAggregateSession
):
    """One operation-scoped staging buffer with an explicit atomic commit."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        operation_id: str,
        item_exists_for: ItemMembershipLookup,
        entry_directory_for: EntryDirectoryResolver,
        source_snapshot_for: TextLayerSourceLookup,
        layer_id_factory: TextLayerIdFactory,
    ) -> None:
        super().__init__(
            write_set,
            item_exists_for=item_exists_for,
            entry_directory_for=entry_directory_for,
            source_snapshot_for=source_snapshot_for,
        )
        self._operation_id = operation_id
        self._layer_id_factory = layer_id_factory
        self._receipt_checked = False
        self._allocated_layer_id: str | None = None
        self._staged_before: TextLayerDocumentSnapshot | None = None
        self._staged_after: TextLayerDocumentSnapshot | None = None
        self._staged_path: Path | None = None
        self._staged_payload: bytes | None = None
        self._committed = False

    def close(self) -> None:
        self._staged_payload = None
        super().close()

    def receipt(
        self, operation_id: str
    ) -> TextLayerStoredMutationReceipt | None:
        self._ensure_open()
        operation = _identifier(operation_id, field="operation_id")
        if operation != self._operation_id:
            raise _repository_error(
                "the replay lookup is outside this operation",
                code="text_layer_receipt_scope_mismatch",
            )
        path = self._receipt_path(operation)
        self._receipt_checked = True
        if not self._path_exists(path):
            return None
        value = self._read_json(
            path,
            maximum=MAX_TEXT_LAYER_REPLAY_BYTES,
            artifact="text_layer_replay",
        )
        return self._decode_receipt(value, operation_id=operation)

    def item_exists(self, item_id: str) -> bool:
        self._ensure_after_receipt()
        return super().item_exists(item_id)

    def list(self, item_id: str) -> Sequence[TextLayerDocumentSnapshot]:
        self._ensure_after_receipt()
        return super().list(item_id)

    def get(
        self, item_id: str, layer_id: str
    ) -> TextLayerDocumentSnapshot | None:
        self._ensure_after_receipt()
        return super().get(item_id, layer_id)

    def source(
        self, item_id: str, representation_id: str
    ) -> TextLayerSourceSnapshot | None:
        self._ensure_after_receipt()
        return super().source(item_id, representation_id)

    def allocate_layer_id(self, item_id: str) -> str:
        self._ensure_stageable()
        identifier = self._bind_item(item_id)
        if not self.item_exists(identifier):
            raise _repository_error(
                "the item does not exist",
                code="item_not_found",
                item_id=identifier,
            )
        if self._allocated_layer_id is not None:
            return self._allocated_layer_id
        documents = self._load_documents(refresh=True)
        aliases = {value.casefold() for value in documents}
        for _attempt in range(MAX_TEXT_LAYER_ID_ALLOCATION_ATTEMPTS):
            try:
                candidate = self._layer_id_factory()
            except Exception as exc:
                raise _repository_error(
                    "a text-layer identity could not be allocated",
                    code="text_layer_id_allocation_failed",
                    cause_type=type(exc).__name__,
                ) from exc
            candidate = _file_identifier(candidate, field="allocated_layer_id")
            if candidate.casefold() not in aliases:
                self._allocated_layer_id = candidate
                return candidate
        raise _repository_error(
            "a unique text-layer identity could not be allocated",
            code="text_layer_id_allocation_exhausted",
        )

    def stage_create(
        self,
        item_id: str,
        layer_id: str,
        draft: TextLayerDraft,
    ) -> TextLayerDocumentSnapshot:
        self._ensure_stageable()
        identifier = self._bind_item(item_id)
        layer = _file_identifier(layer_id, field="layer_id")
        if not isinstance(draft, TextLayerDraft):
            raise _repository_error(
                "the text-layer draft is invalid",
                code="invalid_text_layer_repository_command",
            )
        if self._allocated_layer_id != layer:
            raise _repository_error(
                "the create identity was not allocated by this unit",
                code="text_layer_repository_scope_mismatch",
            )
        documents = self._load_documents(refresh=True)
        if any(stored.casefold() == layer.casefold() for stored in documents):
            raise _repository_error(
                "the allocated text-layer identity is already in use",
                code="allocated_text_layer_id_collision",
                item_id=identifier,
                layer_id=layer,
            )
        after = TextLayerDocumentSnapshot.build(identifier, layer, draft)
        self._stage(None, after)
        return after

    def stage_replace(
        self,
        current: TextLayerDocumentSnapshot,
        draft: TextLayerDraft,
    ) -> TextLayerDocumentSnapshot:
        self._ensure_stageable()
        if not isinstance(current, TextLayerDocumentSnapshot) or not isinstance(
            draft, TextLayerDraft
        ):
            raise _repository_error(
                "the text-layer replacement input is invalid",
                code="invalid_text_layer_repository_command",
            )
        item_id = self._bind_item(current.item_id)
        _file_identifier(current.layer_id, field="layer_id")
        if not self.item_exists(item_id):
            raise _repository_error(
                "the item does not exist",
                code="item_not_found",
                item_id=item_id,
            )
        documents = self._load_documents(refresh=True)
        stored = documents.get(current.layer_id)
        if stored is None or _canonical_json(stored.as_dict()) != _canonical_json(
            current.as_dict()
        ):
            raise _repository_error(
                "the text layer changed outside the locked snapshot",
                code="text_layer_document_revision_conflict",
                item_id=item_id,
                layer_id=current.layer_id,
            )
        current_by_selector = {value.selector: value for value in current.units}
        candidate_by_selector = {value.selector: value for value in draft.units}
        stable_document_fields = (
            draft.source == current.source
            and draft.label == current.label
            and draft.kind == current.kind
            and draft.language == current.language
            and draft.preamble == current.preamble
            and set(candidate_by_selector) == set(current_by_selector)
            and all(
                candidate_by_selector[selector].order == value.order
                and candidate_by_selector[selector].label == value.label
                for selector, value in current_by_selector.items()
            )
        )
        if not stable_document_fields:
            raise _repository_error(
                "a unit replacement changed immutable document structure",
                code="text_layer_repository_content_mismatch",
                item_id=item_id,
                layer_id=current.layer_id,
            )
        after = TextLayerDocumentSnapshot.build(item_id, current.layer_id, draft)
        if after.document_revision == current.document_revision:
            raise _repository_error(
                "the staged text-layer replacement is unchanged",
                code="text_layer_revision_not_advanced",
                item_id=item_id,
                layer_id=current.layer_id,
            )
        self._stage(current, after)
        return after

    def commit(self, receipt: TextLayerStoredMutationReceipt) -> None:
        self._ensure_stageable(require_staged=True)
        assert self._staged_after is not None
        assert self._staged_path is not None
        assert self._staged_payload is not None
        self._validate_receipt(receipt)

        # Re-read state and the authoritative source immediately before the
        # publication boundary.  This catches non-cooperating writers even
        # though well-behaved callers share the surrounding locks.
        documents = self._load_documents(refresh=True)
        live = documents.get(self._staged_after.layer_id)
        if self._staged_before is None:
            if live is not None or any(
                value.casefold() == self._staged_after.layer_id.casefold()
                for value in documents
            ):
                raise _repository_error(
                    "the text-layer create target changed before publication",
                    code="text_layer_document_revision_conflict",
                )
        elif live is None or _canonical_json(live.as_dict()) != _canonical_json(
            self._staged_before.as_dict()
        ):
            raise _repository_error(
                "the text layer changed before publication",
                code="text_layer_document_revision_conflict",
            )
        try:
            item_still_exists = self._item_exists_for(self._staged_after.item_id)
        except Exception as exc:
            raise _repository_error(
                "the authoritative item membership could not be rechecked",
                code="text_layer_authority_unavailable",
                cause_type=type(exc).__name__,
            ) from exc
        if not isinstance(item_still_exists, bool):
            raise _repository_error(
                "the authoritative item membership is invalid",
                code="invalid_text_layer_authority_snapshot",
            )
        if not item_still_exists:
            raise _repository_error(
                "the item disappeared before text-layer publication",
                code="item_not_found",
            )
        authoritative = self._read_source(
            self._staged_after.item_id,
            self._staged_after.source.representation_id,
        )
        if (
            authoritative is None
            or authoritative.revision != self._staged_after.source.revision
        ):
            raise _repository_error(
                "the text-layer source changed before publication",
                code="text_layer_source_revision_conflict",
            )
        receipt_path = self._receipt_path(self._operation_id)
        if self._path_exists(receipt_path):
            raise _repository_error(
                "a durable replay envelope already exists",
                code="text_layer_receipt_exists",
            )
        receipt_payload = self._encode_receipt(receipt)
        try:
            transaction = self._write_set.begin(
                operation_id=self._operation_id,
                scope="text-layer-aggregate",
                metadata={
                    "action": receipt.receipt.action,
                    "item_id": receipt.receipt.item_id,
                    "layer_id": receipt.receipt.layer_id,
                },
            )
            transaction.stage_write(
                self._relative(self._staged_path), self._staged_payload
            )
            transaction.stage_write(
                self._relative(receipt_path), receipt_payload
            )
            transaction.commit(receipt=receipt.receipt.as_public_dict())
        except WriteSetError as exc:
            raise _repository_error(
                "the text-layer transaction failed",
                code=exc.code,
                cause_type=type(exc).__name__,
            ) from exc
        except Exception as exc:
            raise _repository_error(
                "the text-layer transaction failed",
                code="text_layer_transaction_failed",
                cause_type=type(exc).__name__,
            ) from exc
        self._committed = True
        self._documents = {**documents, self._staged_after.layer_id: self._staged_after}

    def _stage(
        self,
        before: TextLayerDocumentSnapshot | None,
        after: TextLayerDocumentSnapshot,
    ) -> None:
        path = self._document_directory(after.item_id) / f"{after.layer_id}.json"
        self._assert_safe_components(path)
        envelope = {
            "schema": TEXT_LAYER_DOCUMENT_SCHEMA,
            "version": TEXT_LAYER_DOCUMENT_VERSION,
            "document": after.as_dict(),
        }
        self._staged_before = before
        self._staged_after = after
        self._staged_path = path
        self._staged_payload = _bounded_json_bytes(
            envelope,
            maximum=MAX_TEXT_LAYER_DOCUMENT_BYTES,
            artifact="text_layer_document",
        )

    def _validate_receipt(self, stored: TextLayerStoredMutationReceipt) -> None:
        before = self._staged_before
        after = self._staged_after
        if not isinstance(stored, TextLayerStoredMutationReceipt) or after is None:
            raise _repository_error(
                "the text-layer receipt is invalid",
                code="invalid_text_layer_receipt",
            )
        receipt = stored.receipt
        expected_action = "create" if before is None else receipt.action
        valid_action = (
            receipt.action == "create"
            if before is None
            else receipt.action in {"replace-unit", "replace-batch"}
        )
        expected_units = () if before is None else _changed_units(before, after)
        if (
            not valid_action
            or receipt.action != expected_action
            or (receipt.action == "replace-unit" and len(expected_units) != 1)
            or receipt.operation_id != self._operation_id
            or receipt.item_id != after.item_id
            or receipt.layer_id != after.layer_id
            or receipt.source_revision != after.source.revision
            or receipt.after_document_revision != after.document_revision
            or receipt.after_content_revision != after.content_revision
            or receipt.units != expected_units
            or receipt.before_document_revision
            != ("" if before is None else before.document_revision)
            or receipt.before_content_revision
            != ("" if before is None else before.content_revision)
        ):
            raise _repository_error(
                "the text-layer receipt is outside the staged mutation",
                code="text_layer_receipt_scope_mismatch",
            )

    def _receipt_path(self, operation_id: str) -> Path:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        path = self._write_set.root.joinpath(
            *TEXT_LAYER_REPLAY_ROOT.parts, f"{digest}.json"
        )
        self._assert_safe_components(path)
        return path

    def _encode_receipt(self, receipt: TextLayerStoredMutationReceipt) -> bytes:
        digest = hashlib.sha256(self._operation_id.encode("utf-8")).hexdigest()
        stored = receipt.as_storage_dict()
        return _bounded_json_bytes(
            {
                "schema": TEXT_LAYER_REPLAY_SCHEMA,
                "version": TEXT_LAYER_REPLAY_VERSION,
                "operation_sha256": digest,
                "stored_receipt_sha256": hashlib.sha256(
                    _canonical_json(stored)
                ).hexdigest(),
                "stored_receipt": stored,
            },
            maximum=MAX_TEXT_LAYER_REPLAY_BYTES,
            artifact="text_layer_replay",
        )

    def _decode_receipt(
        self, value: Any, *, operation_id: str
    ) -> TextLayerStoredMutationReceipt:
        if not isinstance(value, dict):
            raise _repository_error(
                "a text-layer replay envelope is not an object",
                code="invalid_text_layer_receipt",
            )
        version = value.get("version")
        if type(version) is int and version > TEXT_LAYER_REPLAY_VERSION:
            raise _repository_error(
                "the text-layer replay schema is newer than this adapter",
                code="text_layer_receipt_newer_schema",
            )
        if (
            set(value)
            != {
                "schema",
                "version",
                "operation_sha256",
                "stored_receipt_sha256",
                "stored_receipt",
            }
            or value.get("schema") != TEXT_LAYER_REPLAY_SCHEMA
            or type(version) is not int
            or version != TEXT_LAYER_REPLAY_VERSION
        ):
            raise _repository_error(
                "a text-layer replay envelope has an unsupported schema",
                code="invalid_text_layer_receipt",
            )
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        if (
            not isinstance(value["operation_sha256"], str)
            or not _SHA256_RE.fullmatch(value["operation_sha256"])
            or value["operation_sha256"] != digest
        ):
            raise _repository_error(
                "a text-layer replay envelope has inconsistent scope",
                code="text_layer_receipt_scope_mismatch",
            )
        try:
            stored_digest = value["stored_receipt_sha256"]
            if (
                not isinstance(stored_digest, str)
                or not _SHA256_RE.fullmatch(stored_digest)
                or stored_digest
                != hashlib.sha256(
                    _canonical_json(value["stored_receipt"])
                ).hexdigest()
            ):
                raise ValueError("stored receipt digest does not match")
            stored = TextLayerStoredMutationReceipt.from_storage_dict(
                value["stored_receipt"]
            )
            if stored.receipt.operation_id != operation_id:
                raise ValueError("receipt operation does not match its envelope")
            if _canonical_json(stored.as_storage_dict()) != _canonical_json(
                value["stored_receipt"]
            ):
                raise ValueError("stored receipt is not canonical")
            return stored
        except RepositoryError:
            raise
        except (TypeError, ValueError, KeyError, RecursionError) as exc:
            raise _repository_error(
                "a text-layer replay envelope is invalid",
                code="invalid_text_layer_receipt",
                cause_type=type(exc).__name__,
            ) from exc

    def _ensure_after_receipt(self) -> None:
        self._ensure_open()
        if not self._receipt_checked:
            raise _repository_error(
                "live text-layer state was requested before replay lookup",
                code="text_layer_receipt_lookup_required",
            )

    def _ensure_stageable(self, *, require_staged: bool = False) -> None:
        self._ensure_after_receipt()
        if self._committed:
            raise _repository_error(
                "the text-layer mutation unit is already committed",
                code="text_layer_unit_committed",
            )
        staged = self._staged_after is not None
        if require_staged and not staged:
            raise _repository_error(
                "the text-layer mutation unit has no staged document",
                code="text_layer_mutation_not_staged",
            )
        if not require_staged and staged:
            raise _repository_error(
                "a text-layer mutation is already staged",
                code="text_layer_mutation_already_staged",
            )


__all__ = [
    "EntryDirectoryResolver",
    "FilesystemTextLayerAggregateRepository",
    "ItemMembershipLookup",
    "MAX_TEXT_LAYER_DOCUMENT_BYTES",
    "MAX_TEXT_LAYER_REPLAY_BYTES",
    "TEXT_LAYER_DOCUMENT_ROOT",
    "TEXT_LAYER_DOCUMENT_SCHEMA",
    "TEXT_LAYER_DOCUMENT_VERSION",
    "TEXT_LAYER_REPLAY_ROOT",
    "TEXT_LAYER_REPLAY_SCHEMA",
    "TEXT_LAYER_REPLAY_VERSION",
    "TextLayerIdFactory",
    "TextLayerLockFactory",
    "TextLayerSourceLookup",
]
