"""Named, reusable processing recipes saved per workspace.

A preset is a human-authored name for an ordered
:mod:`librarytool.processing.operations` pipeline, optionally terminated by a
manual binary adjustment, and optionally associated with one image category.
That association is what makes a batch meaningful: "apply the Cover preset to
every cover in this book" is a category query plus one transform per match.

Unlike :class:`~librarytool.engine.correction_transforms.CorrectionTransformCommand`,
a preset is *not* content-addressed history — it is mutable, user-owned
configuration with no stored fingerprints depending on its byte layout.  So this
schema takes the opposite serialization rule: every field is always present,
including empty ones.  Do not copy the omit-when-absent rule from the transform
command here, and do not copy this rule back there.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from librarytool.processing.operations import (
    ProcessingOperation,
    ProcessingOperationError,
    operations_from_list,
)
from librarytool.processing.raster import ManualBinaryAdjustRecipe

from .errors import ConflictError, NotFoundError, ValidationError
from .raster_artifacts import IMAGE_CATEGORIES

PROCESSING_PRESET_SCHEMA = "org.whl.processing-preset"
PROCESSING_PRESET_VERSION = 1
MAX_PRESETS_PER_WORKSPACE = 256
MAX_PRESET_NAME_LENGTH = 120

_PRESET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRESET_FIELDS = frozenset(
    {
        "schema",
        "version",
        "preset_id",
        "name",
        "category",
        "operations",
        "adjustment",
    }
)
_ADJUSTMENT_FIELDS = frozenset(
    {
        "schema",
        "version",
        "algorithm",
        "contrast_percent",
        "brightness_percent",
        "threshold",
        "threshold_rule",
        "comparison",
    }
)


def _validation(message: str, *, field_name: str) -> ValidationError:
    return ValidationError(
        message,
        code="invalid_processing_preset",
        details={"field": field_name},
    )


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProcessingPreset:
    """One named recipe a reviewer can apply or batch-apply."""

    preset_id: str
    name: str
    operations: tuple[ProcessingOperation, ...] = ()
    adjustment: ManualBinaryAdjustRecipe | None = None
    category: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.preset_id, str) or not _PRESET_ID_RE.fullmatch(
            self.preset_id
        ):
            raise _validation(
                "preset_id must be a portable opaque identifier",
                field_name="preset_id",
            )
        name = self.name
        if not isinstance(name, str):
            raise _validation("name must be a string", field_name="name")
        # Display names are trimmed rather than rejected for surrounding space:
        # a reviewer typing into a text field should not have to see an error
        # for something the field can fix itself.
        name = name.strip()
        if not name or len(name) > MAX_PRESET_NAME_LENGTH:
            raise _validation(
                "name must be a non-empty string of at most "
                f"{MAX_PRESET_NAME_LENGTH} characters",
                field_name="name",
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in name):
            raise _validation(
                "name contains an unsafe control character",
                field_name="name",
            )
        object.__setattr__(self, "name", name)
        operations = tuple(self.operations)
        if any(not isinstance(value, ProcessingOperation) for value in operations):
            raise _validation(
                "operations must contain ProcessingOperation values",
                field_name="operations",
            )
        object.__setattr__(self, "operations", operations)
        if self.adjustment is not None and not isinstance(
            self.adjustment,
            ManualBinaryAdjustRecipe,
        ):
            raise _validation(
                "adjustment must be a ManualBinaryAdjustRecipe or null",
                field_name="adjustment",
            )
        if not operations and self.adjustment is None:
            raise _validation(
                "a preset must carry at least one operation or an adjustment",
                field_name="operations",
            )
        # "" means "not associated with a category", which is different from a
        # category that happens to be unknown.
        if not isinstance(self.category, str) or (
            self.category and self.category not in IMAGE_CATEGORIES
        ):
            raise _validation(
                "category must be empty or one of: "
                + ", ".join(sorted(IMAGE_CATEGORIES)),
                field_name="category",
            )

    @property
    def revision(self) -> str:
        """A content-derived optimistic-concurrency token.

        Deriving it from the content rather than a counter keeps the store
        stateless: any reader can recompute a preset's revision without having
        seen its history.
        """

        return hashlib.sha256(_canonical_json(self.as_dict())).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PROCESSING_PRESET_SCHEMA,
            "version": PROCESSING_PRESET_VERSION,
            "preset_id": self.preset_id,
            "name": self.name,
            "category": self.category,
            "operations": [value.as_dict() for value in self.operations],
            "adjustment": (
                None if self.adjustment is None else self.adjustment.as_dict()
            ),
        }

    def as_view(self) -> dict[str, Any]:
        """The wire shape, which adds the derived revision."""

        return {**self.as_dict(), "revision": self.revision}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProcessingPreset":
        if not isinstance(payload, Mapping):
            raise _validation("a preset must be an object", field_name="preset")
        payload_fields = frozenset(payload)
        if payload_fields != _PRESET_FIELDS:
            raise ValidationError(
                "processing preset fields must match its schema exactly",
                code="invalid_processing_preset",
                details={
                    "field": "preset",
                    "missing": sorted(_PRESET_FIELDS - payload_fields),
                    "unknown": sorted(
                        str(value) for value in payload_fields - _PRESET_FIELDS
                    ),
                },
            )
        version = payload.get("version")
        if (
            payload.get("schema") != PROCESSING_PRESET_SCHEMA
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version != PROCESSING_PRESET_VERSION
        ):
            raise _validation(
                "unsupported processing preset schema",
                field_name="schema",
            )
        operations_payload = payload["operations"]
        operations: tuple[ProcessingOperation, ...] = ()
        if operations_payload:
            try:
                operations = operations_from_list(operations_payload)
            except ProcessingOperationError as exc:
                raise _validation(str(exc), field_name="operations") from exc
        elif not isinstance(operations_payload, Sequence) or isinstance(
            operations_payload,
            (str, bytes),
        ):
            raise _validation("operations must be a sequence", field_name="operations")
        adjustment = _adjustment_from_dict(payload["adjustment"])
        return cls(
            preset_id=payload["preset_id"],
            name=payload["name"],
            operations=operations,
            adjustment=adjustment,
            category=payload["category"],
        )


def _adjustment_from_dict(
    payload: Any,
) -> ManualBinaryAdjustRecipe | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping) or frozenset(payload) != _ADJUSTMENT_FIELDS:
        raise _validation(
            "manual binary adjustment fields must match its schema exactly",
            field_name="adjustment",
        )
    version = payload.get("version")
    if (
        payload.get("schema") != "org.whl.raster.manual-binary-adjust"
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version != 1
    ):
        raise _validation(
            "unsupported manual binary adjustment schema",
            field_name="adjustment.schema",
        )
    try:
        adjustment = ManualBinaryAdjustRecipe(
            contrast=payload["contrast_percent"],
            brightness=payload["brightness_percent"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _validation(
            "adjustment contains invalid manual binary parameters",
            field_name="adjustment",
        ) from exc
    canonical = adjustment.as_dict()
    if any(
        type(payload[name]) is not type(canonical[name]) or payload[name] != canonical[name]
        for name in _ADJUSTMENT_FIELDS
    ):
        raise _validation(
            "manual binary adjustment values are not canonical",
            field_name="adjustment",
        )
    return adjustment


@runtime_checkable
class ProcessingPresetStorePort(Protocol):
    """Atomic per-workspace persistence for the preset collection.

    Mutations are read-modify-write against a shared document, so the adapter
    owns the lock and performs the compare internally.  A service that read,
    compared, then wrote would leave a window for a concurrent editor.
    """

    def list_presets(self) -> tuple[ProcessingPreset, ...]: ...

    def create_preset(self, preset: ProcessingPreset) -> ProcessingPreset: ...

    def update_preset(
        self,
        preset: ProcessingPreset,
        expected_revision: str,
    ) -> ProcessingPreset: ...

    def delete_preset(self, preset_id: str, expected_revision: str) -> None: ...


class ProcessingPresetService:
    """Validate preset commands and delegate atomicity to the store."""

    def __init__(self, store: ProcessingPresetStorePort) -> None:
        if not isinstance(store, ProcessingPresetStorePort):
            raise TypeError("store must implement ProcessingPresetStorePort")
        self._store = store

    def list_presets(self) -> tuple[ProcessingPreset, ...]:
        return tuple(self._store.list_presets())

    def presets_for_category(self, category: str) -> tuple[ProcessingPreset, ...]:
        if category not in IMAGE_CATEGORIES:
            raise _validation(
                "category is not in the canonical image vocabulary",
                field_name="category",
            )
        return tuple(
            value for value in self._store.list_presets() if value.category == category
        )

    def create_preset(self, preset: ProcessingPreset) -> ProcessingPreset:
        if not isinstance(preset, ProcessingPreset):
            raise TypeError("preset must be a ProcessingPreset")
        return self._store.create_preset(preset)

    def update_preset(
        self,
        preset: ProcessingPreset,
        expected_revision: str,
    ) -> ProcessingPreset:
        if not isinstance(preset, ProcessingPreset):
            raise TypeError("preset must be a ProcessingPreset")
        return self._store.update_preset(preset, _revision_token(expected_revision))

    def delete_preset(self, preset_id: str, expected_revision: str) -> None:
        if not isinstance(preset_id, str) or not _PRESET_ID_RE.fullmatch(preset_id):
            raise _validation(
                "preset_id must be a portable opaque identifier",
                field_name="preset_id",
            )
        self._store.delete_preset(preset_id, _revision_token(expected_revision))


def _revision_token(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"^[0-9a-f]{64}$", value):
        raise ValidationError(
            "expected_revision must be a preset revision token",
            code="invalid_processing_preset_revision",
            details={"field": "expected_revision"},
        )
    return value


def preset_conflict(preset_id: str, *, code: str, message: str) -> ConflictError:
    return ConflictError(message, code=code, details={"preset_id": preset_id})


def preset_not_found(preset_id: str) -> NotFoundError:
    return NotFoundError(
        "the processing preset does not exist",
        code="processing_preset_not_found",
        details={"preset_id": preset_id},
    )


__all__ = [
    "MAX_PRESETS_PER_WORKSPACE",
    "MAX_PRESET_NAME_LENGTH",
    "PROCESSING_PRESET_SCHEMA",
    "PROCESSING_PRESET_VERSION",
    "ProcessingPreset",
    "ProcessingPresetService",
    "ProcessingPresetStorePort",
    "preset_conflict",
    "preset_not_found",
]
