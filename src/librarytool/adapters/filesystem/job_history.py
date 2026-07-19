"""Filesystem persistence adapter for background-job history."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


ReadJson = Callable[[Path, Any], Any]
WriteJson = Callable[[Path, Any], None]


class FilesystemJobHistoryRepository:
    """Persist the manager's allowlisted snapshot through strict JSON I/O.

    Optional callbacks preserve a transitional compatibility seam.  The native
    implementation validates opened file identity and publishes a complete,
    fsynced replacement, with a best-effort parent-directory flush on POSIX,
    without importing ``tools``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        read_json: ReadJson | None = None,
        write_json: WriteJson | None = None,
    ) -> None:
        self._path = Path(path)
        if read_json is not None and not callable(read_json):
            raise TypeError("read_json must be callable")
        if write_json is not None and not callable(write_json):
            raise TypeError("write_json must be callable")
        self._read_json = read_json or self._default_read_json
        self._write_json = write_json or self._default_write_json

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Mapping[str, Mapping[str, Any]]:
        value = self._read_json(self._path, {})
        if not isinstance(value, Mapping):
            raise ValueError("job history must be a JSON object")
        return value

    def save(self, jobs: Mapping[str, Mapping[str, Any]]) -> None:
        self._write_json(
            self._path,
            {str(job_id): dict(job) for job_id, job in jobs.items()},
        )

    @staticmethod
    def _default_read_json(path: Path, default: Any) -> Any:
        if path.is_symlink():
            raise OSError("job history is not a regular file")
        flags = os.O_RDONLY
        flags |= int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_CLOEXEC", 0))
        flags |= int(getattr(os, "O_NOINHERIT", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return default
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
                raise OSError("job history is not a regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                payload = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError(f"duplicate JSON key: {key}")
                value[key] = item
            return value

        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )

    @staticmethod
    def _default_write_json(path: Path, value: Any) -> None:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("job history may not be a symbolic link")
        if os.path.lexists(path):
            existing = path.lstat()
            if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
                raise OSError("job history is not one private regular file")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            FilesystemJobHistoryRepository._fsync_directory(path.parent)
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


__all__ = ["FilesystemJobHistoryRepository"]
