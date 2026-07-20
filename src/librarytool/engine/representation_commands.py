"""Framework-neutral commands for attaching item representations.

The query spine deliberately exposes opaque representation locators.  This
module supplies the complementary mutation boundary without assuming Flask,
filesystem paths, file pickers, or a particular asset store.  An adapter alone
interprets ``source_token`` and receipts never echo that sensitive input.

Each operation owns one representation and uses both item and representation
compare-and-swap preconditions.  Repositories stage a complete aggregate and
publish it with the durable receipt in one transaction.
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
AcquisitionMode: TypeAlias = Literal["reference", "copy"]
RepresentationDisposition: TypeAlias = Literal["referenced", "copied"]
RepresentationContentState: TypeAlias = Literal[
    "unchanged", "drifted", "untracked", "missing"
]
RepresentationMutationAction: TypeAlias = Literal[
    "attach", "replace", "detach"
]

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = frozenset({"attach", "replace", "detach"})
_ACQUISITION_MODES = frozenset({"reference", "copy"})
_DISPOSITIONS = frozenset({"referenced", "copied"})
_CONTENT_STATES = frozenset({"unchanged", "drifted", "untracked", "missing"})


def _text(value: Any, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} is too long")
    for character in value:
        codepoint = ord(character)
        if codepoint == 127 or (
            codepoint < 32 and character not in "\n\r\t"
        ):
            raise ValueError(f"{field_name} contains a control character")
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError(f"{field_name} contains an unpaired surrogate")
    return value


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


def _revision(value: Any, field_name: str) -> str:
    result = _text(value, field_name, maximum=512)
    if (
        not result
        or result != result.strip()
        or '"' in result
        or "\\" in result
        or any(ord(character) <= 32 or ord(character) == 127
               for character in result)
    ):
        raise ValueError(f"{field_name} is not a valid revision")
    return result


def _freeze_json(
    value: Any,
    *,
    path: str,
    active: set[int] | None = None,
) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _text(value, path, maximum=1_000_000)
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
            result: dict[str, Any] = {}
            for raw_key, item in value.items():
                key = _text(raw_key, f"{path} object key", maximum=256)
                if not key or key != key.strip():
                    raise ValueError(
                        f"{path} object keys must be non-empty and trimmed"
                    )
                if key in result:
                    raise ValueError(f"{path} contains a duplicate key")
                result[key] = _freeze_json(
                    item, path=f"{path}.{key}", active=active
                )
            return MappingProxyType(result)
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
    raise TypeError(f"{path} contains non-JSON data")


def _metadata(value: Any, *, path: str) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be an object")
    result = _freeze_json(value, path=path)
    assert isinstance(result, Mapping)
    return result


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


@dataclass(frozen=True, slots=True)
class RepresentationAttachmentDraft:
    """One adapter-addressable source that must not cross read boundaries."""

    representation_id: str
    source_token: str = field(repr=False)
    acquisition: AcquisitionMode = "reference"
    expected_content_sha256: str = ""
    expected_size: int | None = None
    role: str = "source"
    media_type: str = "application/octet-stream"
    label: str = ""
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        _identifier(self.representation_id, "representation_id")
        source_token = _text(
            self.source_token, "source_token", maximum=8192
        )
        if not source_token or source_token != source_token.strip():
            raise ValueError("source_token must be non-empty and trimmed")
        if self.acquisition not in _ACQUISITION_MODES:
            raise ValueError("acquisition is invalid")
        if self.expected_content_sha256 and not _SHA256_RE.fullmatch(
            self.expected_content_sha256
        ):
            raise ValueError("expected_content_sha256 is invalid")
        if self.expected_size is not None and (
            isinstance(self.expected_size, bool)
            or not isinstance(self.expected_size, int)
            or self.expected_size < 0
        ):
            raise ValueError(
                "expected_size must be a non-negative integer or None"
            )
        _identifier(self.role, "representation role")
        media_type = _text(self.media_type, "media_type", maximum=255)
        if not media_type or media_type != media_type.strip():
            raise ValueError("media_type must be non-empty and trimmed")
        _text(self.label, "label", maximum=512)
        object.__setattr__(
            self,
            "metadata",
            _metadata(self.metadata, path="$.representation.metadata"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the command form, including the adapter-only source token."""

        return {
            "id": self.representation_id,
            "source_token": self.source_token,
            "acquisition": self.acquisition,
            "expected_content_sha256": self.expected_content_sha256,
            "expected_size": self.expected_size,
            "role": self.role,
            "media_type": self.media_type,
            "label": self.label,
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RepresentationRecordSnapshot:
    """Safe representation state; the adapter input token is absent."""

    representation_id: str
    revision: str
    role: str
    media_type: str
    locator: str
    label: str = ""
    available: bool = True
    disposition: RepresentationDisposition = "referenced"
    content_state: RepresentationContentState = "untracked"
    content_sha256: str = ""
    size: int | None = None
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        _identifier(self.representation_id, "representation_id")
        _revision(self.revision, "representation revision")
        _identifier(self.role, "representation role")
        media_type = _text(self.media_type, "media_type", maximum=255)
        if not media_type or media_type != media_type.strip():
            raise ValueError("media_type must be non-empty and trimmed")
        locator = _text(self.locator, "locator", maximum=4096)
        if not locator or locator != locator.strip():
            raise ValueError("locator must be non-empty and trimmed")
        _text(self.label, "label", maximum=512)
        if not isinstance(self.available, bool):
            raise TypeError("available must be a boolean")
        if self.disposition not in _DISPOSITIONS:
            raise ValueError("disposition is invalid")
        if self.content_state not in _CONTENT_STATES:
            raise ValueError("content_state is invalid")
        if self.content_sha256 and not _SHA256_RE.fullmatch(
            self.content_sha256
        ):
            raise ValueError("content_sha256 is invalid")
        if self.size is not None and (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
        ):
            raise ValueError("size must be a non-negative integer or None")
        object.__setattr__(
            self,
            "metadata",
            _metadata(self.metadata, path="$.representation.metadata"),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "RepresentationRecordSnapshot":
        if not isinstance(value, Mapping):
            raise TypeError("representation snapshot must be an object")
        fields = {
            "id",
            "revision",
            "role",
            "media_type",
            "locator",
            "label",
            "available",
            "disposition",
            "content_state",
            "content_sha256",
            "size",
            "metadata",
        }
        if set(value) != fields:
            raise ValueError("representation snapshot fields do not match")
        return cls(
            representation_id=value["id"],
            revision=value["revision"],
            role=value["role"],
            media_type=value["media_type"],
            locator=value["locator"],
            label=value["label"],
            available=value["available"],
            disposition=value["disposition"],
            content_state=value["content_state"],
            content_sha256=value["content_sha256"],
            size=value["size"],
            metadata=value["metadata"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.representation_id,
            "revision": self.revision,
            "role": self.role,
            "media_type": self.media_type,
            "locator": self.locator,
            "label": self.label,
            "available": self.available,
            "disposition": self.disposition,
            "content_state": self.content_state,
            "content_sha256": self.content_sha256,
            "size": self.size,
            "metadata": _thaw(self.metadata),
        }


def _representations(
    values: Any,
) -> tuple[RepresentationRecordSnapshot, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("representations must be an iterable")
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError("representations must be an iterable") from exc
    if any(not isinstance(value, RepresentationRecordSnapshot) for value in result):
        raise TypeError("representations contain an invalid snapshot")
    aliases = [value.representation_id.casefold() for value in result]
    if len(aliases) != len(set(aliases)):
        raise ValueError("representations contain duplicate identities")
    return tuple(
        sorted(result, key=lambda value: value.representation_id.casefold())
    )


@dataclass(frozen=True, slots=True)
class RepresentationAggregateSnapshot:
    item_id: str
    item_revision: str
    representations: tuple[RepresentationRecordSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _revision(self.item_revision, "item_revision")
        object.__setattr__(
            self, "representations", _representations(self.representations)
        )

    def get(self, representation_id: str) -> RepresentationRecordSnapshot | None:
        folded = representation_id.casefold()
        return next(
            (
                value
                for value in self.representations
                if value.representation_id.casefold() == folded
            ),
            None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_revision": self.item_revision,
            "representations": [
                value.as_dict() for value in self.representations
            ],
        }


@dataclass(frozen=True, slots=True)
class AttachRepresentationCommand:
    item_id: str
    expected_item_revision: str
    draft: RepresentationAttachmentDraft
    operation_id: str
    expected_representation_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.item_id, str):
            raise TypeError("item_id must be a string")
        if not isinstance(self.expected_item_revision, str):
            raise TypeError("expected_item_revision must be a string")
        if not isinstance(self.draft, RepresentationAttachmentDraft):
            raise TypeError("draft must be a RepresentationAttachmentDraft")
        if not isinstance(self.operation_id, str):
            raise TypeError("operation_id must be a string")
        if self.expected_representation_revision is not None and not isinstance(
            self.expected_representation_revision, str
        ):
            raise TypeError(
                "expected_representation_revision must be a string or None"
            )


@dataclass(frozen=True, slots=True)
class DetachRepresentationCommand:
    item_id: str
    representation_id: str
    expected_item_revision: str
    expected_representation_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "item_id",
            "representation_id",
            "expected_item_revision",
            "expected_representation_revision",
            "operation_id",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")


@dataclass(frozen=True, slots=True)
class RepresentationMutationReceipt:
    action: RepresentationMutationAction
    operation_id: str
    command_sha256: str
    item_id: str
    representation_id: str
    before_item_revision: str
    after_item_revision: str
    before: RepresentationRecordSnapshot | None
    after: RepresentationRecordSnapshot | None

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("action is invalid")
        _identifier(self.operation_id, "operation_id")
        if not isinstance(self.command_sha256, str) or not _SHA256_RE.fullmatch(
            self.command_sha256
        ):
            raise ValueError("command_sha256 is invalid")
        _identifier(self.item_id, "item_id")
        _identifier(self.representation_id, "representation_id")
        _revision(self.before_item_revision, "before_item_revision")
        _revision(self.after_item_revision, "after_item_revision")
        if self.before_item_revision == self.after_item_revision:
            raise ValueError("item revision did not advance")
        if self.before is not None and not isinstance(
            self.before, RepresentationRecordSnapshot
        ):
            raise TypeError("before must be a representation snapshot or None")
        if self.after is not None and not isinstance(
            self.after, RepresentationRecordSnapshot
        ):
            raise TypeError("after must be a representation snapshot or None")
        valid = (
            (self.action == "attach" and self.before is None and self.after is not None)
            or (
                self.action == "replace"
                and self.before is not None
                and self.after is not None
            )
            or (self.action == "detach" and self.before is not None and self.after is None)
        )
        if not valid:
            raise ValueError("receipt state does not match its action")
        for snapshot in (self.before, self.after):
            if snapshot is not None and (
                snapshot.representation_id != self.representation_id
            ):
                raise ValueError("receipt representation identity is inconsistent")

    @classmethod
    def from_dict(cls, value: Any) -> "RepresentationMutationReceipt":
        if not isinstance(value, Mapping):
            raise TypeError("representation receipt must be an object")
        fields = {
            "action",
            "operation_id",
            "command_sha256",
            "item_id",
            "representation_id",
            "before_item_revision",
            "after_item_revision",
            "before",
            "after",
        }
        if set(value) != fields:
            raise ValueError("representation receipt fields do not match")
        before = value["before"]
        after = value["after"]
        return cls(
            action=value["action"],
            operation_id=value["operation_id"],
            command_sha256=value["command_sha256"],
            item_id=value["item_id"],
            representation_id=value["representation_id"],
            before_item_revision=value["before_item_revision"],
            after_item_revision=value["after_item_revision"],
            before=(
                None
                if before is None
                else RepresentationRecordSnapshot.from_dict(before)
            ),
            after=(
                None
                if after is None
                else RepresentationRecordSnapshot.from_dict(after)
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "operation_id": self.operation_id,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "before_item_revision": self.before_item_revision,
            "after_item_revision": self.after_item_revision,
            "before": None if self.before is None else self.before.as_dict(),
            "after": None if self.after is None else self.after.as_dict(),
        }

    def as_public_dict(self) -> dict[str, Any]:
        """Return the client receipt without its private replay fingerprint.

        ``command_sha256`` binds the adapter-only source token for durable
        idempotency checks. Publishing it would let a remote client test likely
        local paths or future bearer-like source tokens offline.
        """
        value = self.as_dict()
        value.pop("command_sha256")
        return value


@dataclass(frozen=True, slots=True)
class RepresentationCommandResult:
    receipt: RepresentationMutationReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, RepresentationMutationReceipt):
            raise TypeError("receipt must be a RepresentationMutationReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_public_dict(),
        }


class RepresentationCommandUnitOfWorkPort(Protocol):
    """One isolated aggregate snapshot and explicit publication boundary.

    The repository must hold the same lock/isolation scope from ``get``
    through ``commit``. Stage methods cannot publish live state; commit must
    atomically publish the staged aggregate and receipt.
    """

    def receipt(
        self, operation_id: str
    ) -> RepresentationMutationReceipt | None: ...

    def get(self, item_id: str) -> RepresentationAggregateSnapshot | None: ...

    def stage_put(
        self,
        current: RepresentationAggregateSnapshot,
        draft: RepresentationAttachmentDraft,
    ) -> RepresentationAggregateSnapshot: ...

    def stage_detach(
        self,
        current: RepresentationAggregateSnapshot,
        representation_id: str,
    ) -> RepresentationAggregateSnapshot: ...

    def commit(self, receipt: RepresentationMutationReceipt) -> None: ...


class RepresentationCommandRepositoryPort(Protocol):
    """Open one operation-scoped, isolated representation unit of work."""

    def unit_of_work(
        self, *, operation_id: str
    ) -> ContextManager[RepresentationCommandUnitOfWorkPort]: ...


class RepresentationCommandService:
    """Conditionally stage and idempotently commit representation commands."""

    def __init__(self, repository: RepresentationCommandRepositoryPort) -> None:
        self._repository = repository

    def attach(
        self, command: AttachRepresentationCommand
    ) -> RepresentationCommandResult:
        if not isinstance(command, AttachRepresentationCommand):
            raise ValidationError(
                "attach requires an AttachRepresentationCommand",
                code="invalid_representation_command",
            )
        item_id = self._item_id(command.item_id)
        representation_id = self._representation_id(
            command.draft.representation_id
        )
        expected_item_revision = self._expected_revision(
            command.expected_item_revision,
            field="expected_item_revision",
            item_id=item_id,
        )
        expected_representation_revision = command.expected_representation_revision
        action: RepresentationMutationAction = "attach"
        if expected_representation_revision is not None:
            action = "replace"
            expected_representation_revision = self._expected_revision(
                expected_representation_revision,
                field="expected_representation_revision",
                item_id=item_id,
                representation_id=representation_id,
            )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": action,
                "item_id": item_id,
                "expected_item_revision": expected_item_revision,
                "expected_representation_revision": (
                    expected_representation_revision
                ),
                "representation": command.draft.as_dict(),
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
                    action=action,
                    item_id=item_id,
                    representation_id=representation_id,
                    expected_item_revision=expected_item_revision,
                    expected_representation_revision=(
                        expected_representation_revision
                    ),
                    draft=command.draft,
                )
                if replay is not None:
                    return replay
                current = self._current(unit, item_id)
                self._match_item_revision(current, expected_item_revision)
                before = current.get(representation_id)
                if action == "attach":
                    if before is not None:
                        raise ConflictError(
                            "the representation already exists",
                            code="representation_already_exists",
                            details={
                                "item_id": item_id,
                                "representation_id": representation_id,
                                "current_revision": before.revision,
                            },
                        )
                else:
                    before = self._require_representation(
                        current, representation_id
                    )
                    assert expected_representation_revision is not None
                    self._match_representation_revision(
                        before, expected_representation_revision, item_id=item_id
                    )
                staged = self._aggregate(
                    unit.stage_put(current, command.draft), required=True
                )
                assert staged is not None
                after = self._validate_put(
                    current=current,
                    staged=staged,
                    draft=command.draft,
                    before=before,
                )
                receipt = RepresentationMutationReceipt(
                    action=action,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    representation_id=representation_id,
                    before_item_revision=current.item_revision,
                    after_item_revision=staged.item_revision,
                    before=before,
                    after=after,
                )
                unit.commit(receipt)
                return RepresentationCommandResult(receipt)
        except EngineError:
            raise
        except Exception as exc:
            raise self._repository_failure(exc) from exc

    def detach(
        self, command: DetachRepresentationCommand
    ) -> RepresentationCommandResult:
        if not isinstance(command, DetachRepresentationCommand):
            raise ValidationError(
                "detach requires a DetachRepresentationCommand",
                code="invalid_representation_command",
            )
        item_id = self._item_id(command.item_id)
        representation_id = self._representation_id(command.representation_id)
        expected_item_revision = self._expected_revision(
            command.expected_item_revision,
            field="expected_item_revision",
            item_id=item_id,
        )
        expected_representation_revision = self._expected_revision(
            command.expected_representation_revision,
            field="expected_representation_revision",
            item_id=item_id,
            representation_id=representation_id,
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = self._command_hash(
            {
                "action": "detach",
                "item_id": item_id,
                "representation_id": representation_id,
                "expected_item_revision": expected_item_revision,
                "expected_representation_revision": (
                    expected_representation_revision
                ),
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
                    action="detach",
                    item_id=item_id,
                    representation_id=representation_id,
                    expected_item_revision=expected_item_revision,
                    expected_representation_revision=(
                        expected_representation_revision
                    ),
                )
                if replay is not None:
                    return replay
                current = self._current(unit, item_id)
                self._match_item_revision(current, expected_item_revision)
                before = self._require_representation(current, representation_id)
                self._match_representation_revision(
                    before,
                    expected_representation_revision,
                    item_id=item_id,
                )
                staged = self._aggregate(
                    unit.stage_detach(current, representation_id), required=True
                )
                assert staged is not None
                self._validate_detach(
                    current=current,
                    staged=staged,
                    representation_id=representation_id,
                )
                receipt = RepresentationMutationReceipt(
                    action="detach",
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    representation_id=representation_id,
                    before_item_revision=current.item_revision,
                    after_item_revision=staged.item_revision,
                    before=before,
                    after=None,
                )
                unit.commit(receipt)
                return RepresentationCommandResult(receipt)
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
        try:
            return _identifier(value, "operation_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "operation id must be a portable identifier",
                code="invalid_operation_id",
            ) from exc

    @staticmethod
    def _item_id(value: str) -> str:
        try:
            return _identifier(value, "item_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "item id must be a portable identifier",
                code="invalid_item_id",
            ) from exc

    @staticmethod
    def _representation_id(value: str) -> str:
        try:
            return _identifier(value, "representation_id")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "representation id must be a portable identifier",
                code="invalid_representation_id",
            ) from exc

    @staticmethod
    def _expected_revision(
        value: str,
        *,
        field: str,
        item_id: str,
        representation_id: str = "",
    ) -> str:
        if not value:
            details = {"field": field, "item_id": item_id}
            if representation_id:
                details["representation_id"] = representation_id
            raise PreconditionRequiredError(
                "an expected revision is required",
                code=(
                    "representation_revision_required"
                    if representation_id
                    else "item_revision_required"
                ),
                details=details,
            )
        try:
            return _revision(value, field)
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "the expected revision is invalid",
                code=(
                    "invalid_representation_revision"
                    if representation_id
                    else "invalid_item_revision"
                ),
                details={"item_id": item_id},
            ) from exc

    @staticmethod
    def _command_hash(value: JsonMapping) -> str:
        return hashlib.sha256(_canonical(value)).hexdigest()

    @staticmethod
    def _aggregate(
        value: Any, *, required: bool = False
    ) -> RepresentationAggregateSnapshot | None:
        if value is None and not required:
            return None
        if not isinstance(value, RepresentationAggregateSnapshot):
            raise RepositoryError(
                "the representation repository returned an invalid aggregate",
                code="invalid_representation_snapshot",
            )
        return value

    def _current(
        self, unit: RepresentationCommandUnitOfWorkPort, item_id: str
    ) -> RepresentationAggregateSnapshot:
        current = self._aggregate(unit.get(item_id))
        if current is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": item_id},
            )
        if current.item_id != item_id:
            raise RepositoryError(
                "the representation repository returned another item",
                code="representation_repository_scope_mismatch",
            )
        return current

    @staticmethod
    def _match_item_revision(
        current: RepresentationAggregateSnapshot, expected: str
    ) -> None:
        if current.item_revision != expected:
            raise ConflictError(
                "the item changed elsewhere",
                code="item_revision_conflict",
                details={
                    "item_id": current.item_id,
                    "expected_revision": expected,
                    "current_revision": current.item_revision,
                },
            )

    @staticmethod
    def _require_representation(
        current: RepresentationAggregateSnapshot,
        representation_id: str,
    ) -> RepresentationRecordSnapshot:
        value = current.get(representation_id)
        if value is None:
            raise NotFoundError(
                "the representation does not exist",
                code="representation_not_found",
                details={
                    "item_id": current.item_id,
                    "representation_id": representation_id,
                },
            )
        if value.representation_id != representation_id:
            raise ConflictError(
                "the representation id differs only by case",
                code="representation_identity_alias",
                details={
                    "item_id": current.item_id,
                    "requested_representation_id": representation_id,
                    "current_representation_id": value.representation_id,
                },
            )
        return value

    @staticmethod
    def _match_representation_revision(
        current: RepresentationRecordSnapshot,
        expected: str,
        *,
        item_id: str,
    ) -> None:
        if current.revision != expected:
            raise ConflictError(
                "the representation changed elsewhere",
                code="representation_revision_conflict",
                details={
                    "item_id": item_id,
                    "representation_id": current.representation_id,
                    "expected_revision": expected,
                    "current_revision": current.revision,
                },
            )

    @staticmethod
    def _siblings(
        aggregate: RepresentationAggregateSnapshot,
        representation_id: str,
    ) -> tuple[RepresentationRecordSnapshot, ...]:
        folded = representation_id.casefold()
        return tuple(
            value
            for value in aggregate.representations
            if value.representation_id.casefold() != folded
        )

    def _validate_put(
        self,
        *,
        current: RepresentationAggregateSnapshot,
        staged: RepresentationAggregateSnapshot,
        draft: RepresentationAttachmentDraft,
        before: RepresentationRecordSnapshot | None,
    ) -> RepresentationRecordSnapshot:
        if staged.item_id != current.item_id:
            raise RepositoryError(
                "the representation repository staged another item",
                code="representation_repository_scope_mismatch",
            )
        if staged.item_revision == current.item_revision:
            raise RepositoryError(
                "the representation repository did not advance the item revision",
                code="item_revision_not_advanced",
            )
        if self._siblings(staged, draft.representation_id) != self._siblings(
            current, draft.representation_id
        ):
            raise RepositoryError(
                "the representation repository changed sibling representations",
                code="representation_repository_content_mismatch",
            )
        after = staged.get(draft.representation_id)
        if after is None:
            raise RepositoryError(
                "the representation repository did not stage the source",
                code="representation_repository_content_mismatch",
            )
        if after.representation_id != draft.representation_id:
            raise RepositoryError(
                "the representation repository changed the source identity",
                code="representation_repository_content_mismatch",
            )
        expected_disposition = (
            "referenced" if draft.acquisition == "reference" else "copied"
        )
        if (
            after.role != draft.role
            or after.media_type != draft.media_type
            or after.label != draft.label
            or after.metadata != draft.metadata
            or after.disposition != expected_disposition
            or not after.available
            or after.content_state != "unchanged"
            or not after.content_sha256
            or after.size is None
            or (
                bool(draft.expected_content_sha256)
                and after.content_sha256 != draft.expected_content_sha256
            )
            or (
                draft.expected_size is not None
                and after.size != draft.expected_size
            )
        ):
            raise RepositoryError(
                "the representation repository changed command content",
                code="representation_repository_content_mismatch",
            )
        if after.locator == draft.source_token:
            raise RepositoryError(
                "the representation repository exposed its source token",
                code="unsafe_representation_locator",
            )
        if before is not None and after.revision == before.revision:
            raise RepositoryError(
                "the representation repository did not advance the source revision",
                code="representation_revision_not_advanced",
            )
        return after

    def _validate_detach(
        self,
        *,
        current: RepresentationAggregateSnapshot,
        staged: RepresentationAggregateSnapshot,
        representation_id: str,
    ) -> None:
        if staged.item_id != current.item_id:
            raise RepositoryError(
                "the representation repository staged another item",
                code="representation_repository_scope_mismatch",
            )
        if staged.item_revision == current.item_revision:
            raise RepositoryError(
                "the representation repository did not advance the item revision",
                code="item_revision_not_advanced",
            )
        if staged.get(representation_id) is not None or self._siblings(
            staged, representation_id
        ) != self._siblings(current, representation_id):
            raise RepositoryError(
                "the representation repository staged the wrong detachment",
                code="representation_repository_content_mismatch",
            )

    @staticmethod
    def _replay(
        unit: RepresentationCommandUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        action: RepresentationMutationAction,
        item_id: str,
        representation_id: str,
        expected_item_revision: str,
        expected_representation_revision: str | None,
        draft: RepresentationAttachmentDraft | None = None,
    ) -> RepresentationCommandResult | None:
        prior = unit.receipt(operation_id)
        if prior is None:
            return None
        if not isinstance(prior, RepresentationMutationReceipt):
            raise RepositoryError(
                "the representation repository returned an invalid receipt",
                code="invalid_representation_receipt",
            )
        if prior.operation_id != operation_id:
            raise RepositoryError(
                "the representation repository returned another operation",
                code="receipt_scope_mismatch",
            )
        if prior.command_sha256 != command_sha256 or prior.action != action:
            raise ConflictError(
                "operation id was already used for another command",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        if (
            prior.item_id != item_id
            or prior.representation_id != representation_id
        ):
            raise RepositoryError(
                "the representation receipt has the wrong scope",
                code="receipt_scope_mismatch",
            )
        if prior.before_item_revision != expected_item_revision:
            raise RepositoryError(
                "the stored receipt has inconsistent item preconditions",
                code="invalid_representation_receipt",
            )
        before = prior.before
        if action == "attach":
            valid = before is None and expected_representation_revision is None
        else:
            valid = (
                before is not None
                and before.revision == expected_representation_revision
                and before.representation_id == representation_id
            )
        if not valid:
            raise RepositoryError(
                "the stored receipt has inconsistent source preconditions",
                code="invalid_representation_receipt",
            )
        if action in {"attach", "replace"}:
            after = prior.after
            expected_disposition = (
                "referenced"
                if draft is not None and draft.acquisition == "reference"
                else "copied"
            )
            if (
                draft is None
                or after is None
                or after.representation_id != representation_id
                or after.role != draft.role
                or after.media_type != draft.media_type
                or after.label != draft.label
                or after.metadata != draft.metadata
                or after.disposition != expected_disposition
                or not after.available
                or after.content_state != "unchanged"
                or not after.content_sha256
                or after.size is None
                or (
                    bool(draft.expected_content_sha256)
                    and after.content_sha256
                    != draft.expected_content_sha256
                )
                or (
                    draft.expected_size is not None
                    and after.size != draft.expected_size
                )
                or after.locator == draft.source_token
                or (before is not None and after.revision == before.revision)
            ):
                raise RepositoryError(
                    "the stored receipt has inconsistent source content",
                    code="invalid_representation_receipt",
                )
        return RepresentationCommandResult(prior, replayed=True)

    @staticmethod
    def _repository_failure(exc: Exception) -> RepositoryError:
        return RepositoryError(
            "the representation command repository failed",
            code="representation_repository_unavailable",
            details={"cause_type": type(exc).__name__},
            retryable=True,
        )


__all__ = [
    "AcquisitionMode",
    "AttachRepresentationCommand",
    "DetachRepresentationCommand",
    "RepresentationAggregateSnapshot",
    "RepresentationAttachmentDraft",
    "RepresentationCommandRepositoryPort",
    "RepresentationCommandResult",
    "RepresentationCommandService",
    "RepresentationCommandUnitOfWorkPort",
    "RepresentationContentState",
    "RepresentationDisposition",
    "RepresentationMutationAction",
    "RepresentationMutationReceipt",
    "RepresentationRecordSnapshot",
]
