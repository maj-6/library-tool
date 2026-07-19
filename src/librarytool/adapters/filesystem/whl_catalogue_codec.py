"""Legacy World Herb Library catalogue-row codec.

The current filesystem catalogue stores book metadata and operational state in
one JSON object.  This adapter isolates that transitional storage shape from
Flask and from the framework-neutral item command service.  Callers inject the
revision clock, category vocabulary, and representation-manifest validator;
the codec imports no host globals and performs no I/O.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeAlias

from ...engine.errors import EngineError, RepositoryError
from ...engine.item_commands import ItemDraft, ItemRecordSnapshot


RevisionAdvancer: TypeAlias = Callable[[str], str]
CategoryIdsLoader: TypeAlias = Callable[[], Iterable[str]]
RepresentationManifestValidator: TypeAlias = Callable[[Mapping[str, Any]], Any]

_LOCAL_FIELDS = frozenset({"pdf_file", "pdf_sources", "images", "extra"})
_MANAGED_FIELDS = frozenset(
    {
        "id",
        "item_id",
        "kind",
        "title",
        "created_at",
        "updated_at",
        "revision",
        "representations",
        "artifacts",
        "relevance",
        "capture_id",
        "published_slug",
        "ocr_active",
        "ocr_verified",
        "ocr_quality",
        "title_pages",
        "thumbnail_source",
        "status",
        "representation_manifest",
    }
) | _LOCAL_FIELDS
_MANAGED_STRING_FIELDS = frozenset(
    {
        "published_slug",
        "ocr_active",
        "ocr_verified",
        "ocr_quality",
        "title_pages",
        "thumbnail_source",
    }
)
_STRING_FIELDS = frozenset(
    {
        "subtitle",
        "authors",
        "year",
        "publisher",
        "publisher_city",
        "edition",
        "volume",
        "group_id",
        "language",
        "pages",
        "categories",
        "description",
        "pdf_source",
        "source_url",
        "notes",
        "rights",
        "attention",
    }
)
_STATUSES = frozenset({"draft", "ready", "uploaded"})
_RIGHTS = frozenset(
    {
        "",
        "public-domain",
        "cleared",
        "searchable-only",
        "no-public-text",
    }
)
_CATEGORY_ID_RE = re.compile(r"^\w{1,12}$")
_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9-]{0,64}$")
_LANGUAGE_ID_RE = re.compile(r"^[a-z-]{1,12}$")
_RECORD_REVISION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$"
)


class WhlCatalogueItemCodec:
    """Translate immutable item DTOs to and from the legacy WHL JSON row."""

    managed_fields = _MANAGED_FIELDS

    def __init__(
        self,
        *,
        advance_revision: RevisionAdvancer,
        category_ids_for: CategoryIdsLoader,
        validate_representation_manifest: RepresentationManifestValidator,
    ) -> None:
        callbacks = (
            (advance_revision, "advance_revision"),
            (category_ids_for, "category_ids_for"),
            (
                validate_representation_manifest,
                "validate_representation_manifest",
            ),
        )
        for callback, name in callbacks:
            if not callable(callback):
                raise TypeError(f"{name} must be callable")
        self._advance_revision = advance_revision
        self._category_ids_for = category_ids_for
        self._validate_representation_manifest = (
            validate_representation_manifest
        )

    @staticmethod
    def valid_record_revision(value: object) -> bool:
        return isinstance(value, str) and bool(
            _RECORD_REVISION_RE.fullmatch(value)
        )

    @classmethod
    def record_revision(cls, item_id: str, raw: Mapping[str, Any]) -> str:
        """Return the row's explicit revision or its deterministic fallback."""

        updated_at = raw.get("updated_at")
        if cls.valid_record_revision(updated_at):
            assert isinstance(updated_at, str)
            return updated_at
        canonical = json.dumps(
            {"item_id": item_id, "record": raw},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "ir-" + hashlib.sha256(canonical).hexdigest()[:24]

    def validate_bundle(self, value: object) -> None:
        if not isinstance(value, Mapping):
            raise TypeError("bundle must be an object")
        allowed = {"about", "annotations", "pages_text", "translations"}
        if not set(value) <= allowed:
            raise ValueError("bundle contains unknown fields")
        for field in ("about", "annotations", "pages_text"):
            if field in value and not isinstance(value[field], bool):
                raise TypeError(f"bundle.{field} must be a boolean")
        if "translations" in value:
            translations = value["translations"]
            if not isinstance(translations, (list, tuple)):
                raise TypeError("bundle.translations must be an array")
            if (
                any(
                    not isinstance(item, str)
                    or not _LANGUAGE_ID_RE.fullmatch(item)
                    for item in translations
                )
                or len(translations) != len(set(translations))
            ):
                raise ValueError("bundle.translations is invalid")

    def validate_catalogue_metadata(
        self,
        metadata: Mapping[str, Any],
        *,
        strict_fields: Iterable[str] = frozenset(),
    ) -> None:
        """Validate known row fields while retaining unknown JSON extensions."""

        if not isinstance(metadata, Mapping):
            raise TypeError("metadata must be an object")
        strict = frozenset(strict_fields)
        for field in _STRING_FIELDS:
            if field in metadata and not isinstance(metadata[field], str):
                raise TypeError(f"metadata.{field} must be a string")
            if (
                field in metadata
                and field in strict
                and metadata[field] != metadata[field].strip()
            ):
                raise ValueError(
                    f"metadata.{field} must not have outer whitespace"
                )
        if "status" in metadata and metadata["status"] not in _STATUSES:
            raise ValueError("metadata.status is invalid")
        if "rights" in metadata and metadata["rights"] not in _RIGHTS:
            raise ValueError("metadata.rights is invalid")
        if "category_ids" in metadata:
            values = metadata["category_ids"]
            if not isinstance(values, (list, tuple)):
                raise TypeError("metadata.category_ids must be an array")
            if (
                any(
                    not isinstance(value, str)
                    or not _CATEGORY_ID_RE.fullmatch(value)
                    for value in values
                )
                or len(values) != len(set(values))
            ):
                raise ValueError("metadata.category_ids is invalid")
            if "category_ids" in strict:
                known = frozenset(self._category_ids_for())
                if any(value not in known for value in values):
                    raise ValueError(
                        "metadata.category_ids contains unknown ids"
                    )
        if "bundle" in metadata:
            self.validate_bundle(metadata["bundle"])

    def validate_managed_record(
        self,
        item_id: str,
        raw: Mapping[str, Any],
    ) -> None:
        embedded_id = raw.get("id")
        if embedded_id is not None and (
            not isinstance(embedded_id, str) or embedded_id != item_id
        ):
            raise ValueError("the embedded build id conflicts with its key")
        if "item_id" in raw and raw["item_id"] != item_id:
            raise ValueError("the embedded item id conflicts with its key")
        if "kind" in raw and raw["kind"] != "book":
            raise ValueError("the transitional catalogue supports only books")
        if "status" in raw and raw["status"] not in _STATUSES:
            raise ValueError("build status is invalid")
        if "title" in raw and not isinstance(raw["title"], str):
            raise TypeError("build title must be a string")
        if "created_at" in raw and not isinstance(raw["created_at"], str):
            raise TypeError("build created_at must be a string")
        if "updated_at" in raw and not isinstance(raw["updated_at"], str):
            raise TypeError("build updated_at must be a string")
        if "pdf_file" in raw and not isinstance(raw["pdf_file"], str):
            raise TypeError("build pdf_file must be a string")
        for field in _MANAGED_STRING_FIELDS:
            if field in raw and not isinstance(raw[field], str):
                raise TypeError(f"build {field} must be a string")
        if "pdf_sources" in raw:
            sources = raw["pdf_sources"]
            if not isinstance(sources, (list, tuple)):
                raise TypeError("build pdf_sources must be an array")
            for source in sources:
                if not isinstance(source, Mapping):
                    raise TypeError("build pdf_sources must contain objects")
                if not isinstance(source.get("id"), str) or not isinstance(
                    source.get("path"), str
                ):
                    raise TypeError(
                        "build PDF source ids and paths must be strings"
                    )
        if "images" in raw and (
            not isinstance(raw["images"], (list, tuple))
            or any(not isinstance(value, str) for value in raw["images"])
        ):
            raise TypeError("build images must be an array of strings")
        if "extra" in raw and not isinstance(raw["extra"], Mapping):
            raise TypeError("build extra must be an object")
        if "relevance" in raw and not isinstance(raw["relevance"], Mapping):
            raise TypeError("build relevance must be an object")
        if "capture_id" in raw and (
            not isinstance(raw["capture_id"], str)
            or not _CAPTURE_ID_RE.fullmatch(raw["capture_id"])
        ):
            raise ValueError("build capture_id is invalid")
        self._validate_representation_manifest(raw)

    def decode(
        self,
        item_id: str,
        raw: Mapping[str, Any],
    ) -> ItemRecordSnapshot:
        """Decode one legacy row into the catalogue-only item aggregate."""

        if not isinstance(raw, Mapping):
            raise TypeError("a build record must be an object")
        self.validate_managed_record(item_id, raw)
        metadata = {
            key: value
            for key, value in raw.items()
            if key not in self.managed_fields
        }
        self.validate_catalogue_metadata(metadata)
        return ItemRecordSnapshot(
            item_id=item_id,
            revision=self.record_revision(item_id, raw),
            kind="book",
            title=raw.get("title", ""),
            metadata=metadata,
            representations=(),
        )

    def encode(
        self,
        item_id: str,
        draft: ItemDraft,
        previous: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        """Encode a command while retaining server-managed raw state."""

        if not isinstance(draft, ItemDraft):
            raise TypeError("the item draft is invalid")
        if draft.kind != "book" or draft.representations:
            raise ValueError("only catalogue metadata for books is supported")
        managed = sorted(set(draft.metadata) & self.managed_fields)
        if managed:
            raise ValueError("item metadata contains server-managed fields")
        self.validate_catalogue_metadata(draft.metadata)

        if previous is None:
            if draft.title != draft.title.strip():
                raise ValueError("item title must not have outer whitespace")
            self.validate_catalogue_metadata(
                draft.metadata,
                strict_fields=frozenset(draft.metadata),
            )
            now = self._advance_revision("")
            result = {
                "id": item_id,
                "title": draft.title,
                "status": "draft",
                "created_at": now,
                "updated_at": now,
                "published_slug": "",
                "pdf_file": "",
                "pdf_sources": [],
                "ocr_active": "",
                "ocr_verified": "",
                "ocr_quality": "",
                "title_pages": "",
                "thumbnail_source": "",
                "images": [],
                "extra": {},
                "capture_id": "",
                "representation_manifest": {
                    "version": 1,
                    "sources": {},
                    "detached": [],
                },
            }
        else:
            if not isinstance(previous, Mapping):
                raise TypeError("the previous build record is invalid")
            self.validate_managed_record(item_id, previous)
            previous_metadata = {
                key: value
                for key, value in previous.items()
                if key not in self.managed_fields
            }
            if (
                draft.title != previous.get("title", "")
                and draft.title != draft.title.strip()
            ):
                raise ValueError("item title must not have outer whitespace")
            changed = frozenset(
                key
                for key, value in draft.metadata.items()
                if key not in previous_metadata
                or previous_metadata[key] != value
            )
            self.validate_catalogue_metadata(
                draft.metadata,
                strict_fields=changed,
            )
            result = dict(previous)
            for key in tuple(result):
                if key not in self.managed_fields:
                    del result[key]
            result["id"] = item_id
            result["title"] = draft.title
            result["updated_at"] = self._advance_revision(
                str(previous.get("updated_at") or "")
            )
        result.update(dict(draft.metadata))
        return result

    def advance_restored_record(
        self,
        item_id: str,
        raw: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Restore the exact raw row with one fresh catalogue revision."""

        if not isinstance(raw, Mapping):
            raise RepositoryError(
                "the deleted build record is not an object",
                code="invalid_item_restore_record",
            )
        try:
            self.validate_managed_record(item_id, raw)
            before = self.decode(item_id, raw)
            restored = json.loads(
                json.dumps(
                    raw,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
            if not isinstance(restored, dict) or restored != raw:
                raise ValueError("the build record cannot be detached exactly")
        except EngineError:
            raise
        except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
            raise RepositoryError(
                "the deleted build record failed its storage codec",
                code="invalid_item_restore_record",
                details={"cause_type": type(exc).__name__},
            ) from exc

        restored["id"] = item_id
        for _attempt in range(2):
            restored["updated_at"] = self._advance_revision(
                str(restored.get("updated_at") or "")
            )
            try:
                self.validate_managed_record(item_id, restored)
                after = self.decode(item_id, restored)
            except EngineError:
                raise
            except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
                raise RepositoryError(
                    "the restored build record failed its storage codec",
                    code="invalid_item_restore_record",
                    details={"cause_type": type(exc).__name__},
                ) from exc
            if (
                after.revision != before.revision
                and self.valid_record_revision(after.revision)
                and after.revision == restored["updated_at"]
            ):
                return restored
        raise RepositoryError(
            "the restored build record revision could not be advanced",
            code="item_restore_revision_not_advanced",
        )


__all__ = [
    "CategoryIdsLoader",
    "RepresentationManifestValidator",
    "RevisionAdvancer",
    "WhlCatalogueItemCodec",
]
