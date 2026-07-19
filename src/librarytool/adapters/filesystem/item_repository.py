"""Callback-backed adapter for the current mapping/file item stores.

The adapter performs no imports from ``tools`` and resolves no global data
root.  A composition root injects loaders for its JSON store and optional
representation/artifact summaries.  That keeps the engine query service
usable in tests, a CLI, Flask, Qt, or another client runtime.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ...engine.errors import RepositoryError


JsonMapping = Mapping[str, Any]
ItemsLoader = Callable[[], Mapping[str, JsonMapping] | Sequence[JsonMapping]]
SummaryLoader = Callable[[str, JsonMapping], Sequence[JsonMapping]]


class FilesystemItemQueryRepository:
    """Project mapping snapshots through the engine item-query port.

    ``load_items`` may return today's ``{id: record}`` JSON shape or a list of
    records that already carry ``id``/``item_id``.  Optional summary loaders
    let the composition root project entry-folder manifests without exposing
    their paths or helper functions to the engine package.

    When no representation loader is supplied, the adapter understands the
    transitional ``pdf_file``/``pdf_sources`` fields.  When no artifact loader
    is supplied, it reads an optional in-record ``artifacts`` list.  These are
    compatibility projections, not the target canonical storage model.
    """

    def __init__(
        self,
        load_items: ItemsLoader,
        *,
        load_representations: SummaryLoader | None = None,
        load_artifacts: SummaryLoader | None = None,
    ) -> None:
        if not callable(load_items):
            raise TypeError("load_items must be callable")
        if load_representations is not None and not callable(load_representations):
            raise TypeError("load_representations must be callable")
        if load_artifacts is not None and not callable(load_artifacts):
            raise TypeError("load_artifacts must be callable")
        self._load_items = load_items
        self._load_representations = load_representations
        self._load_artifacts = load_artifacts

    def list_records(self) -> tuple[dict[str, Any], ...]:
        return tuple(record for _item_id, record in self._snapshot_rows())

    def get_record(self, item_id: str) -> dict[str, Any] | None:
        value = str(item_id or "").strip()
        for candidate, record in self._snapshot_rows():
            if candidate == value:
                return record
        return None

    def list_representation_records(
        self, item_id: str, item_record: JsonMapping | None = None
    ) -> tuple[dict[str, Any], ...]:
        record = (
            self._detached_record(item_record, item_id=item_id)
            if isinstance(item_record, Mapping)
            else self.get_record(item_id)
        )
        if record is None:
            return ()
        if self._load_representations is not None:
            try:
                raw = self._load_representations(
                    item_id, self._detached_record(record, item_id=item_id)
                )
            except RepositoryError:
                raise
            except Exception as exc:
                raise RepositoryError(
                    "the representation summary could not be loaded",
                    code="representation_repository_unavailable",
                    details={"item_id": item_id, "reason": str(exc)},
                    retryable=True,
                ) from exc
            return self._summary(raw, item_id=item_id, section="representations")
        return self._default_representations(record)

    def list_artifact_records(
        self, item_id: str, item_record: JsonMapping | None = None
    ) -> tuple[dict[str, Any], ...]:
        record = (
            self._detached_record(item_record, item_id=item_id)
            if isinstance(item_record, Mapping)
            else self.get_record(item_id)
        )
        if record is None:
            return ()
        if self._load_artifacts is not None:
            try:
                raw = self._load_artifacts(
                    item_id, self._detached_record(record, item_id=item_id)
                )
            except RepositoryError:
                raise
            except Exception as exc:
                raise RepositoryError(
                    "the artifact summary could not be loaded",
                    code="artifact_repository_unavailable",
                    details={"item_id": item_id, "reason": str(exc)},
                    retryable=True,
                ) from exc
            return self._summary(raw, item_id=item_id, section="artifacts")
        return self._summary(
            record.get("artifacts") or (), item_id=item_id, section="artifacts"
        )

    def _snapshot_rows(self) -> tuple[tuple[str, dict[str, Any]], ...]:
        try:
            raw = self._load_items()
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the item repository could not be loaded",
                code="item_repository_unavailable",
                details={"reason": str(exc)},
                retryable=True,
            ) from exc
        rows: list[tuple[str, dict[str, Any]]] = []
        if isinstance(raw, Mapping):
            iterable = raw.items()
            for key, value in iterable:
                if not isinstance(value, Mapping):
                    raise RepositoryError(
                        "item mappings must contain object records",
                        code="invalid_item_repository_snapshot",
                    )
                record = self._detached_record(value, item_id=str(key))
                key_id = str(key).strip()
                embedded_id = str(
                    record.get("item_id") or record.get("id") or ""
                ).strip()
                if embedded_id and embedded_id != key_id:
                    raise RepositoryError(
                        "an item record conflicts with its repository key",
                        code="item_identity_conflict",
                        details={
                            "repository_key": key_id,
                            "record_id": embedded_id,
                        },
                    )
                item_id = embedded_id or key_id
                if not embedded_id:
                    record["id"] = item_id
                rows.append((item_id, record))
            return tuple(rows)
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise RepositoryError(
                "the item repository must return an object or array",
                code="invalid_item_repository_snapshot",
            )
        for value in raw:
            if not isinstance(value, Mapping):
                raise RepositoryError(
                    "item arrays must contain object records",
                    code="invalid_item_repository_snapshot",
                )
            record = self._detached_record(value)
            item_id = str(record.get("item_id") or record.get("id") or "").strip()
            rows.append((item_id, record))
        return tuple(rows)

    @staticmethod
    def _detached_record(
        raw: Mapping[str, Any], *, item_id: str = ""
    ) -> dict[str, Any]:
        try:
            return copy.deepcopy(dict(raw))
        except Exception as exc:
            raise RepositoryError(
                "an item repository record could not be detached",
                code="invalid_item_repository_snapshot",
                details={"item_id": item_id, "reason": str(exc)},
            ) from exc

    @classmethod
    def _summary(
        cls, raw: Any, *, item_id: str = "", section: str = "summary"
    ) -> tuple[dict[str, Any], ...]:
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise RepositoryError(
                "summary loaders must return an array",
                code="invalid_item_repository_snapshot",
                details={"item_id": item_id, "section": section},
            )
        if any(not isinstance(value, Mapping) for value in raw):
            raise RepositoryError(
                "summary arrays must contain object records",
                code="invalid_item_repository_snapshot",
                details={"item_id": item_id, "section": section},
            )
        return tuple(cls._detached_record(value, item_id=item_id) for value in raw)

    @classmethod
    def _default_representations(
        cls, record: JsonMapping
    ) -> tuple[dict[str, Any], ...]:
        supplied = record.get("representations")
        if isinstance(supplied, Sequence) and not isinstance(supplied, (str, bytes)):
            return cls._summary(supplied, section="representations")

        rows: list[dict[str, Any]] = []
        primary = str(record.get("pdf_file") or "").strip()
        if primary:
            rows.append(
                {
                    "id": "primary",
                    "role": "primary",
                    "media_type": "application/pdf",
                    "locator": primary,
                    "label": "Primary source",
                    "available": bool(record.get("pdf_available", True)),
                    "pages": record.get("pages"),
                }
            )
        sources = record.get("pdf_sources")
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes)):
            for source in sources:
                if not isinstance(source, Mapping):
                    continue
                locator = str(source.get("path") or "").strip()
                if not locator:
                    continue
                rows.append(
                    {
                        "id": str(source.get("id") or "").strip(),
                        "role": str(source.get("role") or "alternate"),
                        "media_type": str(
                            source.get("media_type") or "application/pdf"
                        ),
                        "locator": locator,
                        "label": str(source.get("label") or "Alternate source"),
                        "available": bool(source.get("available", True)),
                        "pages": source.get("pages"),
                    }
                )
        return tuple(rows)


__all__ = ["FilesystemItemQueryRepository"]
