"""Recoverable filesystem storage for existing-item ``.lib`` imports."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ContextManager

from ...engine.errors import NotFoundError, RepositoryError
from ...engine.interchange import (
    ImportDestinationSnapshot,
    LibImportPlan,
    LibImportReceipt,
)
from .recoverable_write_set import RecoverableWriteSet, RecoverableWriteTransaction


_PAGE_MARKER = re.compile(r"^--- page (\d+) ---$", re.MULTILINE)
_BOOK_ID = re.compile(r"b-[0-9a-f]{32}")
_TRANSLATION_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _plain(value),
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RepositoryError(
            "an interchange artifact cannot be serialized",
            code="invalid_interchange_artifact",
        ) from exc


def _utf8_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError as exc:
        raise RepositoryError(
            "an interchange text artifact is not valid Unicode",
            code="invalid_interchange_artifact",
        ) from exc


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise RepositoryError(
                "an interchange object has a non-text key",
                code="invalid_interchange_artifact",
            )
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _read_json(path: Path, default: Any) -> Any:
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
            "an interchange artifact cannot be read",
            code="invalid_interchange_artifact",
            details={"path": str(path)},
        ) from exc


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except (OSError, UnicodeError) as exc:
        raise RepositoryError(
            "an interchange artifact cannot be read",
            code="invalid_interchange_artifact",
            details={"path": str(path)},
        ) from exc


def _compiled_parts(text: str) -> tuple[str, dict[int, str]]:
    marks = list(_PAGE_MARKER.finditer(text))
    if not marks:
        return text.rstrip("\n"), {}
    preamble = text[: marks[0].start()].rstrip("\n")
    pages: dict[int, str] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        page = int(mark.group(1))
        if page < 1 or page in pages:
            raise RepositoryError(
                "a compiled document has ambiguous page markers",
                code="invalid_compiled_document",
                details={"page": page},
            )
        pages[page] = text[mark.end() : end].strip("\n")
    return preamble, pages


def _render_compiled(text: str, updates: Mapping[int, str]) -> str:
    preamble, pages = _compiled_parts(text)
    pages.update({int(page): value.strip("\n") for page, value in updates.items()})
    parts = ([preamble] if preamble else []) + [
        f"--- page {page} ---\n{pages[page]}" for page in sorted(pages)
    ]
    return "\n\n".join(parts)


def _translation_pages(text: str) -> dict[int, str]:
    marks = list(_PAGE_MARKER.finditer(text))
    if not marks:
        stripped = text.strip()
        return {1: stripped} if stripped else {}
    pages: dict[int, str] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        page = int(mark.group(1))
        if page < 1 or page in pages:
            raise RepositoryError(
                "a translation document has ambiguous page markers",
                code="invalid_translation_document",
                details={"page": page},
            )
        pages[page] = text[mark.end() : end].strip()
    return pages


def _render_translation(pages: Mapping[int, str]) -> str:
    return (
        "\n\n".join(
            f"--- page {page} ---\n{pages[page]}" for page in sorted(pages)
        )
        + "\n"
    )


class FilesystemInterchangeRepository:
    """Open locked import units backed by :class:`RecoverableWriteSet`.

    ``source_ids_for`` returns ``None`` for a missing item. The supplied lock
    context must acquire legacy in-process locks in their globally documented
    order; it is entered only after the cross-process workspace lease.
    """

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        entry_directory_for: Callable[[str], Path],
        source_ids_for: Callable[[str], tuple[str, ...] | None],
        clean_region_id: Callable[[Any], str],
        normalize_language: Callable[[str], str],
        sanitize_document_name: Callable[[str], str] | None = None,
        lock_context_for: Callable[[str], ContextManager[None]] | None = None,
        recover: bool = True,
    ) -> None:
        self._write_set = write_set
        self._entry_directory_for = entry_directory_for
        self._source_ids_for = source_ids_for
        self._clean_region_id = clean_region_id
        self._normalize_language = normalize_language
        self._sanitize_document_name = sanitize_document_name or str
        self._lock_context_for = lock_context_for or (lambda _item_id: nullcontext())
        if recover:
            # A new process/repository must settle interrupted publication
            # before it can expose a destination snapshot for another import.
            self._write_set.recover_all()

    @contextmanager
    def unit_of_work(
        self,
        item_id: str,
        *,
        source_id: str,
        operation_id: str,
    ) -> Iterator["FilesystemInterchangeUnitOfWork"]:
        with self._write_set.workspace_lease():
            with self._lock_context_for(item_id):
                source_ids = self._source_ids_for(item_id)
                if source_ids is None:
                    raise NotFoundError(
                        "no such item",
                        code="item_not_found",
                        details={"item_id": item_id},
                    )
                unit = FilesystemInterchangeUnitOfWork(
                    self._write_set,
                    item_id=item_id,
                    source_id=source_id,
                    operation_id=operation_id,
                    entry_directory=self._entry_directory_for(item_id),
                    source_ids=source_ids,
                    clean_region_id=self._clean_region_id,
                    normalize_language=self._normalize_language,
                    sanitize_document_name=self._sanitize_document_name,
                )
                yield unit


class FilesystemInterchangeUnitOfWork:
    """One snapshot, staging buffer, and durable publication boundary."""

    def __init__(
        self,
        write_set: RecoverableWriteSet,
        *,
        item_id: str,
        source_id: str,
        operation_id: str,
        entry_directory: Path,
        source_ids: tuple[str, ...],
        clean_region_id: Callable[[Any], str],
        normalize_language: Callable[[str], str],
        sanitize_document_name: Callable[[str], str],
    ) -> None:
        self._write_set = write_set
        self._item_id = item_id
        self._source_id = source_id
        self._operation_id = operation_id
        self._clean_region_id = clean_region_id
        self._normalize_language = normalize_language
        self._sanitize_document_name = sanitize_document_name
        self._entry_directory = Path(entry_directory).resolve()
        try:
            self._entry_relative = self._entry_directory.relative_to(write_set.root)
        except ValueError as exc:
            raise RepositoryError(
                "the item directory escapes the interchange workspace",
                code="invalid_interchange_item_path",
                details={"item_id": item_id},
            ) from exc
        self._ocr = self._entry_directory / "ocr"
        self._layout_path = self._ocr / "layout.json"
        self._sources_path = self._ocr / "sources.json"
        self._manifest_path = self._entry_directory / "manifest.json"
        self._layout = self._load_object(self._layout_path, {})
        self._sources = self._load_object(self._sources_path, {})
        self._manifest = self._load_object(
            self._manifest_path, {"version": 1, "artifacts": {}}
        )
        if not isinstance(self._manifest.get("artifacts"), dict):
            raise RepositoryError(
                "the artifact manifest has no artifacts object",
                code="invalid_interchange_artifact",
                details={"path": str(self._manifest_path)},
            )
        self._source_ids = tuple(source_ids)
        folded_sources = [
            value.casefold()
            for value in self._source_ids
            if isinstance(value, str)
        ]
        if len(folded_sources) != len(self._source_ids) or len(
            folded_sources
        ) != len(set(folded_sources)):
            self._invalid_destination("source identifiers are ambiguous")
        self._transaction: RecoverableWriteTransaction | None = None
        self._applied = False
        self._committed = False
        self.destination = self._snapshot()

    def receipt(self, operation_id: str) -> LibImportReceipt | None:
        path = self._receipt_path(operation_id)
        if not path.is_file():
            return None
        value = _read_json(path, None)
        try:
            return LibImportReceipt.from_dict(value)
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "an import receipt is invalid",
                code="invalid_import_receipt",
                details={"path": str(path)},
            ) from exc

    def apply(self, plan: LibImportPlan) -> None:
        if self._applied:
            raise RepositoryError(
                "the import plan was already staged",
                code="import_plan_already_staged",
            )
        if self._committed:
            raise RepositoryError(
                "the import unit is already committed",
                code="import_unit_committed",
            )
        transaction = self._write_set.begin(
            operation_id=self._operation_id,
            scope=f"interchange:{self._item_id}",
            metadata={
                "item_id": self._item_id,
                "source_id": self._source_id,
                "archive_sha256": plan.archive_sha256,
            },
        )
        postimages: dict[str, bytes] = {}
        deletions: set[str] = set()
        entry_prefix = self._entry_relative.as_posix() + "/"

        def stage_write(target: str, payload: bytes) -> None:
            transaction.stage_write(target, payload)
            artifact = target.removeprefix(entry_prefix)
            postimages[artifact] = payload
            deletions.discard(artifact)

        def stage_delete(target: str) -> None:
            transaction.stage_delete(target)
            artifact = target.removeprefix(entry_prefix)
            postimages.pop(artifact, None)
            deletions.add(artifact)

        layout = deepcopy(self._layout)
        layout_changed = False
        region_map = layout.setdefault("regions", {}).setdefault(
            self._source_id, {}
        )
        for page in sorted(plan.pages, key=lambda value: value.page):
            region_map[str(page.page)] = _plain(page.record)
            layout_changed = True
        template_map = layout.setdefault("templates", {}).setdefault(
            self._source_id, {}
        )
        for template in sorted(plan.templates, key=lambda value: value.name.casefold()):
            template_map[template.name] = _plain(template.record)
            layout_changed = True
        image_map = layout.setdefault("images", {})
        for figure in sorted(plan.figures, key=lambda value: value.name.casefold()):
            image_map[figure.name] = _plain(figure.metadata)
            stage_write(
                self._relative("ocr", "images", figure.name), figure.content
            )
            layout_changed = True
        if layout_changed:
            stage_write(
                self._relative("ocr", "layout.json"), _json_bytes(layout)
            )

        if plan.stylesheet_disposition == "imported":
            stage_write(
                self._relative("ocr", "replica-style.json"),
                _json_bytes({"version": 1, "styles": plan.stylesheet}),
            )
        if plan.manifest_ext_disposition == "imported":
            stage_write(
                self._relative("ocr", "lib-ext.json"),
                _json_bytes(plan.manifest_ext),
            )
        if plan.instructions_disposition == "imported":
            stage_write(
                self._relative("ocr", "lib-instructions.md"),
                _utf8_bytes(plan.instructions),
            )
        if plan.incoming_book_id and not self.destination.book_id:
            stage_write(
                self._relative("ocr", "lib-id.json"),
                _json_bytes({"book_id": plan.incoming_book_id}),
            )

        documents: dict[str, dict[int, str]] = {}
        bound_documents: set[str] = set()
        for compiled in plan.compiled_pages:
            documents.setdefault(compiled.document, {})[compiled.page] = compiled.text
            bound_documents.add(compiled.document)
        for template in plan.templates:
            document = template.record.get("doc")
            if isinstance(document, str) and document:
                bound_documents.add(document)
        for document, updates in sorted(documents.items()):
            path = self._ocr / document
            rendered = _render_compiled(_read_text(path), updates)
            stage_write(
                self._relative("ocr", document),
                _utf8_bytes(rendered),
            )
        if bound_documents:
            sources = deepcopy(self._sources)
            for document in sorted(bound_documents):
                if self._source_id == "primary":
                    sources.pop(document, None)
                else:
                    sources[document] = self._source_id
            if sources != self._sources or self._sources_path.is_file():
                stage_write(
                    self._relative("ocr", "sources.json"), _json_bytes(sources)
                )

        translations: dict[str, dict[int, str]] = {}
        for translation in plan.translations:
            language = translation.language
            normalized_language = (
                self._normalize_language(language)
                if isinstance(language, str)
                else ""
            )
            if (
                not isinstance(language, str)
                or not _TRANSLATION_LANGUAGE.fullmatch(language)
                or normalized_language != language
            ):
                raise RepositoryError(
                    "an import plan contains a non-canonical language",
                    code="invalid_import_plan",
                    details={"language": language},
                )
            translations.setdefault(language, {})[
                translation.page
            ] = translation.text
        for language, updates in sorted(translations.items()):
            text_path = self._entry_directory / "translations" / f"{language}.txt"
            pages = _translation_pages(_read_text(text_path))
            pages.update(updates)
            stage_write(
                self._relative("translations", f"{language}.txt"),
                _utf8_bytes(_render_translation(pages)),
            )
            meta_path = (
                self._entry_directory
                / "translations"
                / f"{language}.meta.json"
            )
            metadata = _read_json(
                meta_path,
                {"version": 1, "src": "", "model": "", "pages": {}},
            )
            if not isinstance(metadata, dict) or not isinstance(
                metadata.get("pages"), dict
            ):
                raise RepositoryError(
                    "translation metadata has no pages object",
                    code="invalid_interchange_artifact",
                    details={"path": str(meta_path)},
                )
            for page in updates:
                metadata["pages"].pop(str(page), None)
            if metadata["pages"]:
                stage_write(
                    self._relative(
                        "translations", f"{language}.meta.json"
                    ),
                    _json_bytes(metadata),
                )
            elif meta_path.is_file():
                stage_delete(
                    self._relative(
                        "translations", f"{language}.meta.json"
                    )
                )

        self._stage_manifest(
            transaction,
            postimages=postimages,
            deletions=deletions,
            plan=plan,
        )
        self._transaction = transaction
        self._applied = True

    def _stage_manifest(
        self,
        transaction: RecoverableWriteTransaction,
        *,
        postimages: Mapping[str, bytes],
        deletions: set[str],
        plan: LibImportPlan,
    ) -> None:
        if not postimages and not deletions:
            return
        manifest = deepcopy(self._manifest)
        artifacts = manifest.setdefault("artifacts", {})
        if not isinstance(artifacts, dict):
            raise RepositoryError(
                "the artifact manifest has no artifacts object",
                code="invalid_interchange_artifact",
                details={"path": str(self._manifest_path)},
            )
        manifest["version"] = 1
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        producer = {
            "kind": "lib-import",
            "operation_id": self._operation_id,
            "archive_sha256": plan.archive_sha256,
            "source_id": self._source_id,
        }
        compiled_artifacts = {
            f"ocr/{compiled.document}" for compiled in plan.compiled_pages
        }
        layout_payload = postimages.get("ocr/layout.json")
        layout_input = (
            {
                "artifact": "ocr/layout.json",
                "sha256": hashlib.sha256(layout_payload).hexdigest(),
            }
            if layout_payload is not None
            else None
        )
        for artifact, payload in sorted(postimages.items()):
            old = artifacts.get(artifact)
            old = old if isinstance(old, dict) else {}
            artifacts[artifact] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "produced_by": producer,
                "inputs": (
                    [layout_input]
                    if artifact in compiled_artifacts and layout_input is not None
                    else []
                ),
                "created_at": (
                    old.get("created_at")
                    if isinstance(old.get("created_at"), str)
                    else timestamp
                ),
                "updated_at": timestamp,
            }
        for artifact in sorted(deletions):
            artifacts.pop(artifact, None)
        transaction.stage_write(
            self._relative("manifest.json"), _json_bytes(manifest)
        )

    def commit(self, receipt: LibImportReceipt) -> None:
        if not self._applied or self._transaction is None:
            raise RepositoryError(
                "no import plan has been staged",
                code="import_plan_not_staged",
            )
        if self._committed:
            raise RepositoryError(
                "the import unit is already committed",
                code="import_unit_committed",
            )
        self._transaction.stage_write(
            self._receipt_relative(receipt.operation_id),
            _json_bytes(receipt.as_dict()),
        )
        self._transaction.commit(receipt=receipt.as_dict())
        self._committed = True

    def _snapshot(self) -> ImportDestinationSnapshot:
        regions = self._layout.get("regions")
        if regions is None:
            regions = {}
        if not isinstance(regions, dict):
            self._invalid_destination("regions is not an object")
        unknown_sources = sorted(set(regions) - set(self._source_ids))
        if unknown_sources:
            self._invalid_destination(
                "regions names an unknown source", sources=unknown_sources
            )

        pages_by_source: dict[str, dict[int, dict[str, Any]]] = {}
        region_ids: dict[str, dict[int, tuple[str, ...]]] = {}
        for source_id in self._source_ids:
            raw_pages = regions.get(source_id)
            if raw_pages is None:
                raw_pages = {}
            if not isinstance(raw_pages, dict):
                self._invalid_destination(
                    "a region source is not an object", source_id=source_id
                )
            canonical_pages: dict[int, dict[str, Any]] = {}
            collected: dict[int, tuple[str, ...]] = {}
            for raw_page, record in raw_pages.items():
                if not isinstance(record, dict):
                    self._invalid_destination(
                        "a region page is not an object",
                        source_id=source_id,
                        page=str(raw_page),
                    )
                try:
                    page = int(raw_page)
                except (TypeError, ValueError):
                    self._invalid_destination(
                        "a region page key is invalid",
                        source_id=source_id,
                        page=str(raw_page),
                    )
                if str(page) != raw_page:
                    self._invalid_destination(
                        "a region page key is not canonical",
                        source_id=source_id,
                        page=str(raw_page),
                    )
                if page < 1 or page > 99_999:
                    self._invalid_destination(
                        "a region page key is out of range",
                        source_id=source_id,
                        page=page,
                    )
                if page in canonical_pages:
                    self._invalid_destination(
                        "the destination has ambiguous page keys",
                        source_id=source_id,
                        page=page,
                    )
                items = record.get("items")
                if not isinstance(items, list):
                    self._invalid_destination(
                        "a region page has no canonical items list",
                        source_id=source_id,
                        page=page,
                    )
                ids_list: list[str] = []
                for index, item in enumerate(items):
                    if not isinstance(item, dict):
                        self._invalid_destination(
                            "a region item is not an object",
                            source_id=source_id,
                            page=page,
                            index=index,
                        )
                    raw_region_id = item.get("rid")
                    region_id = self._clean_region_id(raw_region_id)
                    if raw_region_id not in (None, "") and not region_id:
                        self._invalid_destination(
                            "a region identity is invalid",
                            source_id=source_id,
                            page=page,
                            index=index,
                        )
                    if region_id:
                        ids_list.append(region_id)
                canonical_pages[page] = record
                ids = tuple(ids_list)
                collected[page] = ids
            pages_by_source[source_id] = canonical_pages
            region_ids[source_id] = collected
        pages = pages_by_source.get(self._source_id, {})

        templates_root = self._layout.get("templates")
        if templates_root is None:
            templates_root = {}
        if not isinstance(templates_root, dict):
            self._invalid_destination("templates is not an object")
        unknown_template_sources = sorted(
            set(templates_root) - set(self._source_ids)
        )
        if unknown_template_sources:
            self._invalid_destination(
                "templates names an unknown source",
                sources=unknown_template_sources,
            )
        templates_by_source: dict[str, dict[str, Any]] = {}
        for source_id in self._source_ids:
            source_templates = templates_root.get(source_id)
            if source_templates is None:
                source_templates = {}
            if not isinstance(source_templates, dict):
                self._invalid_destination(
                    "a template source is not an object", source_id=source_id
                )
            templates_by_source[source_id] = source_templates
        templates = templates_by_source.get(self._source_id, {})
        images = self._layout.get("images")
        if images is None:
            images = {}
        if not isinstance(images, dict):
            self._invalid_destination("images is not an object")
        figures: set[str] = set()
        for name, metadata in images.items():
            if not isinstance(name, str) or not isinstance(metadata, dict):
                self._invalid_destination("a stored figure entry is malformed")
            raw_source = metadata.get("src_key", "primary") or "primary"
            if not isinstance(raw_source, str) or raw_source not in self._source_ids:
                self._invalid_destination(
                    "a stored figure names an unknown source",
                    figure=name,
                    source_id=raw_source,
                )
            figures.add(name)
        image_directory = self._ocr / "images"
        if image_directory.is_dir():
            figures.update(
                path.name for path in image_directory.iterdir() if path.is_file()
            )

        translation_pages: dict[str, tuple[int, ...]] = {}
        translation_owners: dict[str, str] = {}
        translation_directory = self._entry_directory / "translations"
        if translation_directory.is_dir():
            for path in sorted(translation_directory.glob("*.txt")):
                raw_language = path.stem
                language = self._normalize_language(raw_language)
                if (
                    not language
                    or not _TRANSLATION_LANGUAGE.fullmatch(raw_language)
                    or language != raw_language
                ):
                    self._invalid_destination(
                        "a translation filename has no canonical language",
                        document=path.name,
                    )
                previous = translation_owners.get(language.casefold())
                if previous is not None:
                    self._invalid_destination(
                        "translation filenames alias the same language",
                        documents=[previous, path.name],
                        language=language,
                    )
                translation_owners[language.casefold()] = path.name
                translation_pages[language] = tuple(
                    sorted(_translation_pages(_read_text(path)))
                )
            metadata_owners: dict[str, str] = {}
            for path in sorted(translation_directory.glob("*.meta.json")):
                raw_language = path.name[: -len(".meta.json")]
                language = self._normalize_language(raw_language)
                if (
                    not language
                    or not _TRANSLATION_LANGUAGE.fullmatch(raw_language)
                    or language != raw_language
                ):
                    self._invalid_destination(
                        "a translation metadata filename has no canonical language",
                        document=path.name,
                    )
                previous = metadata_owners.get(language.casefold())
                if previous is not None:
                    self._invalid_destination(
                        "translation metadata filenames alias the same language",
                        documents=[previous, path.name],
                        language=language,
                    )
                metadata_owners[language.casefold()] = path.name
                metadata = _read_json(path, None)
                if not isinstance(metadata, dict) or not isinstance(
                    metadata.get("pages"), dict
                ):
                    self._invalid_destination(
                        "translation metadata has no pages object",
                        document=path.name,
                    )
                metadata_pages: set[int] = set()
                for raw_page in metadata["pages"]:
                    if not isinstance(raw_page, str):
                        self._invalid_destination(
                            "translation metadata has a non-text page key",
                            document=path.name,
                        )
                    try:
                        page = int(raw_page)
                    except ValueError:
                        self._invalid_destination(
                            "translation metadata has an invalid page key",
                            document=path.name,
                            page=raw_page,
                        )
                    if page < 1 or str(page) != raw_page:
                        self._invalid_destination(
                            "translation metadata has a non-canonical page key",
                            document=path.name,
                            page=raw_page,
                        )
                    if page in metadata_pages:
                        self._invalid_destination(
                            "translation metadata has ambiguous page keys",
                            document=path.name,
                            page=page,
                        )
                    metadata_pages.add(page)

        declared_document_sources: dict[str, str] = {}

        def declare_document(document: Any, owner_source: str, location: str) -> None:
            if not isinstance(document, str) or not document:
                self._invalid_destination(
                    "a layout record has no document binding", location=location
                )
            canonical = self._sanitize_document_name(document)
            if canonical != document:
                self._invalid_destination(
                    "a layout document name is not canonical",
                    document=document,
                    location=location,
                )
            previous = declared_document_sources.get(document)
            if previous is not None and previous != owner_source:
                self._invalid_destination(
                    "a layout document is owned by more than one source",
                    document=document,
                    sources=sorted({previous, owner_source}),
                )
            declared_document_sources[document] = owner_source

        for owner_source, source_pages in pages_by_source.items():
            for page, record in source_pages.items():
                declare_document(
                    record.get("doc"), owner_source, f"regions/{owner_source}/{page}"
                )
        for owner_source, source_templates in templates_by_source.items():
            for template_name, record in source_templates.items():
                if not isinstance(record, dict):
                    self._invalid_destination(
                        "a template is not an object",
                        source_id=owner_source,
                        template=str(template_name),
                    )
                declare_document(
                    record.get("doc"),
                    owner_source,
                    f"templates/{owner_source}/{template_name}",
                )

        documents = set(self._sources) | set(declared_document_sources)
        if self._ocr.is_dir():
            documents.update(
                path.name for path in self._ocr.glob("*.txt") if path.is_file()
            )
        document_sources: dict[str, str] = {}
        document_aliases: dict[str, str] = {}
        for document in sorted(documents):
            if not isinstance(document, str):
                self._invalid_destination("an OCR document name is not text")
            canonical_document = self._sanitize_document_name(document)
            if canonical_document != document:
                self._invalid_destination(
                    "an OCR document name is not canonical", document=document
                )
            previous = document_aliases.get(document.casefold())
            if previous is not None:
                self._invalid_destination(
                    "OCR document names alias the same artifact",
                    documents=[previous, document],
                )
            document_aliases[document.casefold()] = document
            raw_source = self._sources.get(
                document, declared_document_sources.get(document, "primary")
            )
            if not isinstance(raw_source, str):
                self._invalid_destination(
                    "an OCR document source is not text", document=document
                )
            source_id = raw_source or "primary"
            if source_id not in self._source_ids:
                self._invalid_destination(
                    "an OCR document is bound to an unknown source",
                    document=document,
                    source_id=source_id,
                )
            declared_source = declared_document_sources.get(document)
            if declared_source is not None and declared_source != source_id:
                self._invalid_destination(
                    "an OCR source binding contradicts the layout",
                    document=document,
                    source_id=source_id,
                    layout_source_id=declared_source,
                )
            document_sources[document] = source_id

        instructions = _read_text(self._ocr / "lib-instructions.md")
        if len(instructions) > 20_000:
            self._invalid_destination("stored import instructions exceed the limit")
        stylesheet = _read_json(self._ocr / "replica-style.json", None)
        manifest_ext = _read_json(self._ocr / "lib-ext.json", None)
        identity = _read_json(self._ocr / "lib-id.json", None)
        for name, value in (
            ("stylesheet", stylesheet),
            ("manifest ext", manifest_ext),
            ("book identity", identity),
        ):
            if value is not None and not isinstance(value, dict):
                self._invalid_destination(f"stored {name} is not an object")
        if isinstance(stylesheet, dict) and "styles" in stylesheet and not isinstance(
            stylesheet.get("styles"), dict
        ):
            self._invalid_destination("stored stylesheet has no styles object")
        raw_book_id = identity.get("book_id", "") if isinstance(identity, dict) else ""
        if not isinstance(raw_book_id, str):
            self._invalid_destination("stored book identity is not text")
        book_id = raw_book_id
        if book_id and not _BOOK_ID.fullmatch(book_id):
            self._invalid_destination("stored book identity is invalid")
        revision_payload = {
            "layout": self._layout,
            "sources": self._sources,
            "source_ids": self._source_ids,
            "translations": translation_pages,
            "instructions": instructions,
            "stylesheet": stylesheet,
            "manifest_ext": manifest_ext,
            "book_id": book_id,
        }
        try:
            revision_bytes = json.dumps(
                revision_payload,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise RepositoryError(
                "the destination revision cannot be serialized",
                code="invalid_import_destination",
                details={"item_id": self._item_id},
            ) from exc
        revision = "lib-" + hashlib.sha256(revision_bytes).hexdigest()
        try:
            return ImportDestinationSnapshot(
                item_id=self._item_id,
                revision=revision,
                book_id=book_id,
                source_ids=self._source_ids,
                pages=pages,
                region_ids=region_ids,
                templates=tuple(sorted(str(name) for name in templates)),
                figures=tuple(sorted(figures)),
                translation_pages=translation_pages,
                instructions=instructions,
                document_sources=document_sources,
                has_stylesheet=(
                    isinstance(stylesheet, dict)
                    and isinstance(stylesheet.get("styles"), dict)
                    and bool(stylesheet["styles"])
                ),
                has_manifest_ext=isinstance(manifest_ext, dict) and bool(manifest_ext),
            )
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "the destination cannot be represented safely for import",
                code="invalid_import_destination",
                details={"item_id": self._item_id},
            ) from exc

    def _invalid_destination(self, reason: str, **details: Any) -> None:
        raise RepositoryError(
            "the destination cannot be represented safely for import",
            code="invalid_import_destination",
            details={"item_id": self._item_id, "reason": reason, **details},
        )

    def _load_object(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        value = _read_json(path, default)
        if not isinstance(value, dict):
            raise RepositoryError(
                "an interchange artifact is not an object",
                code="invalid_interchange_artifact",
                details={"path": str(path)},
            )
        return value

    def _relative(self, *parts: str) -> str:
        return "/".join((self._entry_relative.as_posix(), *parts))

    def _receipt_relative(self, operation_id: str) -> str:
        digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return self._relative("ocr", ".interchange", "receipts", f"{digest}.json")

    def _receipt_path(self, operation_id: str) -> Path:
        return self._write_set.root / self._receipt_relative(operation_id)


__all__ = ["FilesystemInterchangeRepository", "FilesystemInterchangeUnitOfWork"]
