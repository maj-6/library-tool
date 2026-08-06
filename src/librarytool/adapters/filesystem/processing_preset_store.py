"""Filesystem persistence for named processing presets.

The whole collection lives in one small workspace document.  Presets are
user-authored configuration rather than immutable history, so this adapter is
deliberately much simpler than the correction transform store: no journals, no
content-addressed object files, no replay.  What it does share is the strict
JSON I/O discipline used everywhere else in this package — identity-checked
descriptors, duplicate-key rejection, and a complete fsynced replacement — so a
half-written document can never become visible.

Every mutation is a read-modify-write, so the compare-and-swap happens here,
inside the same lock as the write.  A service that read, compared, then asked
this adapter to save would leave a window for a concurrent editor to lose an
edit silently.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from ...engine.errors import RepositoryError
from ...engine.processing_presets import (
    MAX_PRESETS_PER_WORKSPACE,
    ProcessingPreset,
    preset_conflict,
    preset_not_found,
)
from ._file_lock import (
    is_lock_contention,
    lock_stream,
    open_lock_file,
    unlock_stream,
)

PROCESSING_PRESET_STORE_SCHEMA = "librarytool.processing-presets"
PROCESSING_PRESET_STORE_VERSION = 1
# Workspace-relative and fixed: presets are configuration with one home, not a
# host-configurable location like the catalogue or entry tree.
PROCESSING_PRESET_RELATIVE = PurePosixPath("processing_presets.json")
_TOP_LEVEL_FIELDS = frozenset({"schema", "version", "presets"})
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


def _repository_error(
    message: str,
    *,
    code: str,
    cause: Exception | None = None,
    retryable: bool = False,
) -> RepositoryError:
    details: dict[str, Any] = {"artifact": "processing_presets"}
    if cause is not None:
        details["cause"] = str(cause)
    return RepositoryError(message, code=code, details=details, retryable=retryable)


class FilesystemProcessingPresetStore:
    """Store the workspace preset collection in one strict JSON document."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock_path = self._path.with_name(f"{self._path.name}.lock")

    @property
    def path(self) -> Path:
        return self._path

    # -- public port ----------------------------------------------------

    def list_presets(self) -> tuple[ProcessingPreset, ...]:
        # Windows does not permit os.replace while a reader has the destination
        # open, and the descriptor/name identity check on POSIX must not race an
        # atomic replacement either. Reads therefore share the mutation lock.
        # A read waits for the tiny write critical section instead of surfacing
        # an avoidable transient conflict to the workbench.
        with self._locked(blocking=True):
            return self._read()

    def create_preset(self, preset: ProcessingPreset) -> ProcessingPreset:
        with self._locked():
            presets = self._read()
            existing = next(
                (
                    value
                    for value in presets
                    if value.preset_id == preset.preset_id
                ),
                None,
            )
            if existing is not None:
                # PUT is acknowledgement-safe: if the first response was lost,
                # repeating the exact desired representation reports the state
                # that is already durable. A different representation with the
                # same identity remains a genuine create conflict.
                if existing == preset:
                    return existing
                raise preset_conflict(
                    preset.preset_id,
                    code="processing_preset_exists",
                    message="a preset with this identifier already exists",
                )
            if len(presets) >= MAX_PRESETS_PER_WORKSPACE:
                raise preset_conflict(
                    preset.preset_id,
                    code="processing_preset_limit_reached",
                    message=(
                        "this workspace already holds "
                        f"{MAX_PRESETS_PER_WORKSPACE} presets"
                    ),
                )
            self._write((*presets, preset))
            return preset

    def update_preset(
        self,
        preset: ProcessingPreset,
        expected_revision: str,
    ) -> ProcessingPreset:
        with self._locked():
            presets = self._read()
            index = self._index_of(presets, preset.preset_id)
            current = presets[index]
            if current.revision != expected_revision:
                # A committed update followed by a lost response is already in
                # the requested state. Treat that retry as success without
                # weakening CAS for a different desired representation.
                if current == preset:
                    return current
                raise preset_conflict(
                    preset.preset_id,
                    code="processing_preset_stale",
                    message="the preset changed since it was read",
                )
            replaced = (*presets[:index], preset, *presets[index + 1 :])
            self._write(replaced)
            return preset

    def delete_preset(self, preset_id: str, expected_revision: str) -> None:
        with self._locked():
            presets = self._read()
            index = next(
                (
                    index
                    for index, value in enumerate(presets)
                    if value.preset_id == preset_id
                ),
                None,
            )
            # DELETE is idempotent. An absent identity is already in the
            # requested state; if it has been recreated, the live record below
            # still has to match the caller's revision before it can be removed.
            if index is None:
                return
            if presets[index].revision != expected_revision:
                raise preset_conflict(
                    preset_id,
                    code="processing_preset_stale",
                    message="the preset changed since it was read",
                )
            self._write((*presets[:index], *presets[index + 1 :]))

    # -- internals ------------------------------------------------------

    @staticmethod
    def _index_of(
        presets: Sequence[ProcessingPreset],
        preset_id: str,
    ) -> int:
        for index, value in enumerate(presets):
            if value.preset_id == preset_id:
                return index
        raise preset_not_found(preset_id)

    @contextmanager
    def _locked(self, *, blocking: bool = False):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = open_lock_file(self._lock_path)
        except OSError as exc:
            raise _repository_error(
                "the processing preset lock could not be opened",
                code="processing_preset_lock_failed",
                cause=exc,
                retryable=True,
            ) from exc
        try:
            try:
                # Mutations remain non-blocking: preset edits are interactive,
                # so a competing editor receives an immediate retryable
                # conflict. Reads opt into blocking for the short critical
                # section so Windows never has an open reader during replace.
                lock_stream(
                    stream,
                    blocking=blocking,
                    path=self._lock_path,
                )
            except OSError as exc:
                raise _repository_error(
                    "another editor is changing the processing presets",
                    code="processing_preset_locked"
                    if is_lock_contention(exc)
                    else "processing_preset_lock_failed",
                    cause=exc,
                    retryable=True,
                ) from exc
            try:
                yield
            finally:
                unlock_stream(stream)
        finally:
            stream.close()

    def _read(self) -> tuple[ProcessingPreset, ...]:
        raw = self._read_json()
        if raw is None:
            return ()
        if not isinstance(raw, dict) or frozenset(raw) != _TOP_LEVEL_FIELDS:
            raise _repository_error(
                "the processing preset document has invalid fields",
                code="invalid_processing_preset_storage",
            )
        version = raw["version"]
        if (
            raw["schema"] != PROCESSING_PRESET_STORE_SCHEMA
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version != PROCESSING_PRESET_STORE_VERSION
        ):
            raise _repository_error(
                "the processing preset document version is unsupported",
                code="unsupported_processing_preset_version",
            )
        rows = raw["presets"]
        if not isinstance(rows, list):
            raise _repository_error(
                "the processing preset document must hold a list",
                code="invalid_processing_preset_storage",
            )
        try:
            presets = tuple(ProcessingPreset.from_dict(row) for row in rows)
        except Exception as exc:
            raise _repository_error(
                "a stored processing preset is invalid",
                code="invalid_processing_preset_storage",
                cause=exc,
            ) from exc
        identities = [value.preset_id for value in presets]
        if len(set(identities)) != len(identities):
            raise _repository_error(
                "stored processing presets must have unique identifiers",
                code="invalid_processing_preset_storage",
            )
        return presets

    def _write(self, presets: Sequence[ProcessingPreset]) -> None:
        document = {
            "schema": PROCESSING_PRESET_STORE_SCHEMA,
            "version": PROCESSING_PRESET_STORE_VERSION,
            "presets": [value.as_dict() for value in presets],
        }
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise _repository_error(
                "the processing preset document is too large",
                code="processing_preset_document_too_large",
            )
        try:
            self._write_bytes(payload)
        except RepositoryError:
            raise
        except OSError as exc:
            raise _repository_error(
                "the processing presets could not be written",
                code="processing_preset_write_failed",
                cause=exc,
                retryable=True,
            ) from exc

    def _read_json(self) -> Any:
        path = self._path
        if path.is_symlink():
            raise _repository_error(
                "the processing preset document is not a regular file",
                code="invalid_processing_preset_storage",
            )
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOINHERIT", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _repository_error(
                "the processing presets could not be read",
                code="processing_preset_read_failed",
                cause=exc,
                retryable=True,
            ) from exc
        try:
            opened = os.fstat(descriptor)
            named = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or opened.st_nlink != 1
                or named.st_nlink != 1
                or not os.path.samestat(opened, named)
            ):
                raise _repository_error(
                    "the processing preset document is not one private regular file",
                    code="invalid_processing_preset_storage",
                )
            if opened.st_size > _MAX_DOCUMENT_BYTES:
                raise _repository_error(
                    "the processing preset document is too large",
                    code="processing_preset_document_too_large",
                )
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                payload = stream.read(_MAX_DOCUMENT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        def unique_object(pairs):
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError(f"duplicate JSON key: {key}")
                value[key] = item
            return value

        try:
            return json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=unique_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (UnicodeError, ValueError) as exc:
            raise _repository_error(
                "the processing preset document is invalid JSON",
                code="invalid_processing_preset_storage",
                cause=exc,
            ) from exc

    def _write_bytes(self, payload: bytes) -> None:
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise _repository_error(
                "the processing preset document may not be a symbolic link",
                code="invalid_processing_preset_storage",
            )
        if os.path.lexists(path):
            existing = path.lstat()
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise _repository_error(
                    "the processing preset document is not one private regular file",
                    code="invalid_processing_preset_storage",
                )
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "PROCESSING_PRESET_RELATIVE",
    "PROCESSING_PRESET_STORE_SCHEMA",
    "PROCESSING_PRESET_STORE_VERSION",
    "FilesystemProcessingPresetStore",
]
