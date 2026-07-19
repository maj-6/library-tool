"""World Herb Library book rules for the generic item command boundary.

This module deliberately depends only on engine contracts.  Hosts inject the
category vocabulary; Flask request parsing, legacy record codecs, filesystem
layout, and catalogue locking remain adapter concerns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import TypeAlias

from ..engine.errors import RepositoryError, ValidationError
from ..engine.item_commands import ItemDraft, ItemPatch, ItemRecordSnapshot


CategoryIdCataloguePort: TypeAlias = Callable[[], Iterable[str]]

_CATEGORY_ID_RE = re.compile(r"^\w{1,12}$")
_LANGUAGE_ID_RE = re.compile(r"^[a-z-]{1,12}$")
_RIGHTS = frozenset(
    {
        "",
        "public-domain",
        "cleared",
        "searchable-only",
        "no-public-text",
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
        "pdf_file",
        "pdf_sources",
        "images",
        "extra",
    }
)
_BUNDLE_FIELDS = frozenset(
    {"about", "annotations", "pages_text", "translations"}
)


class WhlBookItemCommandPolicy:
    """Validate WHL book candidates without normalizing or persisting them.

    Unknown JSON metadata keys intentionally remain valid extension points.
    The injected category catalogue is consulted only when ``category_ids``
    is written, so an unrelated update does not acquire a taxonomy dependency.
    """

    def __init__(self, category_ids_for: CategoryIdCataloguePort) -> None:
        if not callable(category_ids_for):
            raise TypeError("category_ids_for must be callable")
        self._category_ids_for = category_ids_for

    def validate_create(self, candidate: ItemDraft) -> None:
        self._require_draft(candidate)
        self._validate_kind(candidate)
        if candidate.representations:
            self._representation_mutation()
        self._validate_managed_fields(candidate.metadata)
        self._validate_title(candidate.title)
        self._validate_metadata(
            candidate.metadata,
            strict_fields=frozenset(candidate.metadata),
        )

    def validate_update(
        self,
        current: ItemRecordSnapshot,
        patch: ItemPatch,
        candidate: ItemDraft,
    ) -> None:
        if not isinstance(current, ItemRecordSnapshot):
            raise TypeError("current must be an ItemRecordSnapshot")
        if not isinstance(patch, ItemPatch):
            raise TypeError("patch must be an ItemPatch")
        self._require_draft(candidate)
        self._validate_kind(candidate)
        if patch.representations is not None:
            self._representation_mutation()
        self._validate_managed_fields(
            candidate.metadata,
            patch.metadata_set,
            patch.metadata_remove,
        )
        if patch.title is not None:
            self._validate_title(candidate.title)
        self._validate_metadata(
            candidate.metadata,
            strict_fields=frozenset(patch.metadata_set),
        )

    @staticmethod
    def _require_draft(candidate: ItemDraft) -> None:
        if not isinstance(candidate, ItemDraft):
            raise TypeError("candidate must be an ItemDraft")

    @staticmethod
    def _validate_kind(candidate: ItemDraft) -> None:
        if candidate.kind != "book":
            raise ValidationError(
                "this catalogue supports book items only",
                code="unsupported_item_kind",
                details={"kind": candidate.kind},
            )

    @staticmethod
    def _representation_mutation() -> None:
        raise ValidationError(
            "representation attachment is a separate operation",
            code="representation_mutation_not_supported",
        )

    @staticmethod
    def _validate_managed_fields(*values: Iterable[str]) -> None:
        fields = sorted(
            {
                key
                for value in values
                for key in value
                if key in _MANAGED_FIELDS
            }
        )
        if fields:
            raise ValidationError(
                "server-managed item fields cannot be changed here",
                code="managed_item_fields_not_writable",
                details={"fields": fields},
            )

    @classmethod
    def _validate_title(cls, title: str) -> None:
        if title != title.strip():
            cls._invalid_metadata("title", "outer_whitespace")

    def _validate_metadata(
        self,
        metadata: Mapping[str, object],
        *,
        strict_fields: frozenset[str],
    ) -> None:
        for field in _STRING_FIELDS:
            if field not in metadata:
                continue
            value = metadata[field]
            if not isinstance(value, str):
                self._invalid_metadata(field, "string_required")
            if field in strict_fields and value != value.strip():
                self._invalid_metadata(field, "outer_whitespace")

        rights = metadata.get("rights")
        if rights is not None and rights not in _RIGHTS:
            self._invalid_metadata("rights", "invalid_value")

        if "category_ids" in metadata:
            self._validate_category_ids(
                metadata["category_ids"],
                verify_membership="category_ids" in strict_fields,
            )
        if "bundle" in metadata:
            self._validate_bundle(metadata["bundle"])

    def _validate_category_ids(
        self,
        value: object,
        *,
        verify_membership: bool,
    ) -> None:
        if not isinstance(value, (list, tuple)):
            self._invalid_metadata("category_ids", "array_required")
        assert isinstance(value, (list, tuple))
        if (
            any(
                not isinstance(item, str)
                or not _CATEGORY_ID_RE.fullmatch(item)
                for item in value
            )
            or len(value) != len(set(value))
        ):
            self._invalid_metadata("category_ids", "invalid_value")
        if not verify_membership:
            return

        known = self._load_category_ids()
        unknown = sorted(item for item in value if item not in known)
        if unknown:
            self._invalid_metadata(
                "category_ids",
                "unknown_ids",
                values=unknown,
            )

    def _load_category_ids(self) -> frozenset[str]:
        try:
            raw = self._category_ids_for()
            if isinstance(raw, (str, bytes)):
                raise TypeError("category catalogue must be an iterable")
            values = frozenset(raw)
            if any(not isinstance(value, str) for value in values):
                raise TypeError("category ids must be strings")
        except RepositoryError:
            # A host adapter may already have translated a legacy taxonomy
            # failure into the engine's sanitized repository contract.
            raise
        except Exception as exc:
            raise RepositoryError(
                "the category catalogue is unavailable",
                code="category_repository_unavailable",
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc
        return values

    @classmethod
    def _validate_bundle(cls, value: object) -> None:
        if not isinstance(value, Mapping):
            cls._invalid_metadata("bundle", "object_required")
        assert isinstance(value, Mapping)
        unknown = sorted(set(value) - _BUNDLE_FIELDS)
        if unknown:
            cls._invalid_metadata(
                "bundle",
                "unknown_fields",
                values=unknown,
            )
        for field in ("about", "annotations", "pages_text"):
            if field in value and not isinstance(value[field], bool):
                cls._invalid_metadata(
                    f"bundle.{field}",
                    "boolean_required",
                )
        if "translations" not in value:
            return
        translations = value["translations"]
        if not isinstance(translations, (list, tuple)):
            cls._invalid_metadata(
                "bundle.translations",
                "array_required",
            )
        assert isinstance(translations, (list, tuple))
        if (
            any(
                not isinstance(item, str)
                or not _LANGUAGE_ID_RE.fullmatch(item)
                for item in translations
            )
            or len(translations) != len(set(translations))
        ):
            cls._invalid_metadata(
                "bundle.translations",
                "invalid_value",
            )

    @staticmethod
    def _invalid_metadata(
        field: str,
        reason: str,
        *,
        values: list[str] | None = None,
    ) -> None:
        details: dict[str, object] = {"field": field, "reason": reason}
        if values is not None:
            details["values"] = values
        raise ValidationError(
            "the item metadata is invalid",
            code="invalid_item_metadata",
            details=details,
        )


__all__ = [
    "CategoryIdCataloguePort",
    "WhlBookItemCommandPolicy",
]
