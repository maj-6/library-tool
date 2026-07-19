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
from typing import Any, ContextManager, Protocol

from .errors import ConflictError, RepositoryError, ValidationError


_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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
    archive: bytes
    operation_id: str
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class ImportDestinationSnapshot:
    item_id: str
    revision: str = ""
    book_id: str = ""
    pages: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)
    templates: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    translation_pages: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    has_stylesheet: bool = False
    has_manifest_ext: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.pages, Mapping):
            raise TypeError("pages must be an object keyed by page number")
        pages: dict[int, Mapping[str, Any]] = {}
        for page, record in self.pages.items():
            if isinstance(page, bool) or not isinstance(page, int) or page < 1:
                raise ValueError("pages contains an invalid page number")
            pages[page] = _freeze_mapping(record, path=f"$.pages[{page}]")
        object.__setattr__(self, "pages", MappingProxyType(pages))
        object.__setattr__(self, "templates", tuple(str(v) for v in self.templates))
        object.__setattr__(self, "figures", tuple(str(v) for v in self.figures))
        if not isinstance(self.translation_pages, Mapping):
            raise TypeError("translation_pages must be an object")
        translations: dict[str, tuple[int, ...]] = {}
        for language, raw_pages in self.translation_pages.items():
            if not isinstance(language, str):
                raise TypeError("translation_pages keys must be strings")
            pages_for_language = tuple(raw_pages)
            if any(
                isinstance(page, bool) or not isinstance(page, int) or page < 1
                for page in pages_for_language
            ):
                raise ValueError("translation_pages contains an invalid page")
            translations[language] = pages_for_language
        object.__setattr__(self, "translation_pages", MappingProxyType(translations))


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
class LibImportPlan:
    archive_sha256: str
    format_version: str
    incoming_book_id: str = ""
    pages: tuple[LibPageImport, ...] = ()
    pages_skipped: tuple[int, ...] = ()
    pages_protected: tuple[int, ...] = ()
    templates: tuple[LibTemplateImport, ...] = ()
    figures: tuple[LibFigureImport, ...] = ()
    translations: tuple[LibTranslationImport, ...] = ()
    stylesheet: Mapping[str, Any] | None = None
    manifest_ext: Mapping[str, Any] | None = None
    instructions: str = ""
    warnings: tuple[ImportWarning, ...] = ()

    def __post_init__(self) -> None:
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
    warnings: tuple[ImportWarning, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "pages_applied",
            "pages_skipped",
            "pages_protected",
            "templates_added",
            "figures_added",
            "translations_added",
        ):
            object.__setattr__(self, field_name, tuple(getattr(self, field_name)))
        object.__setattr__(
            self,
            "warnings",
            _typed_tuple(self.warnings, ImportWarning, field_name="warnings"),
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
            "warnings": [warning.as_dict() for warning in self.warnings],
        }


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
        operation_id: str,
    ) -> ContextManager[InterchangeUnitOfWorkPort]: ...


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
        command_sha256 = self._command_hash(
            item_id=item_id,
            source_id=source_id,
            overwrite=bool(command.overwrite),
            archive_sha256=archive_sha256,
        )
        with self._repository.unit_of_work(item_id, operation_id=operation_id) as unit:
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
            self._validate_plan(plan)
            receipt = self._receipt(
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
    def _validate_plan(plan: LibImportPlan) -> None:
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

        translation_keys: set[tuple[str, int]] = set()
        for translation in plan.translations:
            language = translation.language
            if not isinstance(language, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", language
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

    @staticmethod
    def _receipt(
        operation_id: str,
        item_id: str,
        source_id: str,
        overwrite: bool,
        command_sha256: str,
        plan: LibImportPlan,
    ) -> LibImportReceipt:
        languages = tuple(sorted({value.language for value in plan.translations}))
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
            warnings=tuple(plan.warnings),
        )

    @staticmethod
    def _command_hash(
        *,
        item_id: str,
        source_id: str,
        overwrite: bool,
        archive_sha256: str,
    ) -> str:
        payload = json.dumps(
            {
                "archive_sha256": archive_sha256,
                "item_id": item_id,
                "overwrite": overwrite,
                "source_id": source_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


__all__ = [
    "ImportDestinationSnapshot",
    "ImportLibCommand",
    "ImportWarning",
    "InterchangeRepositoryPort",
    "InterchangeUnitOfWorkPort",
    "LibFigureImport",
    "LibImportPlan",
    "LibImportPlannerPort",
    "LibImportReceipt",
    "LibInterchangeService",
    "LibPageImport",
    "LibTemplateImport",
    "LibTranslationImport",
    "OpenLibCommand",
]
