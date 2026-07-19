"""Application boundary for importing portable ``.lib`` editions.

Archive parsing/sanitizing and durable storage are separate ports.  The service
owns command validation, operation idempotency, archive identity, and the rule
that planning happens against a destination snapshot held inside one unit of
work.  Filesystem paths never cross this boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, ContextManager, Literal, Protocol, TypeAlias

from .errors import ConflictError, EngineError, RepositoryError, ValidationError
from .item_commands import (
    ItemDraft,
    ItemMutationReceipt,
    ItemRecordSnapshot,
    create_item_command_sha256,
)


_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRANSLATION_LANGUAGE_RE = re.compile(
    r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$"
)

ImportDisposition: TypeAlias = Literal["none", "imported", "kept"]
_IMPORT_DISPOSITIONS = frozenset({"none", "imported", "kept"})


def _freeze_json(value: Any, *, path: str = "$", active: set[int] | None = None) -> Any:
    """Clone strict JSON values into immutable containers."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValueError(f"{path} contains a non-finite number")
    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path} contains a non-string object key")
                frozen[key] = _freeze_json(item, path=f"{path}.{key}", active=active)
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{path} contains a reference cycle")
        active.add(identity)
        try:
            return tuple(
                _freeze_json(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise TypeError(f"{path} contains non-JSON value {type(value).__name__}")


def _freeze_mapping(value: Mapping[str, Any], *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be an object")
    frozen = _freeze_json(value, path=path)
    assert isinstance(frozen, Mapping)
    return frozen


def _typed_tuple(values: Any, expected: type, *, field_name: str) -> tuple[Any, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable") from exc
    if any(not isinstance(value, expected) for value in result):
        raise TypeError(f"{field_name} contains an invalid value")
    return result


def _string_tuple(values: Any, *, field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable") from exc
    if any(not isinstance(value, str) for value in result):
        raise TypeError(f"{field_name} contains a non-string value")
    return result


def _positive_int_tuple(values: Any, *, field_name: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable") from exc
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in result
    ):
        raise ValueError(f"{field_name} contains an invalid page number")
    return result


def _portable_identifier(value: Any, *, field_name: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"{field_name} is not a portable identifier")
    return value


def _document_name(value: Any, *, field_name: str = "document") -> str:
    result = _portable_identifier(value, field_name=field_name, maximum=255)
    if result in {".", ".."}:
        raise ValueError(f"{field_name} is not a portable document name")
    return result


def _normalize_disposition(
    value: ImportDisposition | None,
    *,
    payload_present: bool,
    field_name: str,
) -> ImportDisposition:
    # ``None`` is the compatibility input for plans created before explicit
    # disposition fields existed. The stored plan is always normalized.
    if value is None:
        return "imported" if payload_present else "none"
    if not isinstance(value, str) or value not in _IMPORT_DISPOSITIONS:
        raise ValueError(f"{field_name} is not a valid import disposition")
    return value


def _semantic_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _lib_import_command_sha256(
    *,
    item_id: str,
    source_id: str,
    overwrite: bool,
    archive_sha256: str,
) -> str:
    return _semantic_sha256(
        {
            "archive_sha256": archive_sha256,
            "item_id": item_id,
            "overwrite": overwrite,
            "source_id": source_id,
        }
    )


def _open_lib_command_sha256(archive_sha256: str) -> str:
    return _semantic_sha256(
        {
            "action": "open-lib",
            "archive_sha256": archive_sha256,
            "source_id": "primary",
        }
    )


@dataclass(frozen=True, slots=True)
class ImportWarning:
    location: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.location, str) or not isinstance(self.message, str):
            raise TypeError("import warning fields must be strings")

    def as_dict(self) -> dict[str, str]:
        return {"location": self.location, "message": self.message}


@dataclass(frozen=True, slots=True)
class ImportLibCommand:
    item_id: str
    source_id: str
    archive: bytes
    overwrite: bool = False
    operation_id: str = ""


@dataclass(frozen=True, slots=True)
class OpenLibCommand:
    """Open an archive as a newly allocated catalogue item.

    ``source_path`` is optional display/provenance context for callers.  It is
    deliberately excluded from command identity: moving the same archive does
    not turn a retry into a different semantic operation.
    """

    archive: bytes
    operation_id: str
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class ImportDestinationSnapshot:
    item_id: str
    revision: str = ""
    book_id: str = ""
    source_ids: tuple[str, ...] = ("primary",)
    pages: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    region_ids: Mapping[str, Mapping[int, tuple[str, ...]]] = field(
        default_factory=dict
    )
    templates: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    translation_pages: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    instructions: str = ""
    document_sources: Mapping[str, str] = field(default_factory=dict)
    has_stylesheet: bool = False
    has_manifest_ext: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str) or not self.item_id.strip():
            raise ValueError("item_id must be a non-empty string")
        if not isinstance(self.revision, str) or not isinstance(self.book_id, str):
            raise TypeError("revision and book_id must be strings")
        source_ids = _string_tuple(self.source_ids, field_name="source_ids")
        for source_id in source_ids:
            _portable_identifier(source_id, field_name="source id")
        if len({source_id.casefold() for source_id in source_ids}) != len(source_ids):
            raise ValueError("source_ids contains a duplicate source or alias")
        object.__setattr__(self, "source_ids", source_ids)

        if not isinstance(self.pages, Mapping):
            raise TypeError("pages must be an object keyed by page number")
        pages: dict[int, Mapping[str, Any]] = {}
        for page, record in self.pages.items():
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError("pages contains an invalid page number")
            pages[page] = _freeze_mapping(record, path=f"$.pages[{page}]")
        object.__setattr__(self, "pages", MappingProxyType(pages))

        if not isinstance(self.region_ids, Mapping):
            raise TypeError("region_ids must be an object keyed by source id")
        owned_region_ids: set[str] = set()
        regions: dict[str, Mapping[int, tuple[str, ...]]] = {}
        for source_id, raw_pages in self.region_ids.items():
            if not isinstance(source_id, str):
                raise TypeError("region_ids source keys must be strings")
            if source_id not in source_ids:
                raise ValueError("region_ids names an unknown source")
            if not isinstance(raw_pages, Mapping):
                raise TypeError("region_ids source values must be page objects")
            source_pages: dict[int, tuple[str, ...]] = {}
            for page, raw_ids in raw_pages.items():
                if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                    raise ValueError("region_ids contains an invalid page number")
                region_ids = _string_tuple(
                    raw_ids,
                    field_name=f"region_ids[{source_id!r}][{page}]",
                )
                for region_id in region_ids:
                    _portable_identifier(region_id, field_name="region id")
                    if region_id in owned_region_ids:
                        raise ValueError(
                            "a region identity is owned by more than one location"
                        )
                    owned_region_ids.add(region_id)
                source_pages[page] = region_ids
            regions[source_id] = MappingProxyType(source_pages)
        object.__setattr__(self, "region_ids", MappingProxyType(regions))

        templates = _string_tuple(self.templates, field_name="templates")
        figures = _string_tuple(self.figures, field_name="figures")
        for field_name, values in (("templates", templates), ("figures", figures)):
            folded: set[str] = set()
            for value in values:
                _document_name(value, field_name=field_name)
                normalized = value.casefold()
                if normalized in folded:
                    raise ValueError(f"{field_name} contains a duplicate name")
                folded.add(normalized)
        object.__setattr__(self, "templates", templates)
        object.__setattr__(self, "figures", figures)
        if not isinstance(self.translation_pages, Mapping):
            raise TypeError("translation_pages must be an object")
        translations: dict[str, tuple[int, ...]] = {}
        folded_languages: set[str] = set()
        for language, raw_pages in self.translation_pages.items():
            if not isinstance(language, str):
                raise TypeError("translation_pages keys must be strings")
            if not _TRANSLATION_LANGUAGE_RE.fullmatch(language):
                raise ValueError(
                    "translation_pages contains a non-canonical language"
                )
            folded_language = language.casefold()
            if folded_language in folded_languages:
                raise ValueError("translation_pages contains a language alias")
            folded_languages.add(folded_language)
            pages_for_language = tuple(raw_pages)
            if any(
                isinstance(page, bool) or not isinstance(page, int) or page < 1
                for page in pages_for_language
            ):
                raise ValueError("translation_pages contains an invalid page")
            if len(pages_for_language) != len(set(pages_for_language)):
                raise ValueError("translation_pages contains a duplicate page")
            translations[language] = pages_for_language
        object.__setattr__(self, "translation_pages", MappingProxyType(translations))

        if not isinstance(self.instructions, str):
            raise TypeError("instructions must be a string")
        if not isinstance(self.document_sources, Mapping):
            raise TypeError("document_sources must be an object")
        document_sources: dict[str, str] = {}
        folded_documents: set[str] = set()
        for document, source_id in self.document_sources.items():
            document_name = _document_name(document)
            folded_document = document_name.casefold()
            if folded_document in folded_documents:
                raise ValueError("document_sources contains a document alias")
            folded_documents.add(folded_document)
            if not isinstance(source_id, str):
                raise TypeError("document source ids must be strings")
            if source_id not in source_ids:
                raise ValueError("document_sources names an unknown source")
            document_sources[document_name] = source_id
        object.__setattr__(
            self, "document_sources", MappingProxyType(document_sources)
        )
        if not isinstance(self.has_stylesheet, bool) or not isinstance(
            self.has_manifest_ext, bool
        ):
            raise TypeError("destination presence flags must be booleans")

    @property
    def has_instructions(self) -> bool:
        return bool(self.instructions)


@dataclass(frozen=True, slots=True)
class LibPageImport:
    page: int
    record: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            isinstance(self.page, bool)
            or not isinstance(self.page, int)
            or self.page < 1
        ):
            raise ValueError("page must be a positive integer")
        object.__setattr__(
            self, "record", _freeze_mapping(self.record, path="$.page.record")
        )


@dataclass(frozen=True, slots=True)
class LibTemplateImport:
    name: str
    record: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "record", _freeze_mapping(self.record, path="$.template.record")
        )


@dataclass(frozen=True, slots=True)
class LibFigureImport:
    name: str
    content: bytes
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("figure content must be bytes")
        object.__setattr__(
            self,
            "metadata",
            _freeze_mapping(self.metadata, path="$.figure.metadata"),
        )


@dataclass(frozen=True, slots=True)
class LibTranslationImport:
    language: str
    page: int
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.language, str) or not isinstance(self.text, str):
            raise TypeError("translation language and text must be strings")
        if (
            isinstance(self.page, bool)
            or not isinstance(self.page, int)
            or self.page < 1
        ):
            raise ValueError("translation page must be a positive integer")


@dataclass(frozen=True, slots=True)
class LibCompiledPageImport:
    """One deterministic page update to a destination compiled document."""

    document: str
    source_id: str
    page: int
    text: str

    def __post_init__(self) -> None:
        _document_name(self.document)
        _portable_identifier(self.source_id, field_name="compiled source id")
        if not isinstance(self.text, str):
            raise TypeError("compiled page text must be a string")
        if (
            isinstance(self.page, bool)
            or not isinstance(self.page, int)
            or self.page < 1
        ):
            raise ValueError("compiled page must be a positive integer")


@dataclass(frozen=True, slots=True)
class LibImportPlan:
    archive_sha256: str
    format_version: str
    incoming_book_id: str = ""
    manifest_metadata: Mapping[str, Any] = field(default_factory=dict)
    pages: tuple[LibPageImport, ...] = ()
    pages_skipped: tuple[int, ...] = ()
    pages_protected: tuple[int, ...] = ()
    templates: tuple[LibTemplateImport, ...] = ()
    figures: tuple[LibFigureImport, ...] = ()
    translations: tuple[LibTranslationImport, ...] = ()
    compiled_pages: tuple[LibCompiledPageImport, ...] = ()
    stylesheet: Mapping[str, Any] | None = None
    manifest_ext: Mapping[str, Any] | None = None
    instructions: str = ""
    stylesheet_disposition: ImportDisposition | None = None
    manifest_ext_disposition: ImportDisposition | None = None
    instructions_disposition: ImportDisposition | None = None
    warnings: tuple[ImportWarning, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "manifest_metadata",
            _freeze_mapping(
                self.manifest_metadata,
                path="$.manifest_metadata",
            ),
        )
        object.__setattr__(
            self, "pages", _typed_tuple(self.pages, LibPageImport, field_name="pages")
        )
        object.__setattr__(self, "pages_skipped", tuple(self.pages_skipped))
        object.__setattr__(self, "pages_protected", tuple(self.pages_protected))
        object.__setattr__(
            self,
            "templates",
            _typed_tuple(self.templates, LibTemplateImport, field_name="templates"),
        )
        object.__setattr__(
            self,
            "figures",
            _typed_tuple(self.figures, LibFigureImport, field_name="figures"),
        )
        object.__setattr__(
            self,
            "translations",
            _typed_tuple(
                self.translations,
                LibTranslationImport,
                field_name="translations",
            ),
        )
        object.__setattr__(
            self,
            "compiled_pages",
            _typed_tuple(
                self.compiled_pages,
                LibCompiledPageImport,
                field_name="compiled_pages",
            ),
        )
        if self.stylesheet is not None:
            object.__setattr__(
                self,
                "stylesheet",
                _freeze_mapping(self.stylesheet, path="$.stylesheet"),
            )
        if self.manifest_ext is not None:
            object.__setattr__(
                self,
                "manifest_ext",
                _freeze_mapping(self.manifest_ext, path="$.manifest_ext"),
            )
        object.__setattr__(
            self,
            "stylesheet_disposition",
            _normalize_disposition(
                self.stylesheet_disposition,
                payload_present=self.stylesheet is not None,
                field_name="stylesheet_disposition",
            ),
        )
        object.__setattr__(
            self,
            "manifest_ext_disposition",
            _normalize_disposition(
                self.manifest_ext_disposition,
                payload_present=self.manifest_ext is not None,
                field_name="manifest_ext_disposition",
            ),
        )
        object.__setattr__(
            self,
            "instructions_disposition",
            _normalize_disposition(
                self.instructions_disposition,
                payload_present=bool(self.instructions),
                field_name="instructions_disposition",
            ),
        )
        object.__setattr__(
            self,
            "warnings",
            _typed_tuple(self.warnings, ImportWarning, field_name="warnings"),
        )


@dataclass(frozen=True, slots=True)
class LibImportReceipt:
    operation_id: str
    archive_sha256: str
    command_sha256: str
    item_id: str
    source_id: str
    overwrite: bool
    format_version: str
    pages_applied: tuple[int, ...] = ()
    pages_skipped: tuple[int, ...] = ()
    pages_protected: tuple[int, ...] = ()
    templates_added: tuple[str, ...] = ()
    figures_added: tuple[str, ...] = ()
    translations_added: tuple[str, ...] = ()
    compiled_pages: tuple[int, ...] = ()
    documents_updated: tuple[str, ...] = ()
    stylesheet_disposition: ImportDisposition = "none"
    manifest_ext_disposition: ImportDisposition = "none"
    instructions_disposition: ImportDisposition = "none"
    warnings: tuple[ImportWarning, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "operation_id",
            "archive_sha256",
            "command_sha256",
            "item_id",
            "source_id",
            "format_version",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")
        if not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise ValueError("operation_id is not a portable operation token")
        if not _SHA256_RE.fullmatch(self.archive_sha256):
            raise ValueError("archive_sha256 must be a lowercase SHA-256 digest")
        if not _SHA256_RE.fullmatch(self.command_sha256):
            raise ValueError("command_sha256 must be a lowercase SHA-256 digest")
        if not self.item_id or not self.source_id or not self.format_version:
            raise ValueError("receipt identity and format fields must not be empty")
        _portable_identifier(self.item_id, field_name="item_id")
        _portable_identifier(self.source_id, field_name="source_id")
        if self.format_version != self.format_version.strip():
            raise ValueError("format_version must not contain surrounding whitespace")
        if not isinstance(self.overwrite, bool):
            raise TypeError("overwrite must be a boolean")

        for field_name in (
            "pages_applied",
            "pages_skipped",
            "pages_protected",
            "compiled_pages",
        ):
            values = _positive_int_tuple(
                getattr(self, field_name), field_name=field_name
            )
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} contains a duplicate page")
            object.__setattr__(self, field_name, values)
        for field_name in (
            "templates_added",
            "figures_added",
            "translations_added",
            "documents_updated",
        ):
            values = _string_tuple(getattr(self, field_name), field_name=field_name)
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} contains a duplicate value")
            object.__setattr__(self, field_name, values)

        page_groups = (
            set(self.pages_applied),
            set(self.pages_skipped),
            set(self.pages_protected),
        )
        if (
            page_groups[0] & page_groups[1]
            or page_groups[0] & page_groups[2]
            or page_groups[1] & page_groups[2]
        ):
            raise ValueError("receipt page dispositions overlap")
        if not set(self.compiled_pages).issubset(page_groups[0]):
            raise ValueError("compiled_pages must be a subset of pages_applied")

        for field_name in ("templates_added", "figures_added"):
            for value in getattr(self, field_name):
                _document_name(value, field_name=field_name)
        for value in self.documents_updated:
            _document_name(value, field_name="documents_updated")
        for value in self.translations_added:
            if not _TRANSLATION_LANGUAGE_RE.fullmatch(value):
                raise ValueError("translations_added contains an invalid language")
        for field_name in (
            "stylesheet_disposition",
            "manifest_ext_disposition",
            "instructions_disposition",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or value not in _IMPORT_DISPOSITIONS:
                raise ValueError(f"{field_name} is not a valid disposition")
        object.__setattr__(
            self,
            "warnings",
            _typed_tuple(self.warnings, ImportWarning, field_name="warnings"),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LibImportReceipt":
        """Rehydrate one persisted receipt without coercing untrusted data."""

        if not isinstance(value, Mapping):
            raise TypeError("an import receipt must be an object")
        if any(not isinstance(key, str) for key in value):
            raise TypeError("import receipt field names must be strings")
        fields = {
            "operation_id",
            "archive_sha256",
            "command_sha256",
            "item_id",
            "source_id",
            "overwrite",
            "format_version",
            "pages_applied",
            "pages_skipped",
            "pages_protected",
            "templates_added",
            "figures_added",
            "translations_added",
            "compiled_pages",
            "documents_updated",
            "stylesheet_disposition",
            "manifest_ext_disposition",
            "instructions_disposition",
            "warnings",
        }
        supplied = set(value)
        if supplied != fields:
            missing = sorted(fields - supplied)
            extra = sorted(supplied - fields)
            raise ValueError(
                f"import receipt fields do not match the schema; "
                f"missing={missing}, extra={extra}"
            )
        array_fields = (
            "pages_applied",
            "pages_skipped",
            "pages_protected",
            "templates_added",
            "figures_added",
            "translations_added",
            "compiled_pages",
            "documents_updated",
            "warnings",
        )
        for field_name in array_fields:
            if not isinstance(value[field_name], list):
                raise TypeError(f"{field_name} must be a JSON array")
        warning_values: list[ImportWarning] = []
        for warning in value["warnings"]:
            if not isinstance(warning, Mapping):
                raise TypeError("warnings must contain objects")
            if set(warning) != {"location", "message"}:
                raise ValueError("an import warning has invalid fields")
            warning_values.append(
                ImportWarning(warning["location"], warning["message"])
            )
        return cls(
            operation_id=value["operation_id"],
            archive_sha256=value["archive_sha256"],
            command_sha256=value["command_sha256"],
            item_id=value["item_id"],
            source_id=value["source_id"],
            overwrite=value["overwrite"],
            format_version=value["format_version"],
            pages_applied=tuple(value["pages_applied"]),
            pages_skipped=tuple(value["pages_skipped"]),
            pages_protected=tuple(value["pages_protected"]),
            templates_added=tuple(value["templates_added"]),
            figures_added=tuple(value["figures_added"]),
            translations_added=tuple(value["translations_added"]),
            compiled_pages=tuple(value["compiled_pages"]),
            documents_updated=tuple(value["documents_updated"]),
            stylesheet_disposition=value["stylesheet_disposition"],
            manifest_ext_disposition=value["manifest_ext_disposition"],
            instructions_disposition=value["instructions_disposition"],
            warnings=tuple(warning_values),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "archive_sha256": self.archive_sha256,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "source_id": self.source_id,
            "overwrite": self.overwrite,
            "format_version": self.format_version,
            "pages_applied": list(self.pages_applied),
            "pages_skipped": list(self.pages_skipped),
            "pages_protected": list(self.pages_protected),
            "templates_added": list(self.templates_added),
            "figures_added": list(self.figures_added),
            "translations_added": list(self.translations_added),
            "compiled_pages": list(self.compiled_pages),
            "documents_updated": list(self.documents_updated),
            "stylesheet_disposition": self.stylesheet_disposition,
            "manifest_ext_disposition": self.manifest_ext_disposition,
            "instructions_disposition": self.instructions_disposition,
            "warnings": [warning.as_dict() for warning in self.warnings],
        }


@dataclass(frozen=True, slots=True)
class OpenLibReceipt:
    """Durable outcome of creating an item from one ``.lib`` archive."""

    operation_id: str
    archive_sha256: str
    command_sha256: str
    item_id: str
    item_receipt: ItemMutationReceipt
    import_receipt: LibImportReceipt

    def __post_init__(self) -> None:
        for field_name in (
            "operation_id",
            "archive_sha256",
            "command_sha256",
            "item_id",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")
        if not _OPERATION_ID_RE.fullmatch(self.operation_id):
            raise ValueError("operation_id is not a portable operation token")
        if not _SHA256_RE.fullmatch(self.archive_sha256):
            raise ValueError("archive_sha256 must be a lowercase SHA-256 digest")
        if not _SHA256_RE.fullmatch(self.command_sha256):
            raise ValueError("command_sha256 must be a lowercase SHA-256 digest")
        _portable_identifier(self.item_id, field_name="item_id")
        if not isinstance(self.item_receipt, ItemMutationReceipt):
            raise TypeError("item_receipt must be an ItemMutationReceipt")
        if not isinstance(self.import_receipt, LibImportReceipt):
            raise TypeError("import_receipt must be a LibImportReceipt")

        if self.command_sha256 != _open_lib_command_sha256(self.archive_sha256):
            raise ValueError("command_sha256 does not identify this open operation")
        if (
            self.item_receipt.operation_id != self.operation_id
            or self.import_receipt.operation_id != self.operation_id
        ):
            raise ValueError("nested receipt operation identity does not match")
        if (
            self.item_receipt.item_id != self.item_id
            or self.import_receipt.item_id != self.item_id
        ):
            raise ValueError("nested receipt item identity does not match")
        if self.item_receipt.action != "create":
            raise ValueError("item_receipt must describe item creation")
        if self.item_receipt.item is None:
            raise ValueError("item_receipt must contain the created item")
        expected_item_command = create_item_command_sha256(
            self.item_receipt.item.as_draft()
        )
        if self.item_receipt.command_sha256 != expected_item_command:
            raise ValueError("item_receipt command identity does not match")
        if self.import_receipt.archive_sha256 != self.archive_sha256:
            raise ValueError("import_receipt archive identity does not match")
        if self.import_receipt.source_id != "primary":
            raise ValueError("import_receipt must target the primary source")
        if self.import_receipt.overwrite:
            raise ValueError("import_receipt cannot overwrite a new item")
        expected_import_command = _lib_import_command_sha256(
            item_id=self.item_id,
            source_id="primary",
            overwrite=False,
            archive_sha256=self.archive_sha256,
        )
        if self.import_receipt.command_sha256 != expected_import_command:
            raise ValueError("import_receipt command identity does not match")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OpenLibReceipt":
        """Rehydrate a strict composite receipt from persisted JSON data."""

        if not isinstance(value, Mapping):
            raise TypeError("an open .lib receipt must be an object")
        if any(not isinstance(key, str) for key in value):
            raise TypeError("open .lib receipt field names must be strings")
        fields = {
            "operation_id",
            "archive_sha256",
            "command_sha256",
            "item_id",
            "item_receipt",
            "import_receipt",
        }
        supplied = set(value)
        if supplied != fields:
            missing = sorted(fields - supplied)
            extra = sorted(supplied - fields)
            raise ValueError(
                "open .lib receipt fields do not match the schema; "
                f"missing={missing}, extra={extra}"
            )
        return cls(
            operation_id=value["operation_id"],
            archive_sha256=value["archive_sha256"],
            command_sha256=value["command_sha256"],
            item_id=value["item_id"],
            item_receipt=ItemMutationReceipt.from_dict(value["item_receipt"]),
            import_receipt=LibImportReceipt.from_dict(value["import_receipt"]),
        )

    @property
    def item(self) -> ItemRecordSnapshot:
        item = self.item_receipt.item
        assert item is not None
        return item

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "archive_sha256": self.archive_sha256,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "item_receipt": self.item_receipt.as_dict(),
            "import_receipt": self.import_receipt.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class OpenLibResult:
    receipt: OpenLibReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, OpenLibReceipt):
            raise TypeError("receipt must be an OpenLibReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    @property
    def item_id(self) -> str:
        return self.receipt.item_id

    @property
    def item(self) -> ItemRecordSnapshot:
        return self.receipt.item

    @property
    def item_receipt(self) -> ItemMutationReceipt:
        return self.receipt.item_receipt

    @property
    def import_receipt(self) -> LibImportReceipt:
        return self.receipt.import_receipt

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_dict(),
        }


OpenLibDraftFactory: TypeAlias = Callable[[Mapping[str, Any]], ItemDraft]


class LibImportPlannerPort(Protocol):
    """Decode and sanitize an archive against a locked destination view."""

    def plan(
        self,
        archive: bytes,
        destination: ImportDestinationSnapshot,
        *,
        source_id: str,
        overwrite: bool,
        archive_sha256: str,
    ) -> LibImportPlan: ...


class InterchangeUnitOfWorkPort(Protocol):
    """One locked, recoverable import transaction.

    The repository holds the destination item lock from context entry through
    exit. ``destination`` is a detached immutable snapshot made under that
    lock. ``receipt`` returns committed receipts only; applying/rolled-back
    journal metadata is never success. ``apply`` stages every artifact without
    publishing live state. ``commit`` durably publishes the complete staged
    state and receipt as one recoverable operation. Exiting with an exception
    discards or rolls back all staged/live changes before releasing the lock.
    """

    @property
    def destination(self) -> ImportDestinationSnapshot: ...

    def receipt(self, operation_id: str) -> LibImportReceipt | None: ...

    def apply(self, plan: LibImportPlan) -> None: ...

    def commit(self, receipt: LibImportReceipt) -> None: ...


class InterchangeRepositoryPort(Protocol):
    """Open item-scoped units of work with the semantics documented above."""

    def unit_of_work(
        self,
        item_id: str,
        *,
        source_id: str,
        operation_id: str,
    ) -> ContextManager[InterchangeUnitOfWorkPort]: ...


class OpenLibUnitOfWorkPort(Protocol):
    """One atomic catalogue-create and archive-import transaction.

    The repository holds a stable catalogue/workspace lock through context
    exit.  Staging methods must not publish live state.  ``commit`` publishes
    the catalogue record, imported entry artifacts, and composite receipt as
    one recoverable outcome; context exit without a successful commit rolls
    every staged or partially applied change back.
    """

    def receipt(self, operation_id: str) -> OpenLibReceipt | None: ...

    def allocate_item_id(self) -> str: ...

    def pristine_destination(
        self,
        item_id: str,
    ) -> ImportDestinationSnapshot: ...

    def stage_item_create(
        self,
        item_id: str,
        draft: ItemDraft,
    ) -> ItemRecordSnapshot: ...

    def apply(self, plan: LibImportPlan) -> None: ...

    def commit(self, receipt: OpenLibReceipt) -> None: ...


class OpenLibRepositoryPort(Protocol):
    """Open operation-scoped composite units of work."""

    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> ContextManager[OpenLibUnitOfWorkPort]: ...


class LibInterchangeService:
    """Plan and commit one idempotent import into an existing item."""

    def __init__(
        self,
        planner: LibImportPlannerPort,
        repository: InterchangeRepositoryPort,
    ) -> None:
        self._planner = planner
        self._repository = repository

    def import_lib(self, command: ImportLibCommand) -> LibImportReceipt:
        item_id = str(command.item_id or "").strip()
        source_id = str(command.source_id or "").strip()
        operation_id = str(command.operation_id or "").strip()
        if not item_id:
            raise ValidationError("item id is required", code="item_id_required")
        if not source_id:
            raise ValidationError("source id is required", code="source_id_required")
        try:
            _portable_identifier(item_id, field_name="item id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "item id is not a portable identifier",
                code="invalid_item_id",
            ) from exc
        try:
            _portable_identifier(source_id, field_name="source id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "source id is not a portable identifier",
                code="invalid_source_id",
            ) from exc
        if not isinstance(command.overwrite, bool):
            raise ValidationError(
                "overwrite must be a boolean",
                code="invalid_overwrite",
            )
        if not _OPERATION_ID_RE.fullmatch(operation_id):
            raise ValidationError(
                "operation id must be a portable non-empty token",
                code="invalid_operation_id",
            )
        if not isinstance(command.archive, bytes) or not command.archive:
            raise ValidationError(
                "a non-empty .lib archive is required",
                code="archive_required",
            )
        archive_sha256 = hashlib.sha256(command.archive).hexdigest()
        command_sha256 = self.command_sha256(
            item_id=item_id,
            source_id=source_id,
            overwrite=bool(command.overwrite),
            archive_sha256=archive_sha256,
        )
        with self._repository.unit_of_work(
            item_id,
            source_id=source_id,
            operation_id=operation_id,
        ) as unit:
            prior = unit.receipt(operation_id)
            if prior is not None:
                if not isinstance(prior, LibImportReceipt):
                    raise RepositoryError(
                        "interchange repository returned an invalid receipt",
                        code="invalid_import_receipt",
                    )
                if prior.operation_id != operation_id:
                    raise RepositoryError(
                        "interchange repository returned another operation receipt",
                        code="receipt_scope_mismatch",
                    )
                if prior.item_id != item_id:
                    raise RepositoryError(
                        "interchange repository returned a receipt for another item",
                        code="receipt_scope_mismatch",
                    )
                if prior.command_sha256 != command_sha256:
                    raise ConflictError(
                        "operation id was already used for another import command",
                        code="operation_id_conflict",
                        details={
                            "operation_id": operation_id,
                            "item_id": item_id,
                        },
                    )
                return prior
            destination = unit.destination
            if not isinstance(destination, ImportDestinationSnapshot):
                raise RepositoryError(
                    "interchange repository returned an invalid destination",
                    code="invalid_import_destination",
                )
            if destination.item_id != item_id:
                raise RepositoryError(
                    "interchange repository returned the wrong destination",
                    code="destination_mismatch",
                    details={
                        "requested_item_id": item_id,
                        "destination_item_id": destination.item_id,
                    },
                )
            if source_id not in destination.source_ids:
                raise ValidationError(
                    "the destination item has no such source",
                    code="unknown_source_id",
                    details={"item_id": item_id, "source_id": source_id},
                )
            plan = self._planner.plan(
                command.archive,
                destination,
                source_id=source_id,
                overwrite=bool(command.overwrite),
                archive_sha256=archive_sha256,
            )
            if not isinstance(plan, LibImportPlan):
                raise RepositoryError(
                    "interchange planner returned an invalid plan",
                    code="invalid_import_plan",
                )
            if plan.archive_sha256 != archive_sha256:
                raise RepositoryError(
                    "interchange planner returned the wrong archive identity",
                    code="archive_identity_mismatch",
                )
            if (
                destination.book_id
                and plan.incoming_book_id
                and destination.book_id != plan.incoming_book_id
            ):
                raise ConflictError(
                    "the .lib archive belongs to a different item",
                    code="book_identity_mismatch",
                    details={
                        "item_id": item_id,
                        "destination_book_id": destination.book_id,
                        "incoming_book_id": plan.incoming_book_id,
                    },
                )
            self.validate_plan(
                plan,
                destination=destination,
                source_id=source_id,
                overwrite=bool(command.overwrite),
            )
            receipt = self.receipt_for_plan(
                operation_id,
                destination.item_id,
                source_id,
                bool(command.overwrite),
                command_sha256,
                plan,
            )
            unit.apply(plan)
            unit.commit(receipt)
            return receipt

    @staticmethod
    def validate_plan(
        plan: LibImportPlan,
        *,
        destination: ImportDestinationSnapshot,
        source_id: str,
        overwrite: bool,
    ) -> None:
        """Reject malformed plugin output before any adapter can stage it."""

        def invalid(reason: str, **details: Any) -> None:
            raise RepositoryError(
                "interchange planner returned an invalid plan",
                code="invalid_import_plan",
                details={"reason": reason, **details},
            )

        if not isinstance(plan.format_version, str) or not plan.format_version.strip():
            invalid("format_version_required")
        if not isinstance(plan.incoming_book_id, str):
            invalid("incoming_book_id_invalid")
        if not isinstance(plan.instructions, str):
            invalid("instructions_invalid")

        dispositions = (
            (
                "stylesheet",
                plan.stylesheet_disposition,
                plan.stylesheet is not None,
                destination.has_stylesheet,
            ),
            (
                "manifest_ext",
                plan.manifest_ext_disposition,
                plan.manifest_ext is not None,
                destination.has_manifest_ext,
            ),
            (
                "instructions",
                plan.instructions_disposition,
                bool(plan.instructions),
                destination.has_instructions,
            ),
        )
        for name, disposition, payload_present, destination_present in dispositions:
            if disposition not in _IMPORT_DISPOSITIONS:
                invalid("invalid_disposition", field=name)
            if disposition == "imported" and not payload_present:
                invalid("missing_disposition_payload", field=name)
            if disposition != "imported" and payload_present:
                invalid("unexpected_disposition_payload", field=name)
            if disposition == "kept" and not destination_present:
                invalid("kept_artifact_missing", field=name)
            if disposition == "imported" and destination_present and not overwrite:
                invalid("overwrite_required", field=name)

        applied = tuple(value.page for value in plan.pages)
        skipped = tuple(plan.pages_skipped)
        protected = tuple(plan.pages_protected)
        for name, values in (
            ("pages", applied),
            ("pages_skipped", skipped),
            ("pages_protected", protected),
        ):
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in values
            ):
                invalid("page_number_invalid", field=name)
            if len(set(values)) != len(values):
                invalid("duplicate_page", field=name)
        overlap = (set(applied) & set(skipped)) | (set(applied) & set(protected))
        overlap |= set(skipped) & set(protected)
        if overlap:
            invalid("page_dispositions_overlap", pages=sorted(overlap))
        if not overwrite:
            existing_pages = sorted(set(applied) & set(destination.pages))
            if existing_pages:
                invalid(
                    "overwrite_required",
                    field="pages",
                    pages=existing_pages,
                )

        def validate_names(name: str, values: tuple[str, ...]) -> None:
            folded: set[str] = set()
            for value in values:
                if (
                    not isinstance(value, str)
                    or value != value.strip()
                    or not value
                    or len(value) > 255
                    or value in {".", ".."}
                    or "/" in value
                    or "\\" in value
                    or any(ord(character) < 32 for character in value)
                ):
                    invalid("portable_name_invalid", field=name)
                normalized = value.casefold()
                if normalized in folded:
                    invalid("duplicate_name", field=name, name=value)
                folded.add(normalized)

        validate_names("templates", tuple(value.name for value in plan.templates))
        validate_names("figures", tuple(value.name for value in plan.figures))
        if not overwrite:
            destination_templates = {
                value.casefold(): value for value in destination.templates
            }
            template_collisions = sorted(
                destination_templates[value.name.casefold()]
                for value in plan.templates
                if value.name.casefold() in destination_templates
            )
            if template_collisions:
                invalid(
                    "overwrite_required",
                    field="templates",
                    names=template_collisions,
                )
            destination_figures = {
                value.casefold(): value for value in destination.figures
            }
            figure_collisions = sorted(
                destination_figures[value.name.casefold()]
                for value in plan.figures
                if value.name.casefold() in destination_figures
            )
            if figure_collisions:
                invalid(
                    "overwrite_required",
                    field="figures",
                    names=figure_collisions,
                )
        for template in plan.templates:
            record = template.record
            document_value = record.get("doc")
            try:
                document = _document_name(document_value)
            except (TypeError, ValueError):
                invalid("template_document_invalid", template=template.name)
            items = record.get("items")
            if not isinstance(items, (list, tuple)) or any(
                not isinstance(item, Mapping) for item in items
            ):
                invalid("template_items_invalid", template=template.name)
            dims = record.get("dims")
            if dims is not None and not isinstance(dims, Mapping):
                invalid("template_dims_invalid", template=template.name)
            from_page = record.get("from_page")
            if (
                from_page is not None
                and (
                    isinstance(from_page, bool)
                    or not isinstance(from_page, int)
                    or from_page < 0
                )
            ):
                invalid("template_origin_page_invalid", template=template.name)
            binding = next(
                (
                    (name, owner)
                    for name, owner in destination.document_sources.items()
                    if name.casefold() == document.casefold()
                ),
                None,
            )
            if binding is not None and binding[1] != source_id:
                invalid(
                    "document_source_conflict",
                    document=binding[0],
                    destination_source_id=binding[1],
                    source_id=source_id,
                    template=template.name,
                )

        translation_keys: set[tuple[str, int]] = set()
        translation_pages: list[tuple[str, int]] = []
        for translation in plan.translations:
            language = translation.language
            if not isinstance(language, str) or not _TRANSLATION_LANGUAGE_RE.fullmatch(
                language
            ):
                invalid("translation_language_invalid")
            key = (language.casefold(), translation.page)
            if key in translation_keys:
                invalid(
                    "duplicate_translation_page",
                    language=language,
                    page=translation.page,
                )
            translation_keys.add(key)
            translation_pages.append((language, translation.page))
        applied_set = set(applied)
        for language, page in translation_pages:
            if page not in applied_set:
                invalid(
                    "translation_page_not_applied",
                    language=language,
                    page=page,
                )
        if not overwrite:
            destination_translations = {
                language.casefold(): set(pages)
                for language, pages in destination.translation_pages.items()
            }
            translation_collisions = sorted(
                (language, page)
                for language, page in translation_pages
                if page in destination_translations.get(language.casefold(), set())
            )
            if translation_collisions:
                invalid(
                    "overwrite_required",
                    field="translations",
                    translations=[
                        {"language": language, "page": page}
                        for language, page in translation_collisions
                    ],
                )

        compiled_pages: set[int] = set()
        page_records = {value.page: value.record for value in plan.pages}
        for compiled in plan.compiled_pages:
            if compiled.page not in set(applied):
                invalid("compiled_page_not_applied", page=compiled.page)
            if compiled.page in compiled_pages:
                invalid("duplicate_compiled_page", page=compiled.page)
            compiled_pages.add(compiled.page)
            if compiled.source_id != source_id:
                invalid(
                    "compiled_source_mismatch",
                    page=compiled.page,
                    source_id=compiled.source_id,
                )
            if compiled.source_id not in destination.source_ids:
                invalid("compiled_source_unknown", source_id=compiled.source_id)
            try:
                document = _document_name(compiled.document)
            except (TypeError, ValueError):
                invalid("compiled_document_invalid", page=compiled.page)
            bound_source = destination.document_sources.get(document)
            if bound_source is not None and bound_source != source_id:
                invalid(
                    "document_source_conflict",
                    document=document,
                    destination_source_id=bound_source,
                    source_id=source_id,
                )
            record_document = page_records[compiled.page].get("doc")
            if record_document != document:
                invalid(
                    "compiled_document_mismatch",
                    page=compiled.page,
                    document=document,
                    record_document=record_document,
                )
        incoming_region_ids: dict[str, int] = {}
        for page_import in plan.pages:
            items = page_import.record.get("items")
            if not isinstance(items, (list, tuple)) or not items:
                invalid("page_items_invalid", page=page_import.page)
            for index, item in enumerate(items):
                if not isinstance(item, Mapping):
                    invalid(
                        "page_items_invalid",
                        page=page_import.page,
                        index=index,
                    )
                region_id = item.get("rid")
                try:
                    normalized_id = _portable_identifier(
                        region_id, field_name="region id"
                    )
                except (TypeError, ValueError):
                    invalid(
                        "region_identity_invalid",
                        page=page_import.page,
                        index=index,
                    )
                if normalized_id in incoming_region_ids:
                    invalid(
                        "duplicate_region_identity",
                        region_id=normalized_id,
                        pages=sorted(
                            {incoming_region_ids[normalized_id], page_import.page}
                        ),
                    )
                incoming_region_ids[normalized_id] = page_import.page

        surviving_region_ids: dict[str, tuple[str, int]] = {}
        for owner_source, source_pages in destination.region_ids.items():
            for owner_page, region_ids in source_pages.items():
                if owner_source == source_id and owner_page in applied_set:
                    continue
                for region_id in region_ids:
                    surviving_region_ids[region_id] = (owner_source, owner_page)
        collisions = sorted(set(incoming_region_ids) & set(surviving_region_ids))
        if collisions:
            invalid(
                "region_identity_conflict",
                region_ids=collisions,
                owners={
                    region_id: {
                        "source_id": surviving_region_ids[region_id][0],
                        "page": surviving_region_ids[region_id][1],
                    }
                    for region_id in collisions
                },
            )
        missing_compiled_pages = sorted(applied_set - compiled_pages)
        if missing_compiled_pages:
            invalid("compiled_page_missing", pages=missing_compiled_pages)

    @staticmethod
    def receipt_for_plan(
        operation_id: str,
        item_id: str,
        source_id: str,
        overwrite: bool,
        command_sha256: str,
        plan: LibImportPlan,
    ) -> LibImportReceipt:
        languages = tuple(
            sorted({value.language.lower() for value in plan.translations})
        )
        return LibImportReceipt(
            operation_id=operation_id,
            archive_sha256=plan.archive_sha256,
            command_sha256=command_sha256,
            item_id=item_id,
            source_id=source_id,
            overwrite=overwrite,
            format_version=plan.format_version,
            pages_applied=tuple(sorted({value.page for value in plan.pages})),
            pages_skipped=tuple(sorted(set(plan.pages_skipped))),
            pages_protected=tuple(sorted(set(plan.pages_protected))),
            templates_added=tuple(sorted({value.name for value in plan.templates})),
            figures_added=tuple(sorted({value.name for value in plan.figures})),
            translations_added=languages,
            compiled_pages=tuple(
                sorted({value.page for value in plan.compiled_pages})
            ),
            documents_updated=tuple(
                sorted({value.document for value in plan.compiled_pages})
            ),
            stylesheet_disposition=plan.stylesheet_disposition,
            manifest_ext_disposition=plan.manifest_ext_disposition,
            instructions_disposition=plan.instructions_disposition,
            warnings=tuple(plan.warnings),
        )

    @staticmethod
    def command_sha256(
        *,
        item_id: str,
        source_id: str,
        overwrite: bool,
        archive_sha256: str,
    ) -> str:
        return _lib_import_command_sha256(
            item_id=item_id,
            source_id=source_id,
            overwrite=overwrite,
            archive_sha256=archive_sha256,
        )


class OpenLibService:
    """Create a catalogue item and import its archive in one unit of work."""

    SOURCE_ID = "primary"

    def __init__(
        self,
        planner: LibImportPlannerPort,
        repository: OpenLibRepositoryPort,
        draft_factory: OpenLibDraftFactory,
    ) -> None:
        if not callable(draft_factory):
            raise TypeError("draft_factory must be callable")
        self._planner = planner
        self._repository = repository
        self._draft_factory = draft_factory

    def open_lib(self, command: OpenLibCommand) -> OpenLibResult:
        if not isinstance(command, OpenLibCommand):
            raise ValidationError(
                "open requires an OpenLibCommand",
                code="invalid_open_lib_command",
            )
        if not isinstance(command.operation_id, str) or not _OPERATION_ID_RE.fullmatch(
            command.operation_id
        ):
            raise ValidationError(
                "operation id must be a portable non-empty token",
                code="invalid_operation_id",
            )
        if not isinstance(command.archive, bytes) or not command.archive:
            raise ValidationError(
                "a non-empty .lib archive is required",
                code="archive_required",
            )
        if not isinstance(command.source_path, str):
            raise ValidationError(
                "source path context must be a string",
                code="invalid_source_path",
            )

        operation_id = command.operation_id
        archive_sha256 = hashlib.sha256(command.archive).hexdigest()
        command_sha256 = self.command_sha256(archive_sha256)
        try:
            with self._repository.unit_of_work(
                operation_id=operation_id
            ) as unit:
                prior = unit.receipt(operation_id)
                if prior is not None:
                    if not isinstance(prior, OpenLibReceipt):
                        raise RepositoryError(
                            "open .lib repository returned an invalid receipt",
                            code="invalid_open_lib_receipt",
                        )
                    if prior.operation_id != operation_id:
                        raise RepositoryError(
                            "open .lib repository returned another operation receipt",
                            code="receipt_scope_mismatch",
                        )
                    if prior.command_sha256 != command_sha256:
                        raise ConflictError(
                            "operation id was already used for another open command",
                            code="operation_id_conflict",
                            details={"operation_id": operation_id},
                        )
                    return OpenLibResult(prior, replayed=True)

                item_id = self._allocated_item_id(unit.allocate_item_id())
                destination = unit.pristine_destination(item_id)
                self._validate_pristine_destination(
                    destination,
                    item_id=item_id,
                )
                plan = self._planner.plan(
                    command.archive,
                    destination,
                    source_id=self.SOURCE_ID,
                    overwrite=False,
                    archive_sha256=archive_sha256,
                )
                if not isinstance(plan, LibImportPlan):
                    raise RepositoryError(
                        "interchange planner returned an invalid plan",
                        code="invalid_import_plan",
                    )
                if plan.archive_sha256 != archive_sha256:
                    raise RepositoryError(
                        "interchange planner returned the wrong archive identity",
                        code="archive_identity_mismatch",
                    )
                LibInterchangeService.validate_plan(
                    plan,
                    destination=destination,
                    source_id=self.SOURCE_ID,
                    overwrite=False,
                )

                draft = self._draft_factory(plan.manifest_metadata)
                if not isinstance(draft, ItemDraft):
                    raise RepositoryError(
                        "open .lib draft factory returned an invalid draft",
                        code="invalid_open_lib_draft",
                    )
                staged = unit.stage_item_create(item_id, draft)
                self._validate_staged_item(
                    staged,
                    item_id=item_id,
                    draft=draft,
                )
                item_receipt = ItemMutationReceipt(
                    action="create",
                    operation_id=operation_id,
                    command_sha256=create_item_command_sha256(draft),
                    item_id=item_id,
                    after_revision=staged.revision,
                    item=staged,
                )
                import_command_sha256 = LibInterchangeService.command_sha256(
                    item_id=item_id,
                    source_id=self.SOURCE_ID,
                    overwrite=False,
                    archive_sha256=archive_sha256,
                )
                import_receipt = LibInterchangeService.receipt_for_plan(
                    operation_id,
                    item_id,
                    self.SOURCE_ID,
                    False,
                    import_command_sha256,
                    plan,
                )
                receipt = OpenLibReceipt(
                    operation_id=operation_id,
                    archive_sha256=archive_sha256,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    item_receipt=item_receipt,
                    import_receipt=import_receipt,
                )
                unit.apply(plan)
                unit.commit(receipt)
                return OpenLibResult(receipt, replayed=False)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    @staticmethod
    def command_sha256(archive_sha256: str) -> str:
        if not isinstance(archive_sha256, str) or not _SHA256_RE.fullmatch(
            archive_sha256
        ):
            raise ValueError("archive_sha256 must be a lowercase SHA-256 digest")
        return _open_lib_command_sha256(archive_sha256)

    @staticmethod
    def _allocated_item_id(value: Any) -> str:
        try:
            return _portable_identifier(value, field_name="allocated item id")
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "open .lib repository allocated an invalid identity",
                code="invalid_allocated_item_id",
            ) from exc

    @staticmethod
    def _validate_pristine_destination(
        destination: Any,
        *,
        item_id: str,
    ) -> None:
        if not isinstance(destination, ImportDestinationSnapshot):
            raise RepositoryError(
                "open .lib repository returned an invalid destination",
                code="invalid_import_destination",
            )
        if destination.item_id != item_id:
            raise RepositoryError(
                "open .lib repository returned the wrong destination",
                code="destination_mismatch",
                details={
                    "requested_item_id": item_id,
                    "destination_item_id": destination.item_id,
                },
            )
        pristine = (
            not destination.revision
            and not destination.book_id
            and destination.source_ids == ("primary",)
            and not destination.pages
            and not any(destination.region_ids.values())
            and not destination.templates
            and not destination.figures
            and not destination.translation_pages
            and not destination.instructions
            and not destination.document_sources
            and not destination.has_stylesheet
            and not destination.has_manifest_ext
        )
        if not pristine:
            raise RepositoryError(
                "open .lib repository returned a non-pristine destination",
                code="non_pristine_open_lib_destination",
                details={"item_id": item_id},
            )

    @staticmethod
    def _validate_staged_item(
        staged: Any,
        *,
        item_id: str,
        draft: ItemDraft,
    ) -> None:
        if not isinstance(staged, ItemRecordSnapshot):
            raise RepositoryError(
                "open .lib repository returned an invalid item snapshot",
                code="invalid_item_record_snapshot",
            )
        if staged.item_id != item_id:
            raise RepositoryError(
                "open .lib repository staged another item",
                code="item_repository_scope_mismatch",
                details={
                    "requested_item_id": item_id,
                    "returned_item_id": staged.item_id,
                },
            )
        if staged.as_draft() != draft:
            raise RepositoryError(
                "open .lib repository changed canonical item content",
                code="item_repository_content_mismatch",
                details={"item_id": item_id},
            )

    @staticmethod
    def _repository_failure(exc: Exception) -> RepositoryError:
        return RepositoryError(
            "the open .lib repository failed",
            code="open_lib_repository_unavailable",
            details={"cause_type": type(exc).__name__},
            retryable=True,
        )


__all__ = [
    "ImportDisposition",
    "ImportDestinationSnapshot",
    "ImportLibCommand",
    "ImportWarning",
    "InterchangeRepositoryPort",
    "InterchangeUnitOfWorkPort",
    "LibCompiledPageImport",
    "LibFigureImport",
    "LibImportPlan",
    "LibImportPlannerPort",
    "LibImportReceipt",
    "LibInterchangeService",
    "LibPageImport",
    "LibTemplateImport",
    "LibTranslationImport",
    "OpenLibCommand",
    "OpenLibDraftFactory",
    "OpenLibReceipt",
    "OpenLibRepositoryPort",
    "OpenLibResult",
    "OpenLibService",
    "OpenLibUnitOfWorkPort",
]
