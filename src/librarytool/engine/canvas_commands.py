"""Framework-neutral command for preparing representation canvases.

Canvas queries are intentionally passive: they may report that no prepared
sequence exists, but they never inspect media or mint canvas identities.  This
module defines the explicit command boundary that may do that work.

The repository owns resource addressing, media inspection, and all private
source positions.  The engine sees only opaque, fixed-size correlations in a
monotonic identity ledger.  Active entries bind the current sequence; retired
entries reserve identities removed from it.  Correlations are never included
in receipts; they exist solely so the service can prevent an adapter from
changing a surviving source's canvas identity or recycling an old identity
for a different source.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ContextManager, Protocol, TypeAlias

from .canvases import CanvasSequenceView
from .errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)


JsonMapping: TypeAlias = Mapping[str, Any]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CORRELATION_BYTES = 32


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a portable identifier")
    return value


def _revision(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if (
        not value
        or len(value) > 512
        or value != value.strip()
        or '"' in value
        or "\\" in value
        or any(character.isspace() for character in value)
        or any(
            ord(character) == 127
            or ord(character) < 32
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise ValueError(f"{field_name} is not a valid revision")
    return value


def _command_hash(value: JsonMapping) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class PrepareCanvasSequenceCommand:
    """Prepare one representation at one exact representation revision.

    The absence of paths, source positions, page numbers, media types, and
    provider options is intentional.  Those are repository concerns and must
    not become dependencies of a UI or transport.
    """

    item_id: str
    representation_id: str
    expected_representation_revision: str
    operation_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "item_id",
            "representation_id",
            "expected_representation_revision",
            "operation_id",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} must be a string")


@dataclass(frozen=True, slots=True)
class CanvasPreparationItemSnapshot:
    """Minimal live item state required to distinguish a missing item."""

    item_id: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")


@dataclass(frozen=True, slots=True)
class CanvasPreparationRepresentationSnapshot:
    """Minimal live representation state; resource locators are absent."""

    item_id: str
    representation_id: str
    revision: str

    def __post_init__(self) -> None:
        _identifier(self.item_id, "item_id")
        _identifier(self.representation_id, "representation_id")
        _revision(self.revision, "representation revision")


@dataclass(frozen=True, slots=True)
class CanvasSourceIdentityBinding:
    """Port-only link between a public canvas ID and a private source.

    ``source_correlation`` is an opaque 256-bit value derived and persisted by
    the repository.  It is deliberately bytes rather than arbitrary text, so
    a path, URI, page label, or raw source position cannot accidentally cross
    this boundary.  This type has no serializer and hides the value from its
    representation.
    """

    canvas_id: str
    source_correlation: bytes = field(repr=False)
    active: bool = True

    def __post_init__(self) -> None:
        _identifier(self.canvas_id, "canvas_id")
        if (
            not isinstance(self.source_correlation, bytes)
            or len(self.source_correlation) != _CORRELATION_BYTES
        ):
            raise ValueError(
                "source_correlation must be an opaque 256-bit byte string"
            )
        if not isinstance(self.active, bool):
            raise TypeError("active must be a boolean")


def _identity_bindings(
    values: Any,
) -> tuple[CanvasSourceIdentityBinding, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("identities must be an iterable")
    try:
        bindings = tuple(values)
    except TypeError as exc:
        raise TypeError("identities must be an iterable") from exc
    if any(not isinstance(value, CanvasSourceIdentityBinding) for value in bindings):
        raise TypeError("identities contain an invalid binding")
    canvas_aliases = [value.canvas_id.casefold() for value in bindings]
    if len(canvas_aliases) != len(set(canvas_aliases)):
        raise ValueError("identities contain duplicate canvas ids")
    correlations = [value.source_correlation for value in bindings]
    if len(correlations) != len(set(correlations)):
        raise ValueError("identities contain duplicate source correlations")
    return tuple(
        sorted(bindings, key=lambda value: (value.canvas_id.casefold(), value.canvas_id))
    )


@dataclass(frozen=True, slots=True)
class CanvasPreparationSnapshot:
    """A candidate query-shaped sequence plus its private identity ledger.

    This is a repository-port value, not a transport model, and deliberately
    has no serializer.  Query views and preparation receipt summaries are the
    only transport-safe canvas serialization paths.
    """

    sequence: CanvasSequenceView
    identities: tuple[CanvasSourceIdentityBinding, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, CanvasSequenceView):
            raise TypeError("sequence must be a CanvasSequenceView")
        identities = _identity_bindings(self.identities)
        sequence_ids = {canvas.key.canvas_id for canvas in self.sequence.canvases}
        active_binding_ids = {
            binding.canvas_id for binding in identities if binding.active
        }
        if sequence_ids != active_binding_ids:
            raise ValueError(
                "active identities must bind every canvas in the sequence "
                "exactly once"
            )
        object.__setattr__(self, "identities", identities)


@dataclass(frozen=True, slots=True)
class CanvasPreparationSequenceSummary:
    """Narrow durable/public summary of one prepared active sequence.

    The query service alone derives canonical public sequence and canvas
    revisions from a repository record.  Preparation receipts therefore do
    not persist or repeat an adapter-provided ``CanvasSequenceView.revision``;
    they report only the exact representation revision and ordered opaque
    canvas IDs needed to understand the command outcome.
    """

    representation_revision: str
    canvas_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _revision(self.representation_revision, "representation_revision")
        if isinstance(self.canvas_ids, (str, bytes)):
            raise TypeError("canvas_ids must be an iterable")
        try:
            canvas_ids = tuple(
                _identifier(value, "canvas_id") for value in self.canvas_ids
            )
        except TypeError as exc:
            raise TypeError("canvas_ids must be an iterable") from exc
        aliases = [value.casefold() for value in canvas_ids]
        if len(aliases) != len(set(aliases)):
            raise ValueError("canvas_ids contain duplicate identities")
        object.__setattr__(self, "canvas_ids", canvas_ids)

    @classmethod
    def from_sequence(
        cls,
        value: CanvasSequenceView,
    ) -> "CanvasPreparationSequenceSummary":
        if not isinstance(value, CanvasSequenceView):
            raise TypeError("value must be a CanvasSequenceView")
        return cls(
            representation_revision=value.representation_revision,
            canvas_ids=tuple(
                canvas.key.canvas_id for canvas in value.canvases
            ),
        )

    @classmethod
    def from_dict(cls, value: Any) -> "CanvasPreparationSequenceSummary":
        if not isinstance(value, Mapping):
            raise TypeError("canvas preparation summary must be an object")
        if set(value) != {"representation_revision", "canvas_ids"}:
            raise ValueError("canvas preparation summary fields do not match")
        return cls(
            representation_revision=value["representation_revision"],
            canvas_ids=value["canvas_ids"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "representation_revision": self.representation_revision,
            "canvas_ids": list(self.canvas_ids),
        }


@dataclass(frozen=True, slots=True)
class CanvasPreparationReceipt:
    """Durable preparation outcome with a transport-safe public form."""

    operation_id: str
    command_sha256: str = field(repr=False)
    item_id: str = ""
    representation_id: str = ""
    representation_revision: str = ""
    before: CanvasPreparationSequenceSummary | None = None
    after: CanvasPreparationSequenceSummary | None = None

    def __post_init__(self) -> None:
        _identifier(self.operation_id, "operation_id")
        if not isinstance(self.command_sha256, str) or not _SHA256_RE.fullmatch(
            self.command_sha256
        ):
            raise ValueError("command_sha256 is invalid")
        _identifier(self.item_id, "item_id")
        _identifier(self.representation_id, "representation_id")
        _revision(self.representation_revision, "representation_revision")
        if self.before is not None and not isinstance(
            self.before, CanvasPreparationSequenceSummary
        ):
            raise TypeError(
                "before must be a CanvasPreparationSequenceSummary or None"
            )
        if self.after is None:
            raise TypeError("after must be a CanvasPreparationSequenceSummary")
        if not isinstance(self.after, CanvasPreparationSequenceSummary):
            raise TypeError("after must be a CanvasPreparationSequenceSummary")
        if self.after.representation_revision != self.representation_revision:
            raise ValueError("after does not match the prepared representation revision")

    @classmethod
    def from_storage_dict(cls, value: Any) -> "CanvasPreparationReceipt":
        """Rehydrate the private durable form used by repositories."""

        if not isinstance(value, Mapping):
            raise TypeError("canvas preparation receipt must be an object")
        fields = {
            "operation_id",
            "command_sha256",
            "item_id",
            "representation_id",
            "representation_revision",
            "before",
            "after",
        }
        if set(value) != fields:
            raise ValueError("canvas preparation receipt fields do not match")
        before = value["before"]
        after = value["after"]
        return cls(
            operation_id=value["operation_id"],
            command_sha256=value["command_sha256"],
            item_id=value["item_id"],
            representation_id=value["representation_id"],
            representation_revision=value["representation_revision"],
            before=(
                None
                if before is None
                else CanvasPreparationSequenceSummary.from_dict(before)
            ),
            after=(
                None
                if after is None
                else CanvasPreparationSequenceSummary.from_dict(after)
            ),
        )

    def as_storage_dict(self) -> dict[str, Any]:
        """Return the private form for the repository's durable receipt log."""

        return {
            "operation_id": self.operation_id,
            "command_sha256": self.command_sha256,
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "representation_revision": self.representation_revision,
            "before": None if self.before is None else self.before.as_dict(),
            "after": self.after.as_dict() if self.after is not None else None,
        }

    def as_public_dict(self) -> dict[str, Any]:
        """Return a receipt with no command fingerprint or source evidence."""

        value = self.as_storage_dict()
        value.pop("command_sha256")
        return value


@dataclass(frozen=True, slots=True)
class CanvasPreparationResult:
    receipt: CanvasPreparationReceipt
    replayed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, CanvasPreparationReceipt):
            raise TypeError("receipt must be a CanvasPreparationReceipt")
        if not isinstance(self.replayed, bool):
            raise TypeError("replayed must be a boolean")

    def as_dict(self) -> dict[str, Any]:
        return {
            "replayed": self.replayed,
            "receipt": self.receipt.as_public_dict(),
        }


class CanvasPreparationUnitOfWorkPort(Protocol):
    """One coherent live/index snapshot and atomic publication boundary.

    The implementation holds one isolation scope from context entry through
    ``commit``.  ``stage_prepare`` performs media inspection and stages both
    the public index and its private source-correlation ledger.  The staged
    ledger must retain every prior binding, marking removed canvases retired
    rather than deleting them.  ``stage_prepare`` must not publish either
    state.  ``commit`` atomically publishes them with the durable receipt;
    exit without commit discards them.
    """

    def receipt(self, operation_id: str) -> CanvasPreparationReceipt | None: ...

    def get_item(self, item_id: str) -> CanvasPreparationItemSnapshot | None: ...

    def get_representation(
        self,
        item_id: str,
        representation_id: str,
    ) -> CanvasPreparationRepresentationSnapshot | None: ...

    def get_preparation(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
    ) -> CanvasPreparationSnapshot | None: ...

    def stage_prepare(
        self,
        representation: CanvasPreparationRepresentationSnapshot,
        before: CanvasPreparationSnapshot | None,
    ) -> CanvasPreparationSnapshot: ...

    def commit(self, receipt: CanvasPreparationReceipt) -> None: ...


class CanvasPreparationRepositoryPort(Protocol):
    """Open an operation-scoped canvas preparation unit of work."""

    def unit_of_work(
        self,
        *,
        operation_id: str,
    ) -> ContextManager[CanvasPreparationUnitOfWorkPort]: ...


class CanvasPreparationService:
    """Idempotently prepare a canvas sequence at an exact source revision."""

    def __init__(self, repository: CanvasPreparationRepositoryPort) -> None:
        self._repository = repository

    def prepare(
        self,
        command: PrepareCanvasSequenceCommand,
    ) -> CanvasPreparationResult:
        if not isinstance(command, PrepareCanvasSequenceCommand):
            raise ValidationError(
                "prepare requires a PrepareCanvasSequenceCommand",
                code="invalid_canvas_preparation_command",
            )
        item_id = self._item_id(command.item_id)
        representation_id = self._representation_id(command.representation_id)
        expected_revision = self._expected_revision(
            command.expected_representation_revision,
            item_id=item_id,
            representation_id=representation_id,
        )
        operation_id = self._operation_id(command.operation_id)
        command_sha256 = _command_hash(
            {
                "action": "prepare",
                "item_id": item_id,
                "representation_id": representation_id,
                "expected_representation_revision": expected_revision,
            }
        )
        try:
            with self._repository.unit_of_work(operation_id=operation_id) as unit:
                replay = self._replay(
                    unit,
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    representation_id=representation_id,
                    expected_revision=expected_revision,
                )
                if replay is not None:
                    return replay

                item = self._current_item(unit.get_item(item_id), item_id=item_id)
                representation = self._current_representation(
                    unit.get_representation(item.item_id, representation_id),
                    item_id=item_id,
                    representation_id=representation_id,
                )
                self._match_revision(representation, expected_revision)
                before = self._preparation(
                    unit.get_preparation(representation),
                    item_id=item_id,
                    representation_id=representation_id,
                )
                after = self._preparation(
                    unit.stage_prepare(representation, before),
                    item_id=item_id,
                    representation_id=representation_id,
                    required=True,
                )
                assert after is not None
                if after.sequence.representation_revision != expected_revision:
                    raise RepositoryError(
                        "the canvas repository prepared another representation revision",
                        code="canvas_preparation_revision_mismatch",
                        details={
                            "item_id": item_id,
                            "representation_id": representation_id,
                        },
                    )
                self._validate_identity_continuity(before, after)
                receipt = CanvasPreparationReceipt(
                    operation_id=operation_id,
                    command_sha256=command_sha256,
                    item_id=item_id,
                    representation_id=representation_id,
                    representation_revision=expected_revision,
                    before=(
                        None
                        if before is None
                        else CanvasPreparationSequenceSummary.from_sequence(
                            before.sequence
                        )
                    ),
                    after=CanvasPreparationSequenceSummary.from_sequence(
                        after.sequence
                    ),
                )
                unit.commit(receipt)
                return CanvasPreparationResult(receipt, replayed=False)
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the canvas preparation repository failed",
                code="canvas_preparation_repository_unavailable",
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc

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
        item_id: str,
        representation_id: str,
    ) -> str:
        if not value:
            raise PreconditionRequiredError(
                "an exact representation revision is required",
                code="representation_revision_required",
                details={
                    "item_id": item_id,
                    "representation_id": representation_id,
                },
            )
        try:
            return _revision(value, "expected_representation_revision")
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                "the expected representation revision is invalid",
                code="invalid_representation_revision",
                details={
                    "item_id": item_id,
                    "representation_id": representation_id,
                },
            ) from exc

    @staticmethod
    def _current_item(
        value: Any,
        *,
        item_id: str,
    ) -> CanvasPreparationItemSnapshot:
        if value is None:
            raise NotFoundError(
                "the item does not exist",
                code="item_not_found",
                details={"item_id": item_id},
            )
        if not isinstance(value, CanvasPreparationItemSnapshot):
            raise RepositoryError(
                "the canvas repository returned an invalid item snapshot",
                code="invalid_canvas_preparation_item_snapshot",
            )
        if value.item_id != item_id:
            if value.item_id.casefold() == item_id.casefold():
                raise ConflictError(
                    "the item id differs only by case",
                    code="item_identity_alias",
                    details={
                        "requested_item_id": item_id,
                        "current_item_id": value.item_id,
                    },
                )
            raise RepositoryError(
                "the canvas repository returned another item",
                code="canvas_preparation_repository_scope_mismatch",
            )
        return value

    @staticmethod
    def _current_representation(
        value: Any,
        *,
        item_id: str,
        representation_id: str,
    ) -> CanvasPreparationRepresentationSnapshot:
        if value is None:
            raise NotFoundError(
                "the representation does not exist",
                code="representation_not_found",
                details={
                    "item_id": item_id,
                    "representation_id": representation_id,
                },
            )
        if not isinstance(value, CanvasPreparationRepresentationSnapshot):
            raise RepositoryError(
                "the canvas repository returned an invalid representation snapshot",
                code="invalid_canvas_preparation_representation_snapshot",
            )
        if value.item_id != item_id:
            raise RepositoryError(
                "the canvas repository returned another item",
                code="canvas_preparation_repository_scope_mismatch",
            )
        if value.representation_id != representation_id:
            if value.representation_id.casefold() == representation_id.casefold():
                raise ConflictError(
                    "the representation id differs only by case",
                    code="representation_identity_alias",
                    details={
                        "item_id": item_id,
                        "requested_representation_id": representation_id,
                        "current_representation_id": value.representation_id,
                    },
                )
            raise RepositoryError(
                "the canvas repository returned another representation",
                code="canvas_preparation_repository_scope_mismatch",
            )
        return value

    @staticmethod
    def _match_revision(
        current: CanvasPreparationRepresentationSnapshot,
        expected: str,
    ) -> None:
        if current.revision != expected:
            raise ConflictError(
                "the representation changed elsewhere",
                code="representation_revision_conflict",
                details={
                    "item_id": current.item_id,
                    "representation_id": current.representation_id,
                    "expected_revision": expected,
                    "current_revision": current.revision,
                },
            )

    @staticmethod
    def _preparation(
        value: Any,
        *,
        item_id: str,
        representation_id: str,
        required: bool = False,
    ) -> CanvasPreparationSnapshot | None:
        if value is None and not required:
            return None
        if not isinstance(value, CanvasPreparationSnapshot):
            raise RepositoryError(
                "the canvas repository returned an invalid preparation snapshot",
                code="invalid_canvas_preparation_snapshot",
            )
        if (
            value.sequence.item_id != item_id
            or value.sequence.representation_id != representation_id
        ):
            raise RepositoryError(
                "the canvas repository returned a preparation outside its scope",
                code="canvas_preparation_repository_scope_mismatch",
            )
        return value

    @staticmethod
    def _validate_identity_continuity(
        before: CanvasPreparationSnapshot | None,
        after: CanvasPreparationSnapshot,
    ) -> None:
        if before is None:
            return
        before_by_source = {
            value.source_correlation: value
            for value in before.identities
        }
        after_by_source = {
            value.source_correlation: value
            for value in after.identities
        }
        after_by_id = {
            value.canvas_id.casefold(): value
            for value in after.identities
        }
        for source, before_binding in before_by_source.items():
            after_binding = after_by_source.get(source)
            if after_binding is None:
                replacement = after_by_id.get(before_binding.canvas_id.casefold())
                if replacement is not None:
                    raise RepositoryError(
                        "the canvas repository reused an existing canvas identity",
                        code="canvas_identity_reused",
                        details={
                            "item_id": after.sequence.item_id,
                            "representation_id": after.sequence.representation_id,
                            "canvas_id": replacement.canvas_id,
                        },
                    )
                raise RepositoryError(
                    "the canvas repository dropped identity history",
                    code="canvas_identity_ledger_dropped",
                    details={
                        "item_id": after.sequence.item_id,
                        "representation_id": after.sequence.representation_id,
                        "canvas_id": before_binding.canvas_id,
                    },
                )
            if before_binding.canvas_id != after_binding.canvas_id:
                raise RepositoryError(
                    "the canvas repository changed a surviving canvas identity",
                    code="canvas_identity_changed",
                    details={
                        "item_id": after.sequence.item_id,
                        "representation_id": after.sequence.representation_id,
                        "before_canvas_id": before_binding.canvas_id,
                        "after_canvas_id": after_binding.canvas_id,
                    },
                )

        before_active_sources = {
            value.source_correlation for value in before.identities if value.active
        }
        after_active_sources = {
            value.source_correlation for value in after.identities if value.active
        }
        if (
            before.sequence.representation_revision
            == after.sequence.representation_revision
            and before_active_sources != after_active_sources
        ):
            raise RepositoryError(
                "the canvas repository destructively changed an unchanged source",
                code="canvas_source_set_changed",
                details={
                    "item_id": after.sequence.item_id,
                    "representation_id": after.sequence.representation_id,
                    "before_count": len(before_active_sources),
                    "after_count": len(after_active_sources),
                },
            )

    @staticmethod
    def _replay(
        unit: CanvasPreparationUnitOfWorkPort,
        *,
        operation_id: str,
        command_sha256: str,
        item_id: str,
        representation_id: str,
        expected_revision: str,
    ) -> CanvasPreparationResult | None:
        prior = unit.receipt(operation_id)
        if prior is None:
            return None
        if not isinstance(prior, CanvasPreparationReceipt):
            raise RepositoryError(
                "the canvas repository returned an invalid preparation receipt",
                code="invalid_canvas_preparation_receipt",
            )
        if prior.operation_id != operation_id:
            raise RepositoryError(
                "the canvas repository returned another operation receipt",
                code="receipt_scope_mismatch",
            )
        if prior.command_sha256 != command_sha256:
            raise ConflictError(
                "operation id was already used for another canvas command",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        if (
            prior.item_id != item_id
            or prior.representation_id != representation_id
            or prior.representation_revision != expected_revision
        ):
            raise RepositoryError(
                "the canvas repository returned a receipt outside its scope",
                code="receipt_scope_mismatch",
            )
        return CanvasPreparationResult(prior, replayed=True)


__all__ = [
    "CanvasPreparationItemSnapshot",
    "CanvasPreparationReceipt",
    "CanvasPreparationRepositoryPort",
    "CanvasPreparationRepresentationSnapshot",
    "CanvasPreparationResult",
    "CanvasPreparationSequenceSummary",
    "CanvasPreparationService",
    "CanvasPreparationSnapshot",
    "CanvasPreparationUnitOfWorkPort",
    "CanvasSourceIdentityBinding",
    "PrepareCanvasSequenceCommand",
]
