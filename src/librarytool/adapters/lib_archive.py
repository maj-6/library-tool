"""Strict ``.lib`` archive decoding and existing-item import planning.

The planner is intentionally independent of Flask and filesystem layout.  The
composition root supplies the format sanitizers and Replica policies while
this adapter owns hostile-ZIP handling, merge decisions, and construction of
the engine's immutable import plan.
"""

from __future__ import annotations

import io
import hashlib
import json
import re
import zipfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..engine.errors import ConflictError, ValidationError
from ..engine.interchange import (
    ImportDestinationSnapshot,
    ImportWarning,
    LibCompiledPageImport,
    LibFigureImport,
    LibImportPlan,
    LibPageImport,
    LibTemplateImport,
    LibTranslationImport,
)


_PAGE_MEMBER = re.compile(r"pages/(\d{1,5})\.json")
_ASSET_MEMBER = re.compile(r"assets/img/((?!\.+$)[\w.\-]{1,120})")
_TRANSLATION_MEMBER = re.compile(
    r"translations/([a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*)\.json"
)
_BOOK_ID = re.compile(r"b-[0-9a-f]{32}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _strict_json(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number {value}")
            ),
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid strict JSON") from exc


@dataclass(frozen=True, slots=True)
class LibArchiveLimits:
    """Resource limits applied before untrusted ZIP content is decoded."""

    max_archive_bytes: int = 250 * 1024 * 1024
    max_inflated_bytes: int = 300 * 1024 * 1024
    max_json_bytes: int = 10 * 1024 * 1024
    max_figure_bytes: int = 15 * 1024 * 1024
    max_pages: int = 2000
    max_items_per_page: int = 800
    max_members: int = 10_000
    max_instructions_chars: int = 20_000


@dataclass(frozen=True, slots=True)
class _DecodedArchive:
    book: dict[str, Any]
    members: Mapping[str, bytes]


def _plain(value: Any) -> Any:
    """Detach immutable engine JSON for legacy policy callbacks."""

    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class ExistingItemLibArchivePlanner:
    """Decode one archive and plan its merge into an existing destination."""

    def __init__(
        self,
        *,
        parse_format: Callable[[Any], tuple[int, int] | None],
        supported_major: int,
        sanitize_items: Callable[..., list[dict[str, Any]]],
        sanitize_dims: Callable[[Any], dict[str, Any] | None],
        sanitize_document_name: Callable[[str], str],
        sanitize_styles: Callable[[dict[str, Any]], dict[str, Any]],
        sanitize_ext: Callable[..., dict[str, Any]],
        sanitize_figure: Callable[..., dict[str, Any]],
        clean_region_id: Callable[[Any], str],
        is_template_name: Callable[[str], bool],
        is_protected: Callable[[dict[str, Any] | None], bool],
        compose_text: Callable[..., str],
        normalize_language: Callable[[str], str],
        limits: LibArchiveLimits | None = None,
    ) -> None:
        self._parse_format = parse_format
        self._supported_major = int(supported_major)
        self._sanitize_items = sanitize_items
        self._sanitize_dims = sanitize_dims
        self._sanitize_document_name = sanitize_document_name
        self._sanitize_styles = sanitize_styles
        self._sanitize_ext = sanitize_ext
        self._sanitize_figure = sanitize_figure
        self._clean_region_id = clean_region_id
        self._is_template_name = is_template_name
        self._is_protected = is_protected
        self._compose_text = compose_text
        self._normalize_language = normalize_language
        self._limits = limits or LibArchiveLimits()

    def plan(
        self,
        archive: bytes,
        destination: ImportDestinationSnapshot,
        *,
        source_id: str,
        overwrite: bool,
        archive_sha256: str,
    ) -> LibImportPlan:
        decoded = self._decode(archive)
        book = decoded.book
        fmt = self._parse_format(book)
        if fmt is None:
            raise ValidationError(
                "unsupported .lib format", code="unsupported_lib_format"
            )
        if fmt[0] > self._supported_major:
            raise ValidationError(
                "this .lib needs a newer Library Tool "
                f"(format {fmt[0]}.{fmt[1]})",
                code="newer_lib_format",
                details={"format_version": f"{fmt[0]}.{fmt[1]}"},
            )
        incoming_book_id = str(book.get("book_id") or "")
        if fmt[0] >= 2 and not _BOOK_ID.fullmatch(incoming_book_id):
            raise ValidationError(
                "the .lib has no valid stable book_id",
                code="invalid_lib_book_id",
            )

        warnings: list[ImportWarning] = []

        def warn(location: str, message: str) -> None:
            warnings.append(ImportWarning(str(location), str(message)))

        pages = self._pages(
            decoded.members,
            destination,
            source_id=source_id,
            overwrite=overwrite,
            archive_sha256=archive_sha256,
            warn=warn,
        )
        if not pages["usable"]:
            raise ValidationError(
                "no usable pages",
                code="no_usable_lib_pages",
                details={
                    "warnings": [warning.as_dict() for warning in warnings]
                },
            )

        applied: list[LibPageImport] = pages["applied"]
        compiled: list[LibCompiledPageImport] = pages["compiled"]
        self._reject_surviving_region_collisions(
            destination,
            applied,
            source_id=source_id,
        )
        templates = self._templates(
            book,
            destination,
            source_id=source_id,
            overwrite=overwrite,
            warn=warn,
        )
        self._reject_planned_document_aliases(applied, templates)
        figures = self._figures(
            book,
            decoded.members,
            destination,
            source_id=source_id,
            overwrite=overwrite,
            warn=warn,
        )
        translations = self._translations(
            decoded.members,
            destination,
            applied_pages={page.page for page in applied},
            overwrite=overwrite,
            warn=warn,
        )
        stylesheet, stylesheet_disposition = self._stylesheet(
            book, destination, overwrite=overwrite, warn=warn
        )
        manifest_ext, manifest_ext_disposition = self._manifest_ext(
            book, destination, overwrite=overwrite, warn=warn
        )
        instructions, instructions_disposition = self._instructions(
            book, destination, overwrite=overwrite, warn=warn
        )
        return LibImportPlan(
            archive_sha256=archive_sha256,
            format_version=f"{fmt[0]}.{fmt[1]}",
            incoming_book_id=incoming_book_id,
            pages=tuple(applied),
            pages_skipped=tuple(pages["skipped"]),
            pages_protected=tuple(pages["protected"]),
            templates=tuple(templates),
            figures=tuple(figures),
            translations=tuple(translations),
            compiled_pages=tuple(compiled),
            stylesheet=stylesheet,
            manifest_ext=manifest_ext,
            instructions=instructions,
            stylesheet_disposition=stylesheet_disposition,
            manifest_ext_disposition=manifest_ext_disposition,
            instructions_disposition=instructions_disposition,
            warnings=tuple(warnings),
        )

    def _decode(self, archive: bytes) -> _DecodedArchive:
        limits = self._limits
        if len(archive) > limits.max_archive_bytes:
            raise ValidationError("file too large", code="lib_archive_too_large")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                infos = zipped.infolist()
                if len(infos) > limits.max_members:
                    raise ValidationError(
                        "the .lib contains too many members",
                        code="lib_member_limit_exceeded",
                    )
                names: set[str] = set()
                total = 0
                book_info = None
                for info in infos:
                    if info.filename in names:
                        raise ValidationError(
                            f"duplicate archive member {info.filename!r}",
                            code="duplicate_lib_member",
                        )
                    names.add(info.filename)
                    if info.filename == "book.json":
                        book_info = info
                    if info.flag_bits & 0x1:
                        raise ValidationError(
                            "encrypted .lib members are not supported",
                            code="encrypted_lib_member",
                        )
                    total += int(info.file_size)
                    if total > limits.max_inflated_bytes:
                        raise ValidationError(
                            "the .lib expands beyond the size cap",
                            code="lib_inflated_limit_exceeded",
                        )
                if book_info is None:
                    raise ValidationError(
                        "not a .lib archive", code="invalid_lib_archive"
                    )
                if book_info.file_size > limits.max_json_bytes:
                    raise ValidationError(
                        "book.json too large", code="lib_book_too_large"
                    )

                # Read every bounded member through ZipFile before parsing any
                # semantic content. ZipFile verifies decompression and CRC here,
                # so a late corrupt/skipped asset cannot fail after staging.
                members = {
                    info.filename: zipped.read(info)
                    for info in infos
                    if not info.is_dir()
                }
        except ValidationError:
            raise
        except (
            zipfile.BadZipFile,
            zlib.error,
            RuntimeError,
            OSError,
            EOFError,
            NotImplementedError,
        ) as exc:
            raise ValidationError(
                "not a .lib archive", code="invalid_lib_archive"
            ) from exc
        try:
            book = _strict_json(members["book.json"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(
                "book.json is not valid JSON", code="invalid_lib_manifest"
            ) from exc
        if not isinstance(book, dict):
            raise ValidationError(
                "book.json is not an object", code="invalid_lib_manifest"
            )
        return _DecodedArchive(book=book, members=members)

    def _pages(
        self,
        members: Mapping[str, bytes],
        destination: ImportDestinationSnapshot,
        *,
        source_id: str,
        overwrite: bool,
        archive_sha256: str,
        warn: Callable[[str, str], None],
    ) -> dict[str, Any]:
        limits = self._limits
        usable: dict[int, dict[str, Any]] = {}
        parsed: dict[int, tuple[str, dict[str, Any], list[Any]]] = {}
        raw_region_owners: dict[str, str] = {}
        numeric_members: dict[int, str] = {}
        page_count = 0
        for name in sorted(members):
            match = _PAGE_MEMBER.fullmatch(name)
            if match is None:
                continue
            page = int(match.group(1))
            if not 1 <= page <= 99_999:
                warn(name, "page skipped: page number out of range")
                continue
            previous = numeric_members.get(page)
            if previous is not None:
                raise ValidationError(
                    f"duplicate page member for page {page}",
                    code="duplicate_lib_page",
                    details={"members": [previous, name]},
                )
            numeric_members[page] = name
            if page_count >= limits.max_pages:
                warn(name, f"page skipped: over the {limits.max_pages}-page cap")
                continue
            page_count += 1
            payload = members[name]
            if len(payload) > limits.max_json_bytes:
                warn(name, "page skipped: JSON member exceeds the size cap")
                continue
            try:
                record = _strict_json(payload)
            except (TypeError, ValueError):
                warn(name, "page skipped: not valid JSON")
                continue
            if not isinstance(record, dict) or not isinstance(
                record.get("items"), list
            ):
                warn(name, "page skipped: no items array")
                continue
            raw_items = record["items"]
            if len(raw_items) > limits.max_items_per_page:
                warn(
                    name,
                    f"page had more than {limits.max_items_per_page} regions; "
                    "the surplus was dropped",
                )
            capped = raw_items[: limits.max_items_per_page]
            for index, item in enumerate(capped):
                region_id = self._clean_region_id(
                    item.get("rid") if isinstance(item, dict) else ""
                )
                if not region_id:
                    continue
                location = f"{name}[{index}]"
                previous_owner = raw_region_owners.get(region_id)
                if previous_owner is not None:
                    raise ValidationError(
                        f"duplicate region identity {region_id!r}",
                        code="duplicate_region_identity",
                        details={
                            "region_id": region_id,
                            "locations": [previous_owner, location],
                        },
                    )
                raw_region_owners[region_id] = location
            parsed[page] = (name, record, capped)

        # The format sanitizer mints UUIDs for missing identities. Imports must
        # instead be reproducible after a crash and rollback, so reserve every
        # supplied identity first and derive the gaps from immutable input.
        used_region_ids = set(raw_region_owners)
        for page, (name, record, capped) in sorted(parsed.items()):
            deterministic_items: list[Any] = []
            for index, item in enumerate(capped):
                if not isinstance(item, dict):
                    deterministic_items.append(item)
                    continue
                canonical_item = dict(item)
                region_id = self._clean_region_id(canonical_item.get("rid"))
                if not region_id:
                    attempt = 0
                    while True:
                        seed = (
                            f"{archive_sha256}\0{page}\0{index}\0{attempt}"
                        ).encode("utf-8")
                        region_id = "import-" + hashlib.sha256(seed).hexdigest()[:56]
                        if region_id not in used_region_ids:
                            break
                        attempt += 1
                    canonical_item["rid"] = region_id
                    used_region_ids.add(region_id)
                deterministic_items.append(canonical_item)
            items = self._sanitize_items(
                deterministic_items,
                src_type="import",
                warn=warn,
                loc=name,
            )
            if not items:
                warn(name, "page skipped: no usable regions")
                continue
            document = self._document_for_source(
                str(record.get("doc") or "compiled.txt"),
                destination,
                source_id=source_id,
            )
            canonical: dict[str, Any] = {
                "doc": document,
                "dims": self._sanitize_dims(record.get("dims")) or {},
                "items": items,
            }
            page_ext = self._sanitize_ext(
                record.get("ext"), f"{name}.ext", warn
            )
            if page_ext:
                canonical["ext"] = page_ext
            state = record.get("state")
            if state == "verified":
                canonical["imported_state"] = "verified"
                warn(
                    name,
                    "verified state imported as advisory; local review is required",
                )
            elif state:
                warn(
                    name,
                    f"state {state!r} dropped: only 'verified' is recognized",
                )
            usable[page] = canonical

        applied: list[LibPageImport] = []
        compiled: list[LibCompiledPageImport] = []
        skipped: list[int] = []
        protected: list[int] = []
        for page, record in sorted(usable.items()):
            existing = destination.pages.get(page)
            existing_plain = _plain(existing) if existing is not None else None
            if self._is_protected(existing_plain):
                protected.append(page)
                warn(
                    f"pages/{page}.json",
                    "page skipped: the destination is human-edited or verified",
                )
                continue
            if not overwrite and existing is not None:
                skipped.append(page)
                warn(
                    f"pages/{page}.json",
                    "page skipped: the destination already has this page "
                    "(import with overwrite to replace)",
                )
                continue
            applied.append(LibPageImport(page, record))
            compiled.append(
                LibCompiledPageImport(
                    record["doc"],
                    source_id,
                    page,
                    self._compose_text(record["items"]),
                )
            )
        return {
            "usable": usable,
            "applied": applied,
            "compiled": compiled,
            "skipped": skipped,
            "protected": protected,
        }

    def _reject_surviving_region_collisions(
        self,
        destination: ImportDestinationSnapshot,
        pages: list[LibPageImport],
        *,
        source_id: str,
    ) -> None:
        applied_pages = {page.page for page in pages}
        owners: dict[str, tuple[str, int]] = {}
        for owner_source, source_pages in destination.region_ids.items():
            for owner_page, region_ids in source_pages.items():
                if owner_source == source_id and owner_page in applied_pages:
                    continue
                for region_id in region_ids:
                    owners[region_id] = (owner_source, owner_page)
        collisions: dict[str, dict[str, Any]] = {}
        for page in pages:
            for item in page.record.get("items", ()):
                if not isinstance(item, Mapping):
                    continue
                region_id = self._clean_region_id(item.get("rid"))
                owner = owners.get(region_id)
                if owner is not None:
                    collisions[region_id] = {
                        "source_id": owner[0],
                        "page": owner[1],
                    }
        if collisions:
            raise ConflictError(
                "an imported region identity already belongs to another page",
                code="region_identity_conflict",
                details={
                    "region_ids": sorted(collisions),
                    "owners": collisions,
                },
            )

    @staticmethod
    def _reject_planned_document_aliases(
        pages: list[LibPageImport], templates: list[LibTemplateImport]
    ) -> None:
        names: dict[str, str] = {}
        documents = [
            page.record.get("doc") for page in pages
        ] + [template.record.get("doc") for template in templates]
        for document in documents:
            if not isinstance(document, str):
                continue
            previous = names.get(document.casefold())
            if previous is not None and previous != document:
                raise ValidationError(
                    "document names alias the same destination",
                    code="duplicate_document_name",
                    details={"documents": [previous, document]},
                )
            names[document.casefold()] = document

    def _templates(
        self,
        book: Mapping[str, Any],
        destination: ImportDestinationSnapshot,
        *,
        source_id: str,
        overwrite: bool,
        warn: Callable[[str, str], None],
    ) -> list[LibTemplateImport]:
        source = book.get("templates")
        if not isinstance(source, dict):
            return []
        existing = {name.casefold(): name for name in destination.templates}
        result: list[LibTemplateImport] = []
        incoming_names: dict[str, str] = {}
        for raw_name, value in sorted(source.items(), key=lambda item: str(item[0])):
            name = str(raw_name).strip()
            location = f"templates/{name}"
            if not self._is_template_name(name):
                warn(
                    "book.json/templates",
                    f"template {name!r} dropped: not a valid template name",
                )
                continue
            alias = incoming_names.get(name.casefold())
            if alias is not None:
                raise ValidationError(
                    "template names alias the same destination",
                    code="duplicate_template_name",
                    details={"names": [alias, name]},
                )
            incoming_names[name.casefold()] = name
            if not isinstance(value, dict):
                warn(location, "template dropped: not an object")
                continue
            items = self._sanitize_items(
                value.get("items") or [],
                src_type="template",
                warn=warn,
                loc=location,
            )
            if not items:
                warn(location, "template dropped: no usable regions after sanitize")
                continue
            existing_name = existing.get(name.casefold())
            if not overwrite and existing_name is not None:
                warn(
                    location,
                    "template skipped: the destination already has this template "
                    "(import with overwrite to replace)",
                )
                continue
            if existing_name is not None:
                name = existing_name
            record = {
                "from_page": 0,
                "doc": self._document_for_source(
                    str(value.get("doc") or "compiled.txt"),
                    destination,
                    source_id=source_id,
                ),
                "dims": self._sanitize_dims(value.get("dims")) or {},
                "items": [
                    {
                        "role": item["role"],
                        "order": item["order"],
                        "box": item["box"],
                    }
                    for item in items
                ],
            }
            result.append(LibTemplateImport(name, record))
        return result

    def _figures(
        self,
        book: Mapping[str, Any],
        members: Mapping[str, bytes],
        destination: ImportDestinationSnapshot,
        *,
        source_id: str,
        overwrite: bool,
        warn: Callable[[str, str], None],
    ) -> list[LibFigureImport]:
        declared = book.get("figures")
        declared = declared if isinstance(declared, dict) else {}
        declared_names = {str(name) for name in declared}
        for member in sorted(members):
            match = _ASSET_MEMBER.fullmatch(member)
            if match is not None and match.group(1) not in declared_names:
                warn(member, "figure skipped: asset is not declared in book.json")

        existing = {name.casefold(): name for name in destination.figures}
        result: list[LibFigureImport] = []
        incoming_names: dict[str, str] = {}
        for raw_name, raw_metadata in sorted(
            declared.items(), key=lambda item: str(item[0])
        ):
            name = str(raw_name)
            location = f"assets/img/{name}"
            if _ASSET_MEMBER.fullmatch(location) is None:
                warn(
                    "book.json/figures",
                    f"figure {name!r} dropped: not a valid member name",
                )
                continue
            alias = incoming_names.get(name.casefold())
            if alias is not None:
                raise ValidationError(
                    "figure names alias the same destination",
                    code="duplicate_figure_name",
                    details={"names": [alias, name]},
                )
            incoming_names[name.casefold()] = name
            payload = members.get(location)
            if payload is None:
                warn(location, "figure skipped: declared asset is missing")
                continue
            if len(payload) > self._limits.max_figure_bytes:
                warn(location, "figure skipped: image exceeds the size cap")
                continue
            metadata = self._sanitize_figure(
                raw_metadata,
                source_id,
                warn=warn,
                loc=location,
            )
            collision = existing.get(name.casefold())
            if collision is not None:
                if (
                    overwrite
                    and collision == name
                    and metadata.get("rework_of") == name
                ):
                    pass
                elif overwrite:
                    warn(
                        location,
                        "figure skipped: name collides and the entry carries "
                        "no rework_of",
                    )
                    continue
                else:
                    warn(
                        location,
                        "figure skipped: a figure by that name already exists",
                    )
                    continue
            result.append(LibFigureImport(name, payload, metadata))
        return result

    def _translations(
        self,
        members: Mapping[str, bytes],
        destination: ImportDestinationSnapshot,
        *,
        applied_pages: set[int],
        overwrite: bool,
        warn: Callable[[str, str], None],
    ) -> list[LibTranslationImport]:
        collected: dict[str, dict[int, str]] = {}
        owners: dict[tuple[str, int], tuple[str, str]] = {}
        language_members: dict[str, str] = {}
        for name in sorted(members):
            if not name.startswith("translations/") or name.endswith("/"):
                continue
            match = _TRANSLATION_MEMBER.fullmatch(name)
            if match is None:
                warn(
                    name,
                    "translation skipped: member name is not a "
                    "translations/<bcp47>.json tag",
                )
                continue
            language = self._normalize_language(match.group(1).lower())
            if not language:
                warn(name, "translation skipped: invalid language tag")
                continue
            prior_member = language_members.get(language.casefold())
            if prior_member is not None:
                raise ValidationError(
                    "translation members alias the same language",
                    code="duplicate_translation_language",
                    details={
                        "language": language,
                        "members": [prior_member, name],
                    },
                )
            language_members[language.casefold()] = name
            payload = members[name]
            if len(payload) > self._limits.max_json_bytes:
                warn(name, "translation skipped: JSON member exceeds the size cap")
                continue
            try:
                value = _strict_json(payload)
            except (TypeError, ValueError):
                warn(name, "translation skipped: not valid JSON")
                continue
            pages = value.get("pages") if isinstance(value, dict) else None
            if not isinstance(pages, dict):
                warn(name, "translation skipped: no pages map")
                continue
            language_pages = collected.setdefault(language, {})
            logical_pages: dict[int, str] = {}
            for raw_page, raw_text in pages.items():
                try:
                    page = int(raw_page)
                except (TypeError, ValueError):
                    continue
                prior_key = logical_pages.get(page)
                if prior_key is not None:
                    raise ValidationError(
                        f"duplicate translation page {page}",
                        code="duplicate_translation_page",
                        details={
                            "language": language,
                            "member": name,
                            "keys": [prior_key, str(raw_page)],
                        },
                    )
                logical_pages[page] = str(raw_page)
                if page not in applied_pages:
                    continue
                if isinstance(raw_text, str):
                    text = raw_text
                elif isinstance(raw_text, dict):
                    if isinstance(raw_text.get("_page"), str):
                        text = raw_text["_page"]
                    else:
                        text = "\n\n".join(
                            str(raw_text[key])
                            for key in sorted(raw_text)
                            if isinstance(raw_text[key], str)
                            and raw_text[key].strip()
                        )
                else:
                    text = ""
                text = text.strip()
                if len(text) > self._limits.max_instructions_chars:
                    warn(
                        f"{name}/pages/{raw_page}",
                        "translation text truncated to the character limit",
                    )
                    text = text[: self._limits.max_instructions_chars]
                if text:
                    owner_key = (language, page)
                    previous_owner = owners.get(owner_key)
                    if previous_owner is not None:
                        raise ValidationError(
                            f"duplicate translation page {page}",
                            code="duplicate_translation_page",
                            details={
                                "language": language,
                                "members": [previous_owner[0], name],
                                "keys": [previous_owner[1], str(raw_page)],
                            },
                        )
                    owners[owner_key] = (name, str(raw_page))
                    language_pages[page] = text

        result: list[LibTranslationImport] = []
        for language, pages in sorted(collected.items()):
            existing = set(destination.translation_pages.get(language, ()))
            for page, text in sorted(pages.items()):
                if overwrite or page not in existing:
                    result.append(LibTranslationImport(language, page, text))
                else:
                    warn(
                        f"translations/{language}.json/pages/{page}",
                        "translation skipped: the destination already has this "
                        "page (import with overwrite to replace)",
                    )
        return result

    def _document_for_source(
        self,
        raw_document: str,
        destination: ImportDestinationSnapshot,
        *,
        source_id: str,
    ) -> str:
        document = self._sanitize_document_name(raw_document)
        binding = next(
            (
                (name, owner)
                for name, owner in destination.document_sources.items()
                if name.casefold() == document.casefold()
            ),
            None,
        )
        bound_source = binding[1] if binding is not None else None
        if binding is not None and bound_source == source_id:
            document = binding[0]
        needs_source_scope = (
            source_id != "primary" and document.casefold() == "compiled.txt"
        ) or (bound_source is not None and bound_source != source_id)
        if needs_source_scope:
            stem = document[:-4] if document.casefold().endswith(".txt") else document
            document = self._sanitize_document_name(f"{stem}-{source_id}.txt")
            binding = next(
                (
                    (name, owner)
                    for name, owner in destination.document_sources.items()
                    if name.casefold() == document.casefold()
                ),
                None,
            )
            bound_source = binding[1] if binding is not None else None
            if binding is not None and bound_source == source_id:
                document = binding[0]
        if bound_source is not None and bound_source != source_id:
            raise ConflictError(
                "the imported document belongs to another source",
                code="document_source_conflict",
                details={
                    "document": document,
                    "destination_source_id": bound_source,
                    "source_id": source_id,
                },
            )
        return document

    def _stylesheet(
        self,
        book: Mapping[str, Any],
        destination: ImportDestinationSnapshot,
        *,
        overwrite: bool,
        warn: Callable[[str, str], None],
    ) -> tuple[dict[str, Any] | None, str]:
        raw = book.get("stylesheet")
        if not isinstance(raw, dict):
            return None, "none"
        if len(raw) > 40:
            warn("book.json/stylesheet", "stylesheet dropped: more than 40 roles")
            return None, "none"
        styles = self._sanitize_styles(raw)
        if not styles:
            return None, "none"
        if destination.has_stylesheet and not overwrite:
            return None, "kept"
        return styles, "imported"

    def _manifest_ext(
        self,
        book: Mapping[str, Any],
        destination: ImportDestinationSnapshot,
        *,
        overwrite: bool,
        warn: Callable[[str, str], None],
    ) -> tuple[dict[str, Any] | None, str]:
        value = self._sanitize_ext(book.get("ext"), "book.json.ext", warn)
        if not value:
            return None, "none"
        if destination.has_manifest_ext and not overwrite:
            warn(
                "book.json/ext",
                "ext kept: destination already has one "
                "(import with overwrite to replace)",
            )
            return None, "kept"
        return value, "imported"

    def _instructions(
        self,
        book: Mapping[str, Any],
        destination: ImportDestinationSnapshot,
        *,
        overwrite: bool,
        warn: Callable[[str, str], None],
    ) -> tuple[str, str]:
        envelope = book.get("instructions")
        raw = envelope.get("book") if isinstance(envelope, dict) else ""
        if raw and not isinstance(raw, str):
            warn("book.json/instructions/book", "instructions ignored: not text")
            return "", "none"
        text = str(raw or "")
        if len(text) > self._limits.max_instructions_chars:
            warn(
                "book.json/instructions/book",
                "instructions truncated to the character limit",
            )
            text = text[: self._limits.max_instructions_chars]
        if not text.strip():
            return "", "none"
        if destination.has_instructions and not overwrite:
            warn(
                "book.json/instructions/book",
                "instructions kept: destination already has guidance "
                "(import with overwrite to replace)",
            )
            return "", "kept"
        return text, "imported"


__all__ = ["ExistingItemLibArchivePlanner", "LibArchiveLimits"]
