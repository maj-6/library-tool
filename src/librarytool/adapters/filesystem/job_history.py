"""Filesystem persistence adapter for background-job history."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


ReadJson = Callable[[Path, Any], Any]
WriteJson = Callable[[Path, Any], None]


class FilesystemJobHistoryRepository:
    """Persist the manager's allowlisted snapshot through injected JSON I/O.

    The callbacks let the transitional application reuse its hardened atomic
    writer without making the installable engine package import ``tools``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        read_json: ReadJson,
        write_json: WriteJson,
    ) -> None:
        self._path = Path(path)
        self._read_json = read_json
        self._write_json = write_json

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Mapping[str, Mapping[str, Any]]:
        value = self._read_json(self._path, {})
        return value if isinstance(value, Mapping) else {}

    def save(self, jobs: Mapping[str, Mapping[str, Any]]) -> None:
        self._write_json(
            self._path,
            {str(job_id): dict(job) for job_id, job in jobs.items()},
        )


__all__ = ["FilesystemJobHistoryRepository"]
