"""Framework-neutral commands for the core item catalogue.

The query side of the item spine deliberately exposes immutable aggregate
views.  This module supplies the complementary command boundary without
assuming a filesystem, database, HTTP transport, or user interface.

Repositories open one operation-scoped unit of work.  The unit holds a stable
catalogue snapshot from context entry through ``commit`` and stages, rather
than publishes, every mutation.  A successful commit must publish the item
mutation and its durable receipt atomically.  This lets the service make
create, replace, and delete commands safely retryable while keeping storage
and recovery policy outside the engine.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, ContextManager, Literal, Protocol, TypeAlias

from .errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)


JsonMapping: TypeAlias = Mapping[str, Any]
ItemMutationAction: TypeAlias = Literal["create", "update", "delete"]

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"create", "update", "delete"})


def _safe_string(
    value: Any,
    field_name: str,
    *,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if any(
        (ord(character) < 32 and character not in "\n\r\t")
        or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"{field_name} contains a control character")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{field_name} contains an unpaired surrogate")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field_name} is too long")
    return value


def _portable_identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


def _revision(value: Any, field_name: str, *, optional: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if optional and not value:
        return value
    _safe_string(value, field_name, maximum=512)
    if (
        not value
        or value != value.strip()
        or '"' in value
        or "\\" in value
    ):
        raise ValueError(f"{field_name} is not a valid revision token")
    return value


def _metadata_key(value: Any, field_name: str = "metadata key") -> str:
    result = _safe_string(value, field_name, maximum=256)
    if not result or result != result.strip():
        raise ValueError(f"{field_name} must be non-empty without outer whitespace")
    return result


def _freeze_json(
    value: Any,
    *,
    path: str = "$",
    active: set[int] | None = None,
) -> Any:
    """Detach strict JSON into mappings and tuples that callers cannot mutate."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _safe_string(value, path)
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
                _safe_string(key, f"{path} object key")
                frozen[key] = _freeze_json(
                    item,
                    path=f"{path}.{key}",
                    active=active,
                )
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


def _freeze_metadata(value: Any, *, path: str) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be an object")
    for key in value:
        _metadata_key(key, f"{path} key")
    frozen = _freeze_json(value, path=path)
    assert isinstance(frozen, Mapping)
    return frozen


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            _thaw(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def _typed_representations(
    values: Any,
    *,
    field_name: str,
) -> tuple["RepresentationDraft", ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{field_name} must be an iterable") from exc
    if any(not isinstance(value, RepresentationDraft) for value in result):
        raise TypeError(
            f"{field_name} must contain RepresentationDraft values"
        )
    identifiers = [value.representation_id.casefold() for value in result]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"{field_name} contains duplicate representation ids")
    return tuple(
        sorted(
            result,
            key=lambda value: value.representation_id.casefold(),
        )
    )


@dataclass(frozen=True, slots=True)
class RepresentationDraft:
    """Portable description of one representation attached to a new state."""

    representation_id: str
    role: str = "source"
    media_type: str = "application/octet-stream"
    locator: str = ""
    label: str = ""
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        _portable_identifier(self.representation_id, "representation_id")
        _portable_identifier(self.role, "representation role")
        media_type = _safe_string(
            self.media_type,
            "media_type",
            maximum=255,
        )
        if not media_type or media_type != media_type.strip():
            raise ValueError("media_type must be a non-empty trimmed string")
        _safe_string(self.locator, "locator", maximum=4096)
        _safe_string(self.label, "label", maximum=512)
        object.__setattr__(
            self,
            "metadata",
            _freeze_metadata(self.metadata, path="$.representation.metadata"),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "RepresentationDraft":
        if not isinstance(value, Mapping):
            raise TypeError("representation must be an object")
        fields = {
            "id",
            "role",
            "media_type",
            "locator",
            "label",
            "metadata",
        }
        if set(value) != fields:
            raise ValueError("representation fields do not match the schema")
        return cls(
            representation_id=value["id"],
            role=value["role"],
            media_type=value["media_type"],
            locator=value["locator"],
            label=value["label"],
            metadata=value["metadata"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.representation_id,
            "role": self.role,
            "media_type": self.media_type,
            "locator": self.locator,
            "label": self.label,
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ItemDraft:
    """Complete canonical item state accepted by a command repository."""

    kind: str = "book"
    title: str = ""
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)
    representations: tuple[RepresentationDraft, ...] = ()

    def __post_init__(self) -> None:
        _portable_identifier(self.kind, "item kind")
        _safe_string(self.title, "title", maximum=4096)
        object.__setattr__(
            self,
            "metadata",
            _freeze_metadata(self.metadata, path="$.item.metadata"),
        )
        object.__setattr__(
            self,
            "representations",
            _typed_representations(
                self.representations,
                field_name="representations",
            ),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "ItemDraft":
        if not isinstance(value, Mapping):
            raise TypeError("item draft must be an object")
        fields = {"kind", "title", "metadata", "representations"}
        if set(value) != fields:
            raise ValueError("item draft fields do not match the schema")
        raw_representations = value["representations"]
        if isinstance(raw_representations, (str, bytes)):
            raise TypeError("representations must be an array")
        try:
            representations = tuple(
                RepresentationDraft.from_dict(item)
                for item in raw_representations
            )
        except TypeError as exc:
            raise TypeError("representations must be an array") from exc
        return cls(
            kind=value["kind"],
            title=value["title"],
            metadata=value["metadata"],
            representations=representations,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "metadata": _thaw(self.metadata),
            "representations": [
                value.as_dict() for value in self.representations
            ],
        }


@dataclass(frozen=True, slots=True)
class ItemPatch:
    """Unambiguous partial replacement of one item.

    ``None`` means that title or representations are unchanged.  An empty
    string clears the title and an empty tuple clears all representations.
    Metadata values, including JSON null, are set through ``metadata_set``;
    removals use the separate, disjoint ``metadata_remove`` collection.
    """

    title: str | None = None
    metadata_set: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)
    metadata_remove: tuple[str, ...] = ()
    representations: tuple[RepresentationDraft, ...] | None = None

    def __post_init__(self) -> None:
        if self.title is not None:
            _safe_string(self.title, "title", maximum=4096)
        metadata_set = _freeze_metadata(
            self.metadata_set,
            path="$.patch.metadata_set",
        )
        if isinstance(self.metadata_remove, (str, bytes)):
            raise TypeError("metadata_remove must be an iterable")
        try:
            metadata_remove = tuple(self.metadata_remove)
        except TypeError as exc:
            raise TypeError("metadata_remove must be an iterable") from exc
        normalized_remove = tuple(
            _metadata_key(value, "metadata_remove value")
            for value in metadata_remove
        )
        if len(normalized_remove) != len(set(normalized_remove)):
            raise ValueError("metadata_remove contains duplicate keys")
        overlap = sorted(set(metadata_set) & set(normalized_remove))
        if overlap:
            raise ValueError(
                "metadata_set and metadata_remove overlap: "
                + ", ".join(overlap)
            )
        representations = self.representations
        if representations is not None:
            representations = _typed_representations(
                representations,
                field_name="representations",
            )
        object.__setattr__(self, "metadata_set", metadata_set)
        object.__setattr__(
            self,
            "metadata_remove",
            tuple(sorted(normalized_remove)),
        )
        object.__setattr__(self, "representations", representations)

    @property
    def is_empty(self) -> bool:
        return (
            self.title is None
            and not self.metadata_set
            and not self.metadata_remove
            and self.representations is None
        )

    def apply(self, current: "ItemRecordSnapshot") -> ItemDraft:
        if not isinstance(current, ItemRecordSnapshot):
            raise TypeError("current must be an ItemRecordSnapshot")
        metadata = _thaw(current.metadata)
        for key in self.metadata_remove:
            metadata.pop(key, None)
        metadata.update(_thaw(self.metadata_set))
        return ItemDraft(
            kind=current.kind,
            title=current.title if self.title is None else self.title,
            metadata=metadata,
            representations=(
                current.representations
                if self.representations is None
                else self.representations
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "metadata_set": _thaw(self.metadata_set),
            "metadata_remove": list(self.metadata_remove),
            "representations": (
                None
                if self.representations is None
                else [value.as_dict() for value in self.representations]
            ),
        }


@dataclass(frozen=True, slots=True)
class CreateItemCommand:
    draft: ItemDraft
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.draft, ItemDraft):
            raise TypeError("draft must be an ItemDraft")
        if not isinstance(self.operation_id, str):
            raise TypeError("operation_id must be a string")


def create_item_command_sha256(draft: ItemDraft) -> str:
    """Return the canonical identity used by every create-item boundary."""

    if not isinstance(draft, ItemDraft):
        raise TypeError("draft must be an ItemDraft")
    return hashlib.sha256(
        _canonical({"action": "create", "draft": draft.as_dict()})
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class UpdateItemCommand:
    item_id: str
    expected_revision: str
    patch: ItemPatch
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str):
            raise TypeError("item_id must be a string")
        if not isinstance(self.expected_revision, str):
            raise TypeError("expected_revision must be a string")
        if not isinstance(self.patch, ItemPatch):
            raise TypeError("patch must be an ItemPatch")
        if not isinstance(self.operation_id, str):
            raise TypeError("operation_id must be a string")


@dataclass(frozen=True, slots=True)
class DeleteItemCommand:
    item_id: str
    expected_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str):
            raise TypeError("item_id must be a string")
        if not isinstance(self.expected_revision, str):
            raise TypeError("expected_revision must be a string")
        if not isinstance(self.operation_id, str):
            raise TypeError("operation_id must be a string")


@dataclass(frozen=True, slots=True)
class ItemRecordSnapshot:
    """Canonical record state returned by a command repository."""

    item_id: str
    revision: str
    kind: str = "book"
    title: str = ""
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)
    representations: tuple[RepresentationDraft, ...] = ()

    def __post_init__(self) -> None:
        _portable_identifier(self.item_id, "item_id")
        _revision(self.revision, "revision")
        draft = ItemDraft(
            kind=self.kind,
            title=self.title,
            metadata=self.metadata,
            representations=self.representations,
        )
        object.__setattr__(self, "kind", draft.kind)
        object.__setattr__(self, "title", draft.title)
        object.__setattr__(self, "metadata", draft.metadata)
        object.__setattr__(self, "representations", draft.representations)

    @classmethod
    def from_dict(cls, value: Any) -> "ItemRecordSnapshot":
        if not isinstance(value, Mapping):
            raise TypeError("item snapshot must be an object")
        fields = {
            "id",
            "revision",
            "kind",
            "title",
            "metadata",
            "representations",
        }
        if set(value) != fields:
            raise ValueError("item snapshot fields do not match the schema")
        draft = ItemDraft.from_dict(
            {
                "kind": value["kind"],
                "title": value["title"],
                "metadata": value["metadata"],
                "representations": value["representations"],
            }
        )
        return cls(
            item_id=value["id"],
            revision=value["revision"],
            kind=draft.kind,
            title=draft.title,
            metadata=draft.metadata,
            representations=draft.representations,
        )

    def as_draft(self) -> ItemDraft:
        return ItemDraft(
            kind=self.kind,
            title=self.title,
            metadata=self.metadata,
            representations=self.representations,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.item_id,
            "revision": self.revision,
            **self.as_draft().as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ItemDeletionSnapshot:
    """Repository-created pointer to a server-held recoverable tombstone."""

    item_id: str
    prior_revision: str
    tombstone_id: str

    def __post_init__(self) -> None:
        _portable_identifier(self.item_id, "item_id")
        _revision(self.prior_revision, "prior_revision")
        _portable_identifier(self.tombstone_id, "tombstone_id")

    @classmethod
    def from_dict(cls, value: Any) -> "ItemDeletionSnapshot":
        if not isinstance(value, Mapping):
            raise TypeError("deletion snapshot must be an object")
        fields = {"item_id", "prior_revision", "tombstone_id"}
        if set(value) != fields:
            raise ValueError("deletion snapshot fields do not match the schema")
        return cls(
            item_id=value["item_id"],
            prior_revision=value["prior_revision"],
            tombstone_id=value["tombstone_id"],
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "prior_revision": self.prior_revision,
            "tombstone_id": self.tombstone_id,
        }


@dataclass(frozen=True, slots=True)
class ItemMutationReceipt:
    """Durable outcome used to replay an operation without repeating it."""

    action: ItemMutationAction
    operation_id: str
    command_sha256: str
    item_id: str
    before_revision: str = ""
    after_revision: str = ""
    item: ItemRecordSnapshot | None = None
    deletion: ItemDeletionSnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        _portable_identifier(self.operation_id, "operation_id")
        if (
            not isinstance(self.command_sha256, str)
            or not _SHA256_RE.fullmatch(self.command_sha256)
        ):
            raise ValueError("command_sha256 must be a lowercase SHA-256 digest")
        _portable_identifier(self.item_id, "item_id")
        _revision(self.before_revision, "before_revision", optional=True)
        _revision(self.after_revision, "after_revision", optional=True)
        if self.item is not None and not isinstance(
            self.item,
            ItemRecordSnapshot,
        ):
            raise TypeError("item must be an ItemRecordSnapshot or None")
        if self.deletion is not None and not isinstance(
            self.deletion,
            ItemDeletionSnapshot,
        ):
            raise TypeError(
                "deletion must be an ItemDeletionSnapshot or None"
            )

        if self.action == "create":
            valid = (
                not self.before_revision
                and bool(self.after_revision)
                and self.item is not None
                and self.deletion is None
            )
        elif self.action == "update":
            valid = (
                bool(self.before_revision)
                and bool(self.after_revision)
                and self.before_revision != self.after_revision
                and self.item is not None
                and self.deletion is None
            )
        else:
            valid = (
                bool(self.before_revision)
                and not self.after_revision
                and self.item is None
                and self.deletion is not None
            )
        if not valid:
            raise ValueError("receipt state does not match its action")
        if self.item is not None and (
            self.item.item_id != self.item_id
            or self.item.revision != self.after_revision
        ):
            raise ValueError("receipt item does not match its outcome")
        if self.deletion is not None and (
            self.deletion.item_id != self.item_id
            or self.deletion.prior_revision != self.before_revision
        ):
            raise ValueError("receipt deletion does not match its outcome")

    @classmethod
    def from_dict(cls, value: Any) -> "ItemMutationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("item mutation receipt must be an object")
        fields = {
            "action",
            "operation_id",
            "command_sha256",
            "item_id",
            "before_revision",
            "after_revision",
            "item",
            "deletion",
        }
        if set(value) != fields:
            raise ValueError("item mutation receipt fields do not match the schema")
        item = value["item"]
        deletion = value["deletion"]
        return cls(
            action=value["action"],
            operation_id=value["operation_id"],
            command_sha256=value["command_sha256"],
            item_id=value["item_id"],
            before_revision=value["before_revision"],
            after_revision=value["after_revision"],
            item=None if item is None else ItemRecordSnapshot.from_dict(item),
            deletion=(
                None
                if deletion is None
                else ItemDeletionSnapshot.from_dict(deletion)
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "operation_id": self.operation_id,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "item": None if self.item is None else self.item.as_dict(),
            "deletion": (
                None if self.deletion is None else self.deletion.as_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class ItemCommandResult:
    receipt: ItemMutationReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, ItemMutationReceipt):
            raise TypeError("receipt must be an ItemMutationReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_dict(),
        }


class ItemCommandUnitOfWorkPort(Protocol):
    """One locked catalogue snapshot and explicit publication boundary.

    Stage methods must not publish live state. ``commit`` atomically publishes
    the staged mutation and receipt. Exiting without a successful commit must
    discard or roll back staged state before releasing the repository lock.
    """

    def receipt(self, operation_id: str) -> ItemMutationReceipt | None: ...

    def get(self, item_id: str) -> ItemRecordSnapshot | None: ...

    def allocate_item_id(self) -> str: ...

    def stage_create(
        self,
        item_id: str,
        draft: ItemDraft,
    ) -> ItemRecordSnapshot: ...

    def stage_replace(
        self,
        current: ItemRecordSnapshot,
        draft: ItemDraft,
    ) -> ItemRecordSnapshot: ...

    def stage_delete(
        self,
        current: ItemRecordSnapshot,
    ) -> ItemDeletionSnapshot: ...

    def commit(self, receipt: ItemMutationReceipt) -> None: ...


class ItemCommandRepositoryPort(Protocol):
    """Open an operation-scoped item command unit of work."""

    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> ContextManager[ItemCommandUnitOfWorkPort]: ...


class ItemCommandService:
    """Validate, conditionally stage, and idempotently commit item commands."""

    def __init__(self, repository: ItemCommandRepositoryPort) -> None:
        self._repository = repository

    def create(self, command: CreateItemCommand) -> ItemCommandResult:
        if not isinstance(command, CreateItemCommand):
            raise ValidationError(
                "create requires a CreateItemCommand",
                code="invalid_item_command",
            )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = create_item_command_sha256(command.draft)
        try:
            with self._repository.unit_of_work(
                operation_id=operation_id
            ) as unit:
                replay = self._replay(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    action="create",
                )
                if replay is not None:
                    return replay
                item_id = self._repository_item_id(unit.allocate_item_id())
                if self._repository_snapshot(unit.get(item_id)) is not None:
                    raise RepositoryError(
                        "the item repository allocated an existing identity",
                        code="allocated_item_id_collision",
                        details={"item_id": item_id},
                        retryable=True,
                    )
                staged = self._repository_snapshot(
                    unit.stage_create(item_id, command.draft),
                    required=True,
                )
                assert staged is not None
                self._validate_staged_record(
                    staged,
                    item_id=item_id,
                    draft=command.draft,
                )
                receipt = ItemMutationReceipt(
                    action="create",
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    after_revision=staged.revision,
                    item=staged,
                )
                unit.commit(receipt)
                return ItemCommandResult(receipt, replayed=False)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    def update(self, command: UpdateItemCommand) -> ItemCommandResult:
        if not isinstance(command, UpdateItemCommand):
            raise ValidationError(
                "update requires an UpdateItemCommand",
                code="invalid_item_command",
            )
        item_id = self._item_id(command.item_id)
        expected_revision = self._expected_revision(
            command.expected_revision,
            item_id=item_id,
        )
        if command.patch.is_empty:
            raise ValidationError(
                "the item patch has no changes",
                code="empty_item_patch",
                details={"item_id": item_id},
            )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "update",
                "item_id": item_id,
                "expected_revision": expected_revision,
                "patch": command.patch.as_dict(),
            }
        )
        try:
            with self._repository.unit_of_work(
                operation_id=operation_id
            ) as unit:
                replay = self._replay(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    action="update",
                    item_id=item_id,
                )
                if replay is not None:
                    return replay
                current = self._require_current(unit, item_id)
                self._match_revision(current, expected_revision)
                draft = command.patch.apply(current)
                staged = self._repository_snapshot(
                    unit.stage_replace(current, draft),
                    required=True,
                )
                assert staged is not None
                self._validate_staged_record(
                    staged,
                    item_id=item_id,
                    draft=draft,
                )
                if staged.revision == current.revision:
                    raise RepositoryError(
                        "the item repository did not advance the revision",
                        code="item_revision_not_advanced",
                        details={"item_id": item_id},
                    )
                receipt = ItemMutationReceipt(
                    action="update",
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    before_revision=current.revision,
                    after_revision=staged.revision,
                    item=staged,
                )
                unit.commit(receipt)
                return ItemCommandResult(receipt, replayed=False)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    def delete(self, command: DeleteItemCommand) -> ItemCommandResult:
        if not isinstance(command, DeleteItemCommand):
            raise ValidationError(
                "delete requires a DeleteItemCommand",
                code="invalid_item_command",
            )
        item_id = self._item_id(command.item_id)
        expected_revision = self._expected_revision(
            command.expected_revision,
            item_id=item_id,
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "delete",
                "item_id": item_id,
                "expected_revision": expected_revision,
            }
        )
        try:
            with self._repository.unit_of_work(
                operation_id=operation_id
            ) as unit:
                replay = self._replay(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    action="delete",
                    item_id=item_id,
                )
                if replay is not None:
                    return replay
                current = self._require_current(unit, item_id)
                self._match_revision(current, expected_revision)
                deletion = unit.stage_delete(current)
                if not isinstance(deletion, ItemDeletionSnapshot):
                    raise RepositoryError(
                        "the item repository returned an invalid deletion",
                        code="invalid_item_deletion_snapshot",
                    )
                if (
                    deletion.item_id != item_id
                    or deletion.prior_revision != current.revision
                ):
                    raise RepositoryError(
                        "the item repository returned the wrong deletion",
                        code="item_repository_scope_mismatch",
                        details={
                            "requested_item_id": item_id,
                            "returned_item_id": deletion.item_id,
                        },
                    )
                receipt = ItemMutationReceipt(
                    action="delete",
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    before_revision=current.revision,
                    deletion=deletion,
                )
                unit.commit(receipt)
                return ItemCommandResult(receipt, replayed=False)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    @staticmethod
    def _operation_id(value: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an operation id is required",
                code="operation_id_required",
                details={"field": "operation_id"},
            )
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValidationError(
                "operation id must be a portable token",
                code="invalid_operation_id",
            )
        return value

    @staticmethod
    def _item_id(value: str) -> str:
        if not value:
            raise ValidationError(
                "item id is required",
                code="item_id_required",
            )
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValidationError(
                "item id must be a portable identifier",
                code="invalid_item_id",
            )
        return value

    @staticmethod
    def _expected_revision(value: str, *, item_id: str) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an expected item revision is required",
                code="item_revision_required",
                details={"item_id": item_id},
            )
        try:
            return _revision(value, "expected_revision")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "expected item revision is invalid",
                code="invalid_item_revision",
                details={"item_id": item_id},
            ) from exc

    @staticmethod
    def _command_hash(value: JsonMapping) -> str:
        return hashlib.sha256(_canonical(value)).hexdigest()

    @staticmethod
    def _repository_item_id(value: Any) -> str:
        try:
            return _portable_identifier(value, "allocated item id")
        except (TypeError, ValueError) as exc:
            raise RepositoryError(
                "the item repository allocated an invalid identity",
                code="invalid_allocated_item_id",
            ) from exc

    @staticmethod
    def _repository_snapshot(
        value: Any,
        *,
        required: bool = False,
    ) -> ItemRecordSnapshot | None:
        if value is None and not required:
            return None
        if not isinstance(value, ItemRecordSnapshot):
            raise RepositoryError(
                "the item repository returned an invalid snapshot",
                code="invalid_item_record_snapshot",
            )
        return value

    def _require_current(
        self,
        unit: ItemCommandUnitOfWorkPort,
        item_id: str,
    ) -> ItemRecordSnapshot:
        current = self._repository_snapshot(unit.get(item_id))
        if current is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": item_id},
            )
        if current.item_id != item_id:
            raise RepositoryError(
                "the item repository returned the wrong record",
                code="item_repository_scope_mismatch",
                details={
                    "requested_item_id": item_id,
                    "returned_item_id": current.item_id,
                },
            )
        return current

    @staticmethod
    def _match_revision(
        current: ItemRecordSnapshot,
        expected_revision: str,
    ) -> None:
        if current.revision != expected_revision:
            raise ConflictError(
                "the item changed elsewhere",
                code="item_revision_conflict",
                details={
                    "item_id": current.item_id,
                    "expected_revision": expected_revision,
                    "current_revision": current.revision,
                },
            )

    @staticmethod
    def _validate_staged_record(
        staged: ItemRecordSnapshot,
        *,
        item_id: str,
        draft: ItemDraft,
    ) -> None:
        if staged.item_id != item_id:
            raise RepositoryError(
                "the item repository staged another item",
                code="item_repository_scope_mismatch",
                details={
                    "requested_item_id": item_id,
                    "returned_item_id": staged.item_id,
                },
            )
        if staged.as_draft() != draft:
            raise RepositoryError(
                "the item repository changed canonical item content",
                code="item_repository_content_mismatch",
                details={"item_id": item_id},
            )

    @staticmethod
    def _replay(
        unit: ItemCommandUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        action: ItemMutationAction,
        item_id: str = "",
    ) -> ItemCommandResult | None:
        prior = unit.receipt(operation_id)
        if prior is None:
            return None
        if not isinstance(prior, ItemMutationReceipt):
            raise RepositoryError(
                "the item repository returned an invalid receipt",
                code="invalid_item_mutation_receipt",
            )
        if prior.operation_id != operation_id:
            raise RepositoryError(
                "the item repository returned another operation receipt",
                code="receipt_scope_mismatch",
            )
        if prior.command_sha256 != command_sha256 or prior.action != action:
            raise ConflictError(
                "operation id was already used for another item command",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        if item_id and prior.item_id != item_id:
            raise RepositoryError(
                "the item repository returned a receipt for another item",
                code="receipt_scope_mismatch",
                details={
                    "requested_item_id": item_id,
                    "receipt_item_id": prior.item_id,
                },
            )
        return ItemCommandResult(prior, replayed=True)

    @staticmethod
    def _repository_failure(exc: Exception) -> RepositoryError:
        return RepositoryError(
            "the item command repository failed",
            code="item_repository_unavailable",
            # Repository messages may contain paths, credentials, or backend
            # response bodies. Preserve only a diagnostic type across the
            # transport-safe engine boundary.
            details={"cause_type": type(exc).__name__},
            retryable=True,
        )


__all__ = [
    "CreateItemCommand",
    "DeleteItemCommand",
    "ItemCommandRepositoryPort",
    "ItemCommandResult",
    "ItemCommandService",
    "ItemCommandUnitOfWorkPort",
    "ItemDeletionSnapshot",
    "ItemDraft",
    "ItemMutationAction",
    "ItemMutationReceipt",
    "ItemPatch",
    "ItemRecordSnapshot",
    "RepresentationDraft",
    "UpdateItemCommand",
    "create_item_command_sha256",
]
