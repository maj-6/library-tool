"""Provider-neutral, page-aligned translation application services."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import ItemDescriptor
from .translation_contracts import (
    ReplaceTranslationPageCommand,
    TranslationAggregate,
    TranslationDocumentView,
    TranslationPageRecord,
    TranslationPageState,
    TranslationPageView,
    TranslationSourceCanvas,
    TranslationSourceRef,
    TranslationSourceSnapshot,
    TranslationStatus,
    TranslationSummaryView,
)
from .errors import ConflictError, NotFoundError, RepositoryError, ValidationError
from .ports import (
    ItemRepositoryPort,
    TranslationPolicyPort,
    TranslationReadSessionPort,
    TranslationRepositoryPort,
)


_PORTABLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


@dataclass(frozen=True, slots=True)
class _LegacyTranslationStatus:
    """Integer-page result retained for the legacy provenance consumer."""

    stale: tuple[int, ...] = ()
    untracked: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()
    orphaned: tuple[int, ...] = ()


class TranslationProvenanceService:
    """Pure provenance/status rules for legacy page-number translations.

    The legacy text-plus-metadata adapter consumes these rules. They remain
    separate from the selector-based aggregate so existing server behavior can
    migrate without weakening the new engine boundary.
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
        return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()

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
                    legacy
                    and legacy != self.legacy_source_hash(source_pages[page])
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
    ) -> _LegacyTranslationStatus:
        source_numbers = {int(page) for page in source_pages}
        translated_numbers = {int(page) for page in translated_pages}
        tracked = set(self.tracked_pages(metadata))
        return _LegacyTranslationStatus(
            stale=self.stale_pages(metadata, source_pages, source_layer),
            untracked=tuple(sorted(translated_numbers - tracked)),
            missing=tuple(
                sorted(
                    page
                    for page, text in source_pages.items()
                    if str(text or "").strip()
                    and not str(translated_pages.get(page, "") or "").strip()
                )
            ),
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


class CanonicalTranslationPolicy:
    """Deterministic default language and revision policy."""

    def normalize_language(self, value: str) -> str:
        if not isinstance(value, str):
            return ""
        raw = value.strip()
        if not _LANGUAGE_TAG_RE.fullmatch(raw):
            return ""
        parts = raw.split("-")
        normalized = [parts[0].lower()]
        for part in parts[1:]:
            if len(part) == 4 and part.isalpha():
                normalized.append(part.title())
            elif len(part) == 2 and part.isalpha():
                normalized.append(part.upper())
            else:
                normalized.append(part.lower())
        return "-".join(normalized)

    def revision(self, value: Any, prefix: str) -> str:
        if not _PORTABLE_ID_RE.fullmatch(prefix):
            raise ValueError("revision prefix must be a portable identifier")
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return f"{prefix}-" + hashlib.sha256(raw).hexdigest()


class TranslationService:
    """Manage translation aggregates against authoritative source snapshots."""

    def __init__(
        self,
        items: ItemRepositoryPort,
        repository: TranslationRepositoryPort,
        policies: TranslationPolicyPort | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._items = items
        self._repository = repository
        self._policies = policies or CanonicalTranslationPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def list(self, item_id: str) -> tuple[TranslationSummaryView, ...]:
        item = self._require_item(item_id)
        with self._repository.snapshot(item.item_id) as snapshot:
            raw = snapshot.list(item.item_id)
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise RepositoryError(
                    "the translation repository returned an invalid collection",
                    code="invalid_translation_collection",
                    details={"item_id": item.item_id},
                )
            aggregates = tuple(
                self._validate_aggregate(value, item_id=item.item_id)
                for value in raw
            )
            identifiers = [value.translation_id for value in aggregates]
            if len(identifiers) != len(set(identifiers)):
                raise RepositoryError(
                    "the translation repository returned duplicate identifiers",
                    code="duplicate_translation_id",
                    details={"item_id": item.item_id},
                )
            source_cache: dict[str, TranslationSourceSnapshot | None] = {}
            summaries = []
            for aggregate in aggregates:
                if aggregate.source_layer_id not in source_cache:
                    source_cache[aggregate.source_layer_id] = self._load_source(
                        snapshot, aggregate, required=False
                    )
                view = self._view(
                    aggregate, source_cache[aggregate.source_layer_id]
                )
                summaries.append(self._summary(view))
        return tuple(
            sorted(
                summaries,
                key=lambda value: (value.target_language, value.translation_id),
            )
        )

    def get(
        self, item_id: str, translation_id: str
    ) -> TranslationDocumentView:
        item = self._require_item(item_id)
        identifier = self._identifier(translation_id, "translation_id")
        with self._repository.snapshot(item.item_id) as snapshot:
            raw = snapshot.load(item.item_id, identifier)
            if raw is None:
                raise NotFoundError(
                    "the translation does not exist",
                    code="translation_not_found",
                    details={
                        "item_id": item.item_id,
                        "translation_id": identifier,
                    },
                )
            aggregate = self._validate_aggregate(
                raw, item_id=item.item_id, translation_id=identifier
            )
            return self._view(
                aggregate,
                self._load_source(snapshot, aggregate, required=False),
            )

    def replace_page(
        self, command: ReplaceTranslationPageCommand
    ) -> TranslationDocumentView:
        if not isinstance(command, ReplaceTranslationPageCommand):
            raise ValidationError(
                "replace_page requires a ReplaceTranslationPageCommand",
                code="invalid_translation_command",
            )
        item = self._require_item(command.item_id)
        with self._repository.unit_of_work(item.item_id) as unit_of_work:
            raw = unit_of_work.load(item.item_id, command.translation_id)
            if raw is None:
                raise NotFoundError(
                    "the translation does not exist",
                    code="translation_not_found",
                    details={
                        "item_id": item.item_id,
                        "translation_id": command.translation_id,
                    },
                )
            aggregate = self._validate_aggregate(
                raw,
                item_id=item.item_id,
                translation_id=command.translation_id,
            )
            document_revision = self._document_revision(aggregate)
            if command.expected_document_revision != document_revision:
                self._raise_document_conflict(
                    aggregate,
                    command.expected_document_revision,
                    document_revision,
                )

            source = self._load_source(unit_of_work, aggregate, required=True)
            assert source is not None
            source_ref = self._source_ref(aggregate, source)
            if command.expected_source_revision != source_ref.revision:
                self._raise_source_conflict(
                    aggregate,
                    command.expected_source_revision,
                    source_ref.revision,
                )
            canvases = {value.selector: value for value in source.canvases}
            canvas = canvases.get(command.selector)
            if canvas is None:
                raise ValidationError(
                    "the selector does not exist in the source snapshot",
                    code="translation_selector_not_found",
                    details={
                        "translation_id": aggregate.translation_id,
                        "selector": command.selector,
                        "source_revision": source_ref.revision,
                    },
                )

            pages = {value.selector: value for value in aggregate.pages}
            if command.text.strip():
                pages[command.selector] = TranslationPageRecord(
                    selector=command.selector,
                    text=command.text,
                    source_revision=self._canvas_revision(source, canvas),
                    source_layer_id=source.layer_id,
                    origin="human",
                    review_state="reviewed",
                    updated_at=self._timestamp(),
                )
            else:
                pages.pop(command.selector, None)
            updated = TranslationAggregate(
                translation_id=aggregate.translation_id,
                item_id=aggregate.item_id,
                target_language=aggregate.target_language,
                source_layer_id=aggregate.source_layer_id,
                pages=tuple(pages.values()),
            )
            try:
                unit_of_work.compare_and_save(
                    updated,
                    expected_document_revision=document_revision,
                    expected_source_revision=source_ref.revision,
                )
            except ConflictError as exc:
                details = dict(exc.details)
                conflict_kind = str(details.get("conflict_kind") or "")
                if not conflict_kind and exc.code == (
                    "stale_translation_source_revision"
                ):
                    conflict_kind = "source"
                if conflict_kind == "source":
                    current = str(details.get("current_source_revision") or "")
                    self._raise_source_conflict(
                        aggregate, source_ref.revision, current, details=details
                    )
                current = str(details.get("current_document_revision") or "")
                self._raise_document_conflict(
                    aggregate, document_revision, current, details=details
                )
            return self._view(updated, source)

    def document_revision(self, aggregate: TranslationAggregate) -> str:
        """Return the stable repository CAS revision for an aggregate."""

        if not isinstance(aggregate, TranslationAggregate):
            raise TypeError("aggregate must be a TranslationAggregate")
        return self._document_revision(aggregate)

    def source_revision(self, source: TranslationSourceSnapshot) -> str:
        """Return the stable source precondition revision for a snapshot."""

        if not isinstance(source, TranslationSourceSnapshot):
            raise TypeError("source must be a TranslationSourceSnapshot")
        return self._source_snapshot_revision(source)

    def _view(
        self,
        aggregate: TranslationAggregate,
        source: TranslationSourceSnapshot | None,
    ) -> TranslationDocumentView:
        source_ref = self._source_ref(aggregate, source)
        records = {value.selector: value for value in aggregate.pages}
        page_views: list[TranslationPageView] = []
        groups: dict[str, list[str]] = {
            "current": [],
            "stale": [],
            "untracked": [],
            "missing": [],
            "orphaned": [],
        }
        source_selectors: set[str] = set()
        for canvas in source.canvases if source is not None else ():
            source_selectors.add(canvas.selector)
            record = records.get(canvas.selector)
            state = self._page_state(aggregate, source, canvas, record)
            groups[state].append(canvas.selector)
            page_views.append(
                self._page_view(
                    canvas.selector,
                    state,
                    record,
                    order=canvas.order,
                    label=canvas.label,
                    source_text=canvas.text,
                )
            )
        for selector in sorted(set(records) - source_selectors):
            record = records[selector]
            groups["orphaned"].append(selector)
            page_views.append(
                self._page_view(
                    selector,
                    "orphaned",
                    record,
                    order=None,
                    label="",
                    source_text="",
                )
            )
        status = TranslationStatus(**{key: tuple(value) for key, value in groups.items()})
        document_revision = self._document_revision(aggregate)
        view_revision = self._policies.revision(
            {
                "document_revision": document_revision,
                "source": source_ref.as_dict(),
                "status": status.as_dict(),
            },
            "tv",
        )
        return TranslationDocumentView(
            translation_id=aggregate.translation_id,
            item_id=aggregate.item_id,
            target_language=aggregate.target_language,
            document_revision=document_revision,
            view_revision=view_revision,
            source=source_ref,
            pages=tuple(page_views),
            status=status,
        )

    def _page_state(
        self,
        aggregate: TranslationAggregate,
        source: TranslationSourceSnapshot,
        canvas: TranslationSourceCanvas,
        record: TranslationPageRecord | None,
    ) -> TranslationPageState:
        """Return exactly one exhaustive state for a source/page pair."""

        if record is None or not record.text.strip():
            return "missing" if canvas.text.strip() else "current"
        if not record.source_revision or not record.source_layer_id:
            return "untracked"
        if record.source_layer_id != aggregate.source_layer_id:
            return "stale"
        if record.source_revision != self._canvas_revision(source, canvas):
            return "stale"
        return "current"

    @staticmethod
    def _page_view(
        selector: str,
        state: TranslationPageState,
        record: TranslationPageRecord | None,
        *,
        order: int | None,
        label: str,
        source_text: str,
    ) -> TranslationPageView:
        if record is None:
            return TranslationPageView(
                selector=selector,
                order=order,
                label=label,
                source_text=source_text,
                text="",
                state=state,
            )
        return TranslationPageView(
            selector=selector,
            order=order,
            label=label,
            source_text=source_text,
            text=record.text,
            state=state,
            source_revision=record.source_revision,
            source_layer_id=record.source_layer_id,
            origin=record.origin,
            review_state=record.review_state,
            provider_id=record.provider_id,
            model=record.model,
            recipe_revision=record.recipe_revision,
            updated_at=record.updated_at,
        )

    def _load_source(
        self,
        snapshot: TranslationReadSessionPort,
        aggregate: TranslationAggregate,
        *,
        required: bool,
    ) -> TranslationSourceSnapshot | None:
        raw = snapshot.load_source(
            aggregate.item_id, aggregate.source_layer_id
        )
        if raw is None:
            if required:
                raise NotFoundError(
                    "the translation source is not available",
                    code="translation_source_not_found",
                    details={
                        "item_id": aggregate.item_id,
                        "translation_id": aggregate.translation_id,
                        "source_layer_id": aggregate.source_layer_id,
                    },
                )
            return None
        if not isinstance(raw, TranslationSourceSnapshot):
            raise RepositoryError(
                "the source repository returned an invalid snapshot",
                code="invalid_translation_source_snapshot",
                details={"item_id": aggregate.item_id},
            )
        if (
            raw.item_id != aggregate.item_id
            or raw.layer_id != aggregate.source_layer_id
        ):
            raise RepositoryError(
                "the source snapshot identity does not match the translation",
                code="translation_source_identity_mismatch",
                details={
                    "translation_id": aggregate.translation_id,
                    "expected_item_id": aggregate.item_id,
                    "actual_item_id": raw.item_id,
                    "expected_layer_id": aggregate.source_layer_id,
                    "actual_layer_id": raw.layer_id,
                },
            )
        return raw

    def _source_ref(
        self,
        aggregate: TranslationAggregate,
        source: TranslationSourceSnapshot | None,
    ) -> TranslationSourceRef:
        if source is None:
            return TranslationSourceRef(
                layer_id=aggregate.source_layer_id,
                representation_id="",
                revision=self._policies.revision(
                    {
                        "available": False,
                        "item_id": aggregate.item_id,
                        "layer_id": aggregate.source_layer_id,
                    },
                    "ts",
                ),
                available=False,
            )
        return TranslationSourceRef(
            layer_id=source.layer_id,
            representation_id=source.representation_id,
            revision=self._source_snapshot_revision(source),
        )

    def _source_snapshot_revision(self, source: TranslationSourceSnapshot) -> str:
        return self._policies.revision(source.as_dict(), "ts")

    def _canvas_revision(
        self, source: TranslationSourceSnapshot, canvas: TranslationSourceCanvas
    ) -> str:
        return self._policies.revision(
            {
                "item_id": source.item_id,
                "layer_id": source.layer_id,
                "representation_id": source.representation_id,
                "selector": canvas.selector,
                "text": canvas.text,
            },
            "tc",
        )

    def _document_revision(self, aggregate: TranslationAggregate) -> str:
        return self._policies.revision(aggregate.as_dict(), "tr")

    @staticmethod
    def _summary(view: TranslationDocumentView) -> TranslationSummaryView:
        return TranslationSummaryView(
            translation_id=view.translation_id,
            item_id=view.item_id,
            target_language=view.target_language,
            document_revision=view.document_revision,
            view_revision=view.view_revision,
            source=view.source,
            page_count=sum(bool(value.text.strip()) for value in view.pages),
            status=view.status,
        )

    def _validate_aggregate(
        self,
        raw: object,
        *,
        item_id: str,
        translation_id: str | None = None,
    ) -> TranslationAggregate:
        if not isinstance(raw, TranslationAggregate):
            raise RepositoryError(
                "the translation repository returned an invalid aggregate",
                code="invalid_translation_aggregate",
                details={"item_id": item_id},
            )
        if raw.item_id != item_id or (
            translation_id is not None and raw.translation_id != translation_id
        ):
            raise RepositoryError(
                "the translation aggregate identity does not match the request",
                code="translation_identity_mismatch",
                details={
                    "expected_item_id": item_id,
                    "actual_item_id": raw.item_id,
                    "expected_translation_id": translation_id or "",
                    "actual_translation_id": raw.translation_id,
                },
            )
        language = self._policies.normalize_language(raw.target_language)
        if not language or language != raw.target_language:
            raise RepositoryError(
                "the translation repository returned a non-canonical language",
                code="invalid_translation_language",
                details={
                    "translation_id": raw.translation_id,
                    "target_language": raw.target_language,
                },
            )
        return raw

    def _require_item(self, item_id: str) -> ItemDescriptor:
        value = self._identifier(item_id, "item_id")
        item = self._items.get(value)
        if item is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": value},
            )
        if not isinstance(item, ItemDescriptor) or item.item_id != value:
            raise RepositoryError(
                "the item repository returned an invalid descriptor",
                code="invalid_item_descriptor",
                details={"item_id": value},
            )
        return item

    @staticmethod
    def _identifier(value: str, name: str) -> str:
        if not isinstance(value, str) or not _PORTABLE_ID_RE.fullmatch(value):
            raise ValidationError(
                f"{name} must be a portable identifier",
                code=f"invalid_{name}",
                details={name: value if isinstance(value, str) else ""},
            )
        return value

    @staticmethod
    def _raise_document_conflict(
        aggregate: TranslationAggregate,
        expected: str,
        current: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        merged.update(
            {
                "item_id": aggregate.item_id,
                "translation_id": aggregate.translation_id,
                "expected_document_revision": expected,
                "current_document_revision": current,
            }
        )
        raise ConflictError(
            "the translation document changed; reload it before saving",
            code="stale_translation_document_revision",
            details=merged,
            retryable=True,
        )

    @staticmethod
    def _raise_source_conflict(
        aggregate: TranslationAggregate,
        expected: str,
        current: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        merged.update(
            {
                "item_id": aggregate.item_id,
                "translation_id": aggregate.translation_id,
                "expected_source_revision": expected,
                "current_source_revision": current,
            }
        )
        raise ConflictError(
            "the translation source changed; reload it before saving",
            code="stale_translation_source_revision",
            details=merged,
            retryable=True,
        )

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CanonicalTranslationPolicy",
    "TranslationProvenanceService",
    "TranslationService",
]
