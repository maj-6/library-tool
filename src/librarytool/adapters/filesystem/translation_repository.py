"""Strict, recoverable storage for page-aligned translation aggregates.

The current application stores translations as page-marked UTF-8 text plus a
JSON provenance sidecar.  This adapter keeps that format as an implementation
detail while exposing the provider-neutral translation repository port.  It
also supplies the transaction boundary the legacy format never had: text,
metadata, and the artifact manifest are published through one recoverable
write set.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager

from ...engine.errors import ConflictError, NotFoundError, RepositoryError
from ...engine.ports import TranslationPolicyPort
from ...engine.translation_contracts import (
    TranslationAggregate,
    TranslationPageRecord,
    TranslationSourceCanvas,
    TranslationSourceSnapshot,
)
from ...engine.translations import (
    CanonicalTranslationPolicy,
    TranslationProvenanceService,
)
from .recoverable_write_set import RecoverableWriteSet


_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_STORAGE_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
_PAGE_MARKER = re.compile(r"^--- page ([0-9]+) ---\r?$", re.MULTILINE)
_SELECTOR = re.compile(r"^page:([1-9][0-9]*)$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SOURCE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")

SourceSnapshotLoader = Callable[
    [str, str], TranslationSourceSnapshot | None
]
SourceReference = Callable[[TranslationSourceSnapshot], str]


def translation_id_for_language(language: str) -> str:
    """Return the stable storage-neutral identifier for a language layer."""

    if not isinstance(language, str) or not _LANGUAGE_TAG.fullmatch(language):
        raise ValueError("language must be a valid canonical language tag")
    try:
        payload = language.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("language must be valid Unicode") from exc
    return "translation-" + hashlib.sha256(payload).hexdigest()[:32]


def _fallback_layer_id(reference: str) -> str:
    payload = reference.encode("utf-8")
    return "source-" + hashlib.sha256(payload).hexdigest()[:32]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key {key!r}")
        value[key] = item
    return value


def _strict_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return deepcopy(default)
    try:
        payload = path.read_bytes().decode("utf-8")
        return json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RepositoryError(
            "a translation storage artifact cannot be read",
            code="invalid_translation_storage",
            details={"path": str(path)},
        ) from exc


def _strict_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8") if path.is_file() else ""
    except (OSError, UnicodeError) as exc:
        raise RepositoryError(
            "a translation storage artifact cannot be read",
            code="invalid_translation_storage",
            details={"path": str(path)},
        ) from exc


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RepositoryError(
            "translation metadata cannot be serialized",
            code="invalid_translation_aggregate",
        ) from exc


def _text_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise RepositoryError(
            "translation text cannot be serialized",
            code="invalid_translation_aggregate",
        ) from exc


def _parse_pages(text: str, *, path: Path) -> dict[int, str]:
    marks = list(_PAGE_MARKER.finditer(text))
    if not marks:
        stripped = text.strip()
        return {1: stripped} if stripped else {}
    if text[: marks[0].start()].strip():
        raise RepositoryError(
            "a translation document has text before its first page marker",
            code="invalid_translation_storage",
            details={"path": str(path)},
        )
    pages: dict[int, str] = {}
    for index, mark in enumerate(marks):
        raw_page = mark.group(1)
        page = int(raw_page)
        if page < 1 or str(page) != raw_page or page in pages:
            raise RepositoryError(
                "a translation document has ambiguous page markers",
                code="invalid_translation_storage",
                details={"path": str(path), "page": raw_page},
            )
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        pages[page] = text[mark.end() : end].strip()
    return pages


def _render_pages(pages: Mapping[int, str]) -> str:
    if not pages:
        return ""
    return (
        "\n\n".join(
            f"--- page {page} ---\n{pages[page]}" for page in sorted(pages)
        )
        + "\n"
    )


def _selector(page: int) -> str:
    return f"page:{page}"


def _page(selector: str) -> int:
    match = _SELECTOR.fullmatch(selector)
    if match is None:
        raise RepositoryError(
            "the filesystem translation adapter requires page selectors",
            code="unsupported_translation_selector",
            details={"selector": selector},
        )
    return int(match.group(1))


def _string_field(
    value: Any,
    name: str,
    *,
    path: Path,
    optional: bool = True,
) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise RepositoryError(
            f"translation metadata {name} is not text",
            code="invalid_translation_storage",
            details={"path": str(path), "field": name},
        )
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value) or any(
        0xD800 <= ord(char) <= 0xDFFF for char in value
    ):
        raise RepositoryError(
            f"translation metadata {name} is not safe text",
            code="invalid_translation_storage",
            details={"path": str(path), "field": name},
        )
    return value


class FilesystemTranslationRepository:
    """Open coherent translation/source sessions over legacy entry folders.

    The workspace lease is always acquired before ``lock_context_for``.  The
    latter should bridge the still-active legacy page/OCR/analyze/manifest
    locks in their documented order.  ``source_snapshot_for`` runs inside
    both lock domains; its second argument is the legacy metadata ``src``
    reference (an empty string asks for the current authoritative source).
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        entry_directory_for: Callable[[str], Path],
        item_exists_for: Callable[[str], bool],
        source_snapshot_for: SourceSnapshotLoader,
        policies: TranslationPolicyPort | None = None,
        source_reference_for: SourceReference | None = None,
        lock_context_for: Callable[[str], ContextManager[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        recover: bool = True,
    ) -> None:
        self._write_set = write_set
        self._entry_directory_for = entry_directory_for
        self._item_exists_for = item_exists_for
        self._source_snapshot_for = source_snapshot_for
        self._policies = policies or CanonicalTranslationPolicy()
        self._source_reference_for = source_reference_for or (
            lambda source: source.layer_id
        )
        self._lock_context_for = lock_context_for or (
            lambda _item_id: nullcontext()
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if recover:
            self._write_set.recover_all()

    @contextmanager
    def snapshot(self, item_id: str) -> Iterator["FilesystemTranslationSession"]:
        with self._session(item_id, writable=False) as session:
            yield session

    @contextmanager
    def unit_of_work(
        self, item_id: str
    ) -> Iterator["FilesystemTranslationSession"]:
        with self._session(item_id, writable=True) as session:
            yield session

    @contextmanager
    def _session(
        self, item_id: str, *, writable: bool
    ) -> Iterator["FilesystemTranslationSession"]:
        identifier = self._item_id(item_id)
        with self._write_set.workspace_lease():
            with self._lock_context_for(identifier):
                try:
                    exists = self._item_exists_for(identifier)
                except Exception as exc:
                    raise RepositoryError(
                        "the item repository could not be read",
                        code="translation_item_read_failed",
                        details={"item_id": identifier},
                    ) from exc
                if not isinstance(exists, bool):
                    raise RepositoryError(
                        "the item repository returned an invalid existence result",
                        code="invalid_translation_item_identity",
                        details={"item_id": identifier},
                    )
                if not exists:
                    raise NotFoundError(
                        "the item does not exist",
                        code="item_not_found",
                        details={"item_id": identifier},
                    )
                entry, relative = self._entry_path(identifier)
                yield FilesystemTranslationSession(
                    self._write_set,
                    item_id=identifier,
                    entry_directory=entry,
                    entry_relative=relative,
                    source_snapshot_for=self._source_snapshot_for,
                    source_reference_for=self._source_reference_for,
                    policies=self._policies,
                    clock=self._clock,
                    writable=writable,
                )

    @staticmethod
    def _item_id(item_id: str) -> str:
        if not isinstance(item_id, str) or not _PORTABLE_ID.fullmatch(item_id):
            raise RepositoryError(
                "the translation item identity is invalid",
                code="invalid_translation_item_identity",
                details={"item_id": item_id if isinstance(item_id, str) else ""},
            )
        return item_id

    def _entry_path(self, item_id: str) -> tuple[Path, Path]:
        try:
            supplied = Path(self._entry_directory_for(item_id))
        except Exception as exc:
            raise RepositoryError(
                "the item directory could not be resolved",
                code="invalid_translation_item_path",
                details={"item_id": item_id},
            ) from exc
        lexical = Path(os.path.abspath(supplied))
        resolved = lexical.resolve()
        try:
            relative = resolved.relative_to(self._write_set.root)
        except ValueError as exc:
            raise RepositoryError(
                "the item directory escapes the translation workspace",
                code="invalid_translation_item_path",
                details={"item_id": item_id, "path": str(supplied)},
            ) from exc
        if resolved != lexical or resolved.name != item_id:
            raise RepositoryError(
                "the item directory does not match its repository identity",
                code="invalid_translation_item_identity",
                details={"item_id": item_id, "path": str(supplied)},
            )
        return resolved, relative


class FilesystemTranslationSession:
    """One lease-bound coherent read session and optional write unit."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_id: str,
        entry_directory: Path,
        entry_relative: Path,
        source_snapshot_for: SourceSnapshotLoader,
        source_reference_for: SourceReference,
        policies: TranslationPolicyPort,
        clock: Callable[[], datetime],
        writable: bool,
    ) -> None:
        self._write_set = write_set
        self._item_id = item_id
        self._entry_directory = entry_directory
        self._entry_relative = entry_relative
        self._translations = entry_directory / "translations"
        self._manifest_path = entry_directory / "manifest.json"
        self._source_snapshot_for = source_snapshot_for
        self._source_reference_for = source_reference_for
        self._policies = policies
        self._clock = clock
        self._writable = writable
        self._provenance = TranslationProvenanceService()
        self._source_by_layer: dict[str, TranslationSourceSnapshot | None] = {}
        self._reference_by_layer: dict[str, str] = {}
        self._stored_by_id: dict[str, _StoredTranslation] | None = None

    def list(self, item_id: str) -> tuple[TranslationAggregate, ...]:
        self._require_item(item_id)
        return tuple(
            stored.aggregate
            for stored in sorted(
                self._load_all().values(),
                key=lambda value: (
                    value.aggregate.target_language,
                    value.aggregate.translation_id,
                ),
            )
        )

    def load(
        self, item_id: str, translation_id: str
    ) -> TranslationAggregate | None:
        self._require_item(item_id)
        self._require_translation_id(translation_id)
        stored = self._load_all().get(translation_id)
        return stored.aggregate if stored is not None else None

    def load_source(
        self, item_id: str, layer_id: str
    ) -> TranslationSourceSnapshot | None:
        self._require_item(item_id)
        if not isinstance(layer_id, str) or not _PORTABLE_ID.fullmatch(layer_id):
            raise RepositoryError(
                "the requested source layer identity is invalid",
                code="invalid_translation_source_identity",
                details={"item_id": item_id, "layer_id": str(layer_id)},
            )
        if layer_id not in self._source_by_layer:
            # Loading aggregates establishes the reversible mapping from their
            # opaque engine layer to the legacy ``src`` reference.
            self._load_all()
        if layer_id not in self._source_by_layer:
            return None
        return self._source_by_layer[layer_id]

    def compare_and_save(
        self,
        aggregate: TranslationAggregate,
        *,
        expected_document_revision: str,
        expected_source_revision: str,
    ) -> None:
        if not self._writable:
            raise RepositoryError(
                "a read-only translation snapshot cannot save",
                code="translation_snapshot_read_only",
            )
        if not isinstance(aggregate, TranslationAggregate):
            raise RepositoryError(
                "the translation aggregate is invalid",
                code="invalid_translation_aggregate",
            )
        self._require_item(aggregate.item_id)
        self._require_translation_id(aggregate.translation_id)

        # Discard cached file state for the compare.  Legacy or test writers
        # which bypass the shared lock are still caught before publication.
        self._source_by_layer.clear()
        self._reference_by_layer.clear()
        fresh = self._read_all()
        current = fresh.get(aggregate.translation_id)
        if current is None:
            raise NotFoundError(
                "the translation does not exist",
                code="translation_not_found",
                details={
                    "item_id": self._item_id,
                    "translation_id": aggregate.translation_id,
                },
            )
        if (
            aggregate.item_id != current.aggregate.item_id
            or aggregate.translation_id != current.aggregate.translation_id
            or aggregate.target_language != current.aggregate.target_language
            or aggregate.source_layer_id != current.aggregate.source_layer_id
        ):
            raise RepositoryError(
                "the translation aggregate identity cannot be changed",
                code="translation_identity_mismatch",
                details={"translation_id": aggregate.translation_id},
            )

        reference = current.source_reference
        source = self._read_source(reference)
        if source is None or source.layer_id != current.aggregate.source_layer_id:
            current_source_revision = self._unavailable_source_revision(
                current.aggregate.source_layer_id
            )
        else:
            current_source_revision = self._source_revision(source)
        if current_source_revision != expected_source_revision:
            raise ConflictError(
                "the translation source changed; reload it before saving",
                code="stale_translation_source_revision",
                details={
                    "conflict_kind": "source",
                    "item_id": self._item_id,
                    "translation_id": aggregate.translation_id,
                    "expected_source_revision": expected_source_revision,
                    "current_source_revision": current_source_revision,
                },
                retryable=True,
            )
        current_document_revision = self._document_revision(current.aggregate)
        if current_document_revision != expected_document_revision:
            raise ConflictError(
                "the translation document changed; reload it before saving",
                code="stale_translation_document_revision",
                details={
                    "conflict_kind": "document",
                    "item_id": self._item_id,
                    "translation_id": aggregate.translation_id,
                    "expected_document_revision": expected_document_revision,
                    "current_document_revision": current_document_revision,
                },
                retryable=True,
            )
        if source is None:
            raise NotFoundError(
                "the translation source is not available",
                code="translation_source_not_found",
                details={
                    "item_id": self._item_id,
                    "translation_id": aggregate.translation_id,
                },
            )

        text_payload, metadata_payload = self._render_update(
            current, aggregate, source
        )
        manifest_payload = self._render_manifest(
            current,
            source,
            text_payload,
            expected_source_revision,
        )
        transaction = self._write_set.begin(
            scope=f"translation:{self._item_id}",
            metadata={
                "item_id": self._item_id,
                "translation_id": aggregate.translation_id,
                "document_revision": expected_document_revision,
                "source_revision": expected_source_revision,
            },
        )
        transaction.stage_write(current.text_relative, text_payload)
        transaction.stage_write(current.metadata_relative, metadata_payload)
        transaction.stage_write(self._relative("manifest.json"), manifest_payload)
        transaction.commit()

        # Keep this lease-bound session coherent if a caller performs another
        # read after saving.
        self._stored_by_id = None
        self._source_by_layer.clear()
        self._reference_by_layer.clear()

    def _load_all(self) -> dict[str, "_StoredTranslation"]:
        if self._stored_by_id is None:
            self._stored_by_id = self._read_all()
        return self._stored_by_id

    def _read_all(self) -> dict[str, "_StoredTranslation"]:
        if not os.path.lexists(self._translations):
            return {}
        self._assert_safe_read_path(self._translations, directory=True)
        text_by_language: dict[str, Path] = {}
        meta_by_language: dict[str, Path] = {}
        aliases: dict[str, str] = {}
        try:
            children = sorted(self._translations.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise RepositoryError(
                "the translation directory cannot be read",
                code="invalid_translation_storage",
                details={"path": str(self._translations)},
            ) from exc
        for path in children:
            if not path.is_file():
                continue
            self._assert_safe_read_path(path)
            name = path.name
            if name.casefold().endswith(".meta.json"):
                if not name.endswith(".meta.json"):
                    self._invalid_storage(path, "metadata suffix is not canonical")
                raw_language = name[: -len(".meta.json")]
                target = meta_by_language
            elif name.casefold().endswith(".txt"):
                if not name.endswith(".txt"):
                    self._invalid_storage(path, "text suffix is not canonical")
                raw_language = name[:-4]
                target = text_by_language
            else:
                continue
            language = self._canonical_storage_language(raw_language, path)
            alias = language.casefold()
            owner = aliases.get(alias)
            if owner is not None and owner != raw_language:
                self._invalid_storage(path, "translation languages alias each other")
            aliases[alias] = raw_language
            if language in target:
                self._invalid_storage(path, "translation artifact is duplicated")
            target[language] = path

        orphan_metadata = sorted(set(meta_by_language) - set(text_by_language))
        if orphan_metadata:
            path = meta_by_language[orphan_metadata[0]]
            self._invalid_storage(path, "metadata has no translation document")

        stored: dict[str, _StoredTranslation] = {}
        for language, path in sorted(text_by_language.items()):
            value = self._read_translation(
                language, path, meta_by_language.get(language)
            )
            identifier = value.aggregate.translation_id
            if identifier in stored:
                self._invalid_storage(path, "translation identifiers collide")
            stored[identifier] = value
        return stored

    def _read_translation(
        self, language: str, text_path: Path, metadata_path: Path | None
    ) -> "_StoredTranslation":
        pages = _parse_pages(_strict_text(text_path), path=text_path)
        default_meta = {"version": 1, "src": "", "model": "", "pages": {}}
        metadata = (
            _strict_json(metadata_path, default_meta)
            if metadata_path is not None
            else default_meta
        )
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("pages"), dict
        ):
            self._invalid_storage(
                metadata_path or text_path, "metadata has no pages object"
            )
        version = metadata.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            self._invalid_storage(
                metadata_path or text_path, "metadata version is invalid"
            )
        source_reference = _string_field(
            metadata.get("src", ""), "src", path=metadata_path or text_path
        )
        top_model = _string_field(
            metadata.get("model", ""), "model", path=metadata_path or text_path
        )
        raw_records: dict[int, dict[str, Any]] = {}
        for raw_page, record in metadata["pages"].items():
            if not isinstance(raw_page, str) or not raw_page.isdigit():
                self._invalid_storage(
                    metadata_path or text_path, "metadata page key is invalid"
                )
            page = int(raw_page)
            if page < 1 or str(page) != raw_page or not isinstance(record, dict):
                self._invalid_storage(
                    metadata_path or text_path, "metadata page record is invalid"
                )
            if page not in pages:
                self._invalid_storage(
                    metadata_path or text_path,
                    "metadata page has no translation text",
                )
            raw_records[page] = record

        source = self._read_source(source_reference)
        layer_id = (
            source.layer_id if source is not None else _fallback_layer_id(source_reference)
        )
        self._remember_source(layer_id, source_reference, source)
        source_canvases = {
            _page(canvas.selector): canvas
            for canvas in (source.canvases if source is not None else ())
        }
        records: list[TranslationPageRecord] = []
        decoded: dict[int, TranslationPageRecord] = {}
        for page, text in sorted(pages.items()):
            raw = raw_records.get(page, {})
            record = self._decode_record(
                page=page,
                text=text,
                raw=raw,
                top_source_reference=source_reference,
                top_model=top_model,
                source=source,
                source_canvas=source_canvases.get(page),
                aggregate_layer_id=layer_id,
                metadata_path=metadata_path or text_path,
            )
            records.append(record)
            decoded[page] = record
        try:
            aggregate = TranslationAggregate(
                translation_id=translation_id_for_language(language),
                item_id=self._item_id,
                target_language=language,
                source_layer_id=layer_id,
                pages=tuple(records),
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "a stored translation cannot be represented safely",
                code="invalid_translation_storage",
                details={"path": str(text_path)},
            ) from exc
        return _StoredTranslation(
            aggregate=aggregate,
            storage_language=text_path.stem,
            text_path=text_path,
            metadata_path=metadata_path
            or self._translations / f"{text_path.stem}.meta.json",
            text_relative=self._relative("translations", text_path.name),
            metadata_relative=self._relative(
                "translations", f"{text_path.stem}.meta.json"
            ),
            metadata=metadata,
            raw_records=raw_records,
            decoded_records=decoded,
            source_reference=source_reference,
        )

    def _decode_record(
        self,
        *,
        page: int,
        text: str,
        raw: Mapping[str, Any],
        top_source_reference: str,
        top_model: str,
        source: TranslationSourceSnapshot | None,
        source_canvas: TranslationSourceCanvas | None,
        aggregate_layer_id: str,
        metadata_path: Path,
    ) -> TranslationPageRecord:
        source_hash = _string_field(
            raw.get("source_hash", ""), "source_hash", path=metadata_path
        )
        legacy_sha = _string_field(raw.get("sha1", ""), "sha1", path=metadata_path)
        if source_hash and not _SOURCE_HASH.fullmatch(source_hash):
            self._invalid_storage(metadata_path, "source_hash is invalid")
        if legacy_sha and not _SHA1.fullmatch(legacy_sha):
            self._invalid_storage(metadata_path, "sha1 is invalid")
        record_reference = _string_field(
            raw.get("src", top_source_reference), "page src", path=metadata_path
        )
        explicit_revision = _string_field(
            raw.get("source_revision", ""),
            "source_revision",
            path=metadata_path,
        )
        explicit_layer = _string_field(
            raw.get("source_layer_id", ""),
            "source_layer_id",
            path=metadata_path,
        )
        if explicit_revision and not _PORTABLE_ID.fullmatch(explicit_revision):
            self._invalid_storage(metadata_path, "source_revision is invalid")
        if explicit_layer and not _PORTABLE_ID.fullmatch(explicit_layer):
            self._invalid_storage(metadata_path, "source_layer_id is invalid")

        source_revision = explicit_revision
        source_layer_id = explicit_layer
        tracked = bool(explicit_revision or source_hash or legacy_sha)
        if not source_revision and tracked:
            matches = False
            if source is not None and source_canvas is not None:
                current_reference = self._source_reference(source)
                reference_matches = not record_reference or (
                    record_reference == current_reference
                )
                if reference_matches and source_hash:
                    matches = source_hash == self._provenance.source_hash(
                        source_canvas.text
                    )
                elif reference_matches and legacy_sha:
                    matches = legacy_sha == self._provenance.legacy_source_hash(
                        source_canvas.text
                    )
            source_revision = (
                self._canvas_revision(source, source_canvas)
                if matches and source is not None and source_canvas is not None
                else source_hash or ("legacy-" + legacy_sha)
            )
        if not source_layer_id and tracked:
            current_reference = self._source_reference(source) if source else ""
            source_layer_id = (
                aggregate_layer_id
                if not record_reference or record_reference == current_reference
                else _fallback_layer_id(record_reference)
            )

        model_value = raw.get("model", top_model)
        model = _string_field(model_value, "model", path=metadata_path)
        provider_id = _string_field(
            raw.get("provider_id", ""), "provider_id", path=metadata_path
        )
        recipe_revision = _string_field(
            raw.get("recipe_revision", ""),
            "recipe_revision",
            path=metadata_path,
        )
        updated_at = _string_field(
            raw.get("updated_at", raw.get("at", "")),
            "updated_at",
            path=metadata_path,
        )
        origin = _string_field(
            raw.get("origin", "machine" if tracked and model else "legacy"),
            "origin",
            path=metadata_path,
        )
        review_state = _string_field(
            raw.get("review_state", "unreviewed"),
            "review_state",
            path=metadata_path,
        )
        try:
            return TranslationPageRecord(
                selector=_selector(page),
                text=text,
                source_revision=source_revision,
                source_layer_id=source_layer_id,
                origin=origin,  # type: ignore[arg-type]
                review_state=review_state,  # type: ignore[arg-type]
                provider_id=provider_id,
                model=model,
                recipe_revision=recipe_revision,
                updated_at=updated_at,
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "translation page metadata cannot be represented safely",
                code="invalid_translation_storage",
                details={"path": str(metadata_path), "page": page},
            ) from exc

    def _render_update(
        self,
        current: "_StoredTranslation",
        aggregate: TranslationAggregate,
        source: TranslationSourceSnapshot,
    ) -> tuple[bytes, bytes]:
        pages: dict[int, str] = {}
        records_by_page: dict[int, TranslationPageRecord] = {}
        for record in aggregate.pages:
            page = _page(record.selector)
            if page in pages:
                raise RepositoryError(
                    "the translation aggregate contains duplicate pages",
                    code="invalid_translation_aggregate",
                    details={"page": page},
                )
            pages[page] = record.text
            records_by_page[page] = record
        text_payload = _text_bytes(_render_pages(pages))

        metadata = deepcopy(current.metadata)
        raw_pages: dict[str, Any] = {}
        source_by_page = {
            _page(canvas.selector): canvas for canvas in source.canvases
        }
        source_reference = self._source_reference(source)
        for page, record in sorted(records_by_page.items()):
            old_decoded = current.decoded_records.get(page)
            old_raw = current.raw_records.get(page)
            if old_decoded == record and isinstance(old_raw, dict):
                raw_pages[str(page)] = deepcopy(old_raw)
                continue
            raw = {
                key: deepcopy(value)
                for key, value in (old_raw or {}).items()
                if key
                not in {
                    "source_hash",
                    "sha1",
                    "src",
                    "source_revision",
                    "source_layer_id",
                    "origin",
                    "review_state",
                    "provider_id",
                    "model",
                    "recipe_revision",
                    "updated_at",
                    "at",
                }
            }
            raw.update(
                {
                    "source_revision": record.source_revision,
                    "source_layer_id": record.source_layer_id,
                    "origin": record.origin,
                    "review_state": record.review_state,
                    "provider_id": record.provider_id,
                    "model": record.model,
                    "recipe_revision": record.recipe_revision,
                    "updated_at": record.updated_at,
                    "at": record.updated_at,
                    "src": source_reference,
                }
            )
            canvas = source_by_page.get(page)
            if canvas is not None and record.source_layer_id == source.layer_id:
                raw["source_hash"] = self._provenance.source_hash(canvas.text)
                raw["sha1"] = self._provenance.legacy_source_hash(canvas.text)
            raw_pages[str(page)] = raw
        metadata["version"] = max(3, int(metadata.get("version", 1)))
        metadata["src"] = source_reference
        if not isinstance(metadata.get("model"), str):
            metadata["model"] = ""
        metadata["pages"] = raw_pages
        return text_payload, _json_bytes(metadata)

    def _render_manifest(
        self,
        current: "_StoredTranslation",
        source: TranslationSourceSnapshot,
        text_payload: bytes,
        source_revision: str,
    ) -> bytes:
        if os.path.lexists(self._manifest_path):
            self._assert_safe_read_path(self._manifest_path)
        manifest = _strict_json(
            self._manifest_path, {"version": 1, "artifacts": {}}
        )
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("artifacts"), dict
        ):
            self._invalid_storage(
                self._manifest_path, "manifest has no artifacts object"
            )
        artifacts = manifest["artifacts"]
        old = artifacts.get(current.text_relative.removeprefix(
            self._entry_relative.as_posix() + "/"
        ))
        row = deepcopy(old) if isinstance(old, dict) else {}
        now = self._timestamp()
        row.pop("size", None)
        row.pop("mtime", None)
        row["sha256"] = hashlib.sha256(text_payload).hexdigest()
        row["produced_by"] = {
            "kind": "manual-edit",
            "engine": "translation-aggregate",
            "source_revision": source_revision,
        }
        source_input = self._source_manifest_input(source)
        if source_input is not None:
            row["inputs"] = [source_input]
        else:
            row["inputs"] = list(row.get("inputs") or [])
        row["created_at"] = row.get("created_at") or now
        row["updated_at"] = now
        artifact = f"translations/{current.storage_language}.txt"
        artifacts[artifact] = row
        manifest["version"] = 1
        return _json_bytes(manifest)

    def _source_manifest_input(
        self, source: TranslationSourceSnapshot
    ) -> dict[str, str] | None:
        reference = self._source_reference(source)
        if (
            not reference
            or Path(reference).name != reference
            or reference in {".", ".."}
        ):
            return None
        path = self._entry_directory / "ocr" / reference
        if not path.is_file():
            return None
        self._assert_safe_read_path(path)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise RepositoryError(
                "the translation source input cannot be fingerprinted",
                code="invalid_translation_storage",
                details={"path": str(path)},
            ) from exc
        return {
            "artifact": f"ocr/{reference}",
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _read_source(self, reference: str) -> TranslationSourceSnapshot | None:
        try:
            source = self._source_snapshot_for(self._item_id, reference)
        except RepositoryError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the authoritative translation source cannot be read",
                code="translation_source_read_failed",
                details={"item_id": self._item_id, "source_reference": reference},
            ) from exc
        if source is None:
            return None
        if not isinstance(source, TranslationSourceSnapshot):
            raise RepositoryError(
                "the source callback returned an invalid snapshot",
                code="invalid_translation_source_snapshot",
                details={"item_id": self._item_id},
            )
        if source.item_id != self._item_id:
            raise RepositoryError(
                "the source snapshot identity does not match the repository",
                code="translation_source_identity_mismatch",
                details={
                    "expected_item_id": self._item_id,
                    "actual_item_id": source.item_id,
                },
            )
        for canvas in source.canvases:
            try:
                _page(canvas.selector)
            except RepositoryError as exc:
                raise RepositoryError(
                    "the source snapshot does not use page selectors",
                    code="invalid_translation_source_snapshot",
                    details={
                        "item_id": self._item_id,
                        "selector": canvas.selector,
                    },
                ) from exc
        self._source_reference(source)
        return source

    def _source_reference(self, source: TranslationSourceSnapshot) -> str:
        try:
            reference = self._source_reference_for(source)
        except Exception as exc:
            raise RepositoryError(
                "the source storage reference cannot be resolved",
                code="invalid_translation_source_snapshot",
                details={"item_id": self._item_id},
            ) from exc
        return _string_field(
            reference,
            "source reference",
            path=self._entry_directory,
            optional=False,
        )

    def _remember_source(
        self,
        layer_id: str,
        reference: str,
        source: TranslationSourceSnapshot | None,
    ) -> None:
        if layer_id in self._reference_by_layer:
            previous_reference = self._reference_by_layer[layer_id]
            previous_source = self._source_by_layer[layer_id]
            if previous_reference != reference or previous_source != source:
                raise RepositoryError(
                    "source layer identities collide in translation storage",
                    code="translation_source_identity_mismatch",
                    details={"item_id": self._item_id, "layer_id": layer_id},
                )
        self._reference_by_layer[layer_id] = reference
        self._source_by_layer[layer_id] = source

    def _document_revision(self, aggregate: TranslationAggregate) -> str:
        try:
            return self._policies.revision(aggregate.as_dict(), "tr")
        except Exception as exc:
            raise RepositoryError(
                "the translation document revision cannot be computed",
                code="invalid_translation_revision",
            ) from exc

    def _source_revision(self, source: TranslationSourceSnapshot) -> str:
        try:
            return self._policies.revision(source.as_dict(), "ts")
        except Exception as exc:
            raise RepositoryError(
                "the translation source revision cannot be computed",
                code="invalid_translation_revision",
            ) from exc

    def _canvas_revision(
        self,
        source: TranslationSourceSnapshot,
        canvas: TranslationSourceCanvas,
    ) -> str:
        try:
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
        except Exception as exc:
            raise RepositoryError(
                "the translation canvas revision cannot be computed",
                code="invalid_translation_revision",
            ) from exc

    def _unavailable_source_revision(self, layer_id: str) -> str:
        try:
            return self._policies.revision(
                {
                    "available": False,
                    "item_id": self._item_id,
                    "layer_id": layer_id,
                },
                "ts",
            )
        except Exception as exc:
            raise RepositoryError(
                "the translation source revision cannot be computed",
                code="invalid_translation_revision",
            ) from exc

    def _timestamp(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise RepositoryError(
                "the translation repository clock returned an invalid value",
                code="translation_clock_failed",
            )
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _canonical_storage_language(self, raw: str, path: Path) -> str:
        if not _STORAGE_LANGUAGE.fullmatch(raw):
            self._invalid_storage(path, "translation language is not canonical")
        try:
            language = self._policies.normalize_language(raw)
        except Exception as exc:
            raise RepositoryError(
                "the translation language policy failed",
                code="invalid_translation_storage",
                details={"path": str(path)},
            ) from exc
        if not language:
            self._invalid_storage(path, "translation language is invalid")
        return language

    def _assert_safe_read_path(self, path: Path, *, directory: bool = False) -> None:
        lexical = Path(os.path.abspath(path))
        resolved = lexical.resolve()
        try:
            resolved.relative_to(self._entry_directory)
        except ValueError as exc:
            raise RepositoryError(
                "a translation artifact escapes its item directory",
                code="invalid_translation_item_path",
                details={"path": str(path)},
            ) from exc
        if resolved != lexical or path.is_symlink():
            raise RepositoryError(
                "a translation artifact redirects through a link",
                code="invalid_translation_item_path",
                details={"path": str(path)},
            )
        if directory and not path.is_dir():
            self._invalid_storage(path, "translation storage is not a directory")

    def _require_item(self, item_id: str) -> None:
        if item_id != self._item_id:
            raise RepositoryError(
                "the translation session item identity does not match",
                code="translation_identity_mismatch",
                details={
                    "expected_item_id": self._item_id,
                    "actual_item_id": item_id,
                },
            )

    @staticmethod
    def _require_translation_id(translation_id: str) -> None:
        if not isinstance(translation_id, str) or not _PORTABLE_ID.fullmatch(
            translation_id
        ):
            raise RepositoryError(
                "the translation identity is invalid",
                code="invalid_translation_identity",
            )

    def _relative(self, *parts: str) -> str:
        return "/".join((self._entry_relative.as_posix(), *parts))

    @staticmethod
    def _invalid_storage(path: Path, reason: str) -> None:
        raise RepositoryError(
            "translation storage is malformed",
            code="invalid_translation_storage",
            details={"path": str(path), "reason": reason},
        )


class _StoredTranslation:
    def __init__(
        self,
        *,
        aggregate: TranslationAggregate,
        storage_language: str,
        text_path: Path,
        metadata_path: Path,
        text_relative: str,
        metadata_relative: str,
        metadata: dict[str, Any],
        raw_records: dict[int, dict[str, Any]],
        decoded_records: dict[int, TranslationPageRecord],
        source_reference: str,
    ) -> None:
        self.aggregate = aggregate
        self.storage_language = storage_language
        self.text_path = text_path
        self.metadata_path = metadata_path
        self.text_relative = text_relative
        self.metadata_relative = metadata_relative
        self.metadata = metadata
        self.raw_records = raw_records
        self.decoded_records = decoded_records
        self.source_reference = source_reference


__all__ = [
    "FilesystemTranslationRepository",
    "FilesystemTranslationSession",
    "translation_id_for_language",
]
