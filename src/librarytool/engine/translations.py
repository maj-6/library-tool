"""Page-aligned translation application service."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import (
    TranslationDocumentView,
    TranslationPageCommand,
    TranslationStatus,
)
from .errors import (
    ConflictError,
    NotFoundError,
    PreconditionRequiredError,
    ValidationError,
)
from .ports import ItemRepositoryPort, ReplicaPolicyPort, TranslationRepositoryPort


class TranslationProvenanceService:
    """Pure provenance/status rules for page-aligned translations.

    The legacy text-plus-metadata adapter and the newer revisioned translation
    repository both consume these rules. Keeping them independent of either
    storage shape lets an alternative client or backend make exactly the same
    stale/current decision.
    """

    @staticmethod
    def legacy_source_hash(text: str) -> str:
        normalized = re.sub(r"\s+", " ", str(text or "").strip())
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def source_hash(text: str) -> str:
        raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [
            re.sub(r"\s+", " ", part).strip()
            for part in re.split(r"\n[ \t]*\n+", raw)
        ]
        normalized = "\n\n".join(part for part in paragraphs if part)
        return "sha256:" + hashlib.sha256(
            normalized.encode("utf-8")
        ).hexdigest()

    def stale_pages(
        self,
        metadata: Mapping[str, Any],
        source_pages: Mapping[int, str],
        source_layer: str = "",
    ) -> tuple[int, ...]:
        stale: list[int] = []
        source_changed = bool(
            source_layer
            and metadata.get("src")
            and str(metadata.get("src")) != source_layer
        )
        pages = metadata.get("pages")
        for key, record in (pages.items() if isinstance(pages, Mapping) else ()):
            try:
                page = int(key)
            except (TypeError, ValueError):
                continue
            if page not in source_pages or not isinstance(record, Mapping):
                continue
            current_hash = record.get("source_hash")
            if current_hash:
                mismatched = current_hash != self.source_hash(source_pages[page])
            else:
                legacy = record.get("sha1")
                mismatched = bool(
                    legacy and legacy != self.legacy_source_hash(source_pages[page])
                )
            if source_changed or mismatched:
                stale.append(page)
        return tuple(sorted(stale))

    @staticmethod
    def tracked_pages(metadata: Mapping[str, Any]) -> tuple[int, ...]:
        pages = metadata.get("pages")
        tracked = []
        for key, record in (pages.items() if isinstance(pages, Mapping) else ()):
            if not str(key).isdigit() or not isinstance(record, Mapping):
                continue
            if record.get("source_hash") or record.get("sha1"):
                tracked.append(int(key))
        return tuple(sorted(set(tracked)))

    def status(
        self,
        metadata: Mapping[str, Any],
        source_pages: Mapping[int, str],
        translated_pages: Mapping[int, str],
        *,
        source_layer: str = "",
    ) -> TranslationStatus:
        source_numbers = {int(page) for page in source_pages}
        translated_numbers = {int(page) for page in translated_pages}
        tracked = set(self.tracked_pages(metadata))
        return TranslationStatus(
            stale=self.stale_pages(metadata, source_pages, source_layer),
            untracked=tuple(sorted(translated_numbers - tracked)),
            missing=tuple(sorted(
                page for page, text in source_pages.items()
                if str(text or "").strip()
                and not str(translated_pages.get(page, "") or "").strip()
            )),
            orphaned=tuple(sorted(translated_numbers - source_numbers)),
        )

    def page_record(
        self,
        source_text: str,
        *,
        source_layer: str,
        model: str,
        at: str,
    ) -> dict[str, str]:
        return {
            "source_hash": self.source_hash(source_text),
            "sha1": self.legacy_source_hash(source_text),
            "src": str(source_layer or ""),
            "model": str(model or ""),
            "at": str(at or ""),
        }


class TranslationService:
    """Manage translated pages with source provenance and optimistic CAS."""

    def __init__(
        self,
        items: ItemRepositoryPort,
        repository: TranslationRepositoryPort,
        policies: ReplicaPolicyPort,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._items = items
        self._repository = repository
        self._policies = policies
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def get(self, item_id: str, language: str) -> TranslationDocumentView:
        item = self._require_item(item_id)
        lang = self._language(language)
        document = self._normalize(self._repository.load(item.item_id, lang))
        return self._view(item.item_id, lang, document)

    def replace_page(
        self, command: TranslationPageCommand
    ) -> TranslationDocumentView:
        item = self._require_item(command.item_id)
        lang = self._language(command.language)
        if not isinstance(command.page, int) or isinstance(command.page, bool) \
                or command.page < 1:
            raise ValidationError(
                "page must be a positive integer",
                code="invalid_page",
                details={"page": command.page},
            )
        expected = str(command.expected_revision or "").strip()
        if not expected:
            raise PreconditionRequiredError(
                "a translation revision is required",
                code="translation_revision_required",
                details={"item_id": item.item_id, "language": lang},
            )
        current = self._normalize(self._repository.load(item.item_id, lang))
        current_revision = self._revision(current)
        if expected != current_revision:
            raise ConflictError(
                "the translation changed; reload it before saving",
                code="stale_translation_revision",
                details={
                    "item_id": item.item_id,
                    "language": lang,
                    "expected_revision": expected,
                    "current_revision": current_revision,
                },
                retryable=True,
            )
        pages = current["pages"]
        text = str(command.text or "")
        if text.strip():
            source_text = str(command.source_text or "")
            record: dict[str, Any] = {
                "text": text,
                "source_revision": (
                    self._policies.content_revision(source_text, "ts")
                    if source_text
                    else ""
                ),
                "source_layer": str(command.source_layer or ""),
                "model": str(command.model or ""),
                "updated_at": self._timestamp(),
            }
            pages[str(command.page)] = record
        else:
            pages.pop(str(command.page), None)
        document = {"version": 1, "pages": pages}
        document["revision"] = self._revision(document)
        self._repository.compare_and_save(
            item.item_id,
            lang,
            copy.deepcopy(document),
            expected_revision=current_revision,
        )
        return self._view(item.item_id, lang, document)

    def status(
        self,
        item_id: str,
        language: str,
        source_pages: Mapping[int, str],
        *,
        source_layer: str = "",
    ) -> TranslationStatus:
        view = self.get(item_id, language)
        pages = view.pages
        source_numbers = {int(page) for page in source_pages}
        translated_numbers = {
            int(page) for page in pages if str(page).isdigit()
        }
        stale: list[int] = []
        untracked: list[int] = []
        for page in sorted(source_numbers & translated_numbers):
            record = pages.get(str(page))
            if not isinstance(record, Mapping):
                untracked.append(page)
                continue
            tracked = str(record.get("source_revision") or "")
            if not tracked:
                untracked.append(page)
                continue
            current = self._policies.content_revision(
                str(source_pages[page] or ""), "ts"
            )
            if tracked != current or (
                source_layer
                and record.get("source_layer")
                and str(record.get("source_layer")) != source_layer
            ):
                stale.append(page)
        return TranslationStatus(
            stale=tuple(stale),
            untracked=tuple(untracked),
            missing=tuple(sorted(source_numbers - translated_numbers)),
            orphaned=tuple(sorted(translated_numbers - source_numbers)),
        )

    def _view(
        self, item_id: str, language: str, document: Mapping[str, Any]
    ) -> TranslationDocumentView:
        return TranslationDocumentView(
            item_id=item_id,
            language=language,
            revision=self._revision(document),
            pages=copy.deepcopy(document.get("pages") or {}),
        )

    def _revision(self, document: Mapping[str, Any]) -> str:
        value = {key: val for key, val in document.items() if key != "revision"}
        return self._policies.content_revision(value, "tr")

    @staticmethod
    def _normalize(raw: Mapping[str, Any] | None) -> dict[str, Any]:
        pages = raw.get("pages") if isinstance(raw, Mapping) else None
        clean_pages = {
            str(page): copy.deepcopy(record)
            for page, record in (pages or {}).items()
            if str(page).isdigit() and isinstance(record, Mapping)
        }
        return {"version": 1, "pages": clean_pages}

    def _require_item(self, item_id: str):
        value = str(item_id or "").strip()
        item = self._items.get(value) if value else None
        if item is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": value},
            )
        return item

    def _language(self, language: str) -> str:
        value = self._policies.normalize_language(str(language or ""))
        if not value:
            raise ValidationError(
                "the translation language is invalid",
                code="invalid_language",
                details={"language": str(language or "")},
            )
        return value

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
