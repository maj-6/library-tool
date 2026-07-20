"""Framework-neutral, read-only canvas query contracts.

A canvas is one ordered spatial or temporal coordinate space owned by a
representation.  This boundary deliberately does not expose the resource that
backs a canvas.  Resource addressing and delivery belong to a later engine
slice, so adapters must not leak paths, filenames, URIs, asset references, or
storage positions through these views.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias

from .errors import (
    EngineError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)


JsonMapping: TypeAlias = Mapping[str, Any]

_EMPTY_MAPPING: JsonMapping = MappingProxyType({})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PRIVATE_METADATA_KEYS = frozenset(
    {
        "asset",
        "asset_id",
        "asset_ref",
        "file",
        "file_name",
        "filename",
        "filepath",
        "locator",
        "ordinal",
        "path",
        "source_position",
        "storage_locator",
        "storage_position",
        "uri",
        "url",
    }
)


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValidationError(
            f"{field_name} must be a portable identifier",
            code="invalid_canvas_identity",
            details={"field": field_name},
        )
    return value


def _text(value: Any, field_name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValidationError(
            f"{field_name} must be a string no longer than {maximum} characters",
            code="invalid_canvas_contract",
            details={"field": field_name},
        )
    if any(
        ord(character) == 127
        or (ord(character) < 32 and character not in "\n\r\t")
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValidationError(
            f"{field_name} contains a control character",
            code="invalid_canvas_contract",
            details={"field": field_name},
        )
    return value


def _revision(value: Any, field_name: str) -> str:
    revision = _text(value, field_name, maximum=512)
    if (
        not revision
        or revision != revision.strip()
        or '"' in revision
        or "\\" in revision
        or any(character.isspace() for character in revision)
    ):
        raise ValidationError(
            f"{field_name} is not a valid revision",
            code="invalid_canvas_revision",
            details={"field": field_name},
        )
    return revision


def _private_metadata_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _PRIVATE_METADATA_KEYS


def _freeze_json(
    value: Any,
    *,
    path: str,
    active: set[int] | None = None,
    depth: int = 0,
) -> Any:
    """Detach and recursively freeze strict, public JSON data."""

    if depth > 64:
        raise ValidationError(
            "canvas metadata is nested too deeply",
            code="invalid_canvas_metadata",
            details={"path": path},
        )

    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            return _text(value, path, maximum=1_000_000)
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValidationError(
            "canvas metadata contains a non-finite number",
            code="invalid_canvas_metadata",
            details={"path": path},
        )

    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValidationError(
                "canvas metadata contains a reference cycle",
                code="invalid_canvas_metadata",
                details={"path": path},
            )
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key or key != key.strip():
                    raise ValidationError(
                        "canvas metadata keys must be non-empty, trimmed strings",
                        code="invalid_canvas_metadata",
                        details={"path": path},
                    )
                _text(key, f"{path} object key", maximum=256)
                if _private_metadata_key(key):
                    raise ValidationError(
                        "canvas metadata contains a private resource field",
                        code="private_canvas_metadata",
                        details={"path": f"{path}.{key}"},
                    )
                frozen[key] = _freeze_json(
                    item,
                    path=f"{path}.{key}",
                    active=active,
                    depth=depth + 1,
                )
            return MappingProxyType(frozen)
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValidationError(
                "canvas metadata contains a reference cycle",
                code="invalid_canvas_metadata",
                details={"path": path},
            )
        active.add(identity)
        try:
            return tuple(
                _freeze_json(
                    item,
                    path=f"{path}[{index}]",
                    active=active,
                    depth=depth + 1,
                )
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise ValidationError(
        "canvas metadata contains non-JSON data",
        code="invalid_canvas_metadata",
        details={"path": path, "value_type": type(value).__name__},
    )


def _metadata(value: Any) -> JsonMapping:
    if not isinstance(value, Mapping):
        raise ValidationError(
            "canvas metadata must be an object",
            code="invalid_canvas_metadata",
            details={"path": "$.canvas.metadata"},
        )
    result = _freeze_json(value, path="$.canvas.metadata")
    assert isinstance(result, Mapping)
    return result


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _derived_revision(prefix: str, value: Any) -> str:
    payload = json.dumps(
        _thaw(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _positive_number(value: Any, field_name: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(
            f"{field_name} must be a positive finite number or null",
            code="invalid_canvas_extent",
            details={"field": field_name},
        )
    if isinstance(value, int):
        if value <= 0:
            raise ValidationError(
                f"{field_name} must be a positive finite number or null",
                code="invalid_canvas_extent",
                details={"field": field_name},
            )
        return value
    number = value
    if not math.isfinite(number) or number <= 0:
        raise ValidationError(
            f"{field_name} must be a positive finite number or null",
            code="invalid_canvas_extent",
            details={"field": field_name},
        )
    if number.is_integer():
        return int(number)
    return number


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(
            f"{field_name} must be a non-negative integer",
            code="invalid_canvas_order",
            details={"field": field_name},
        )
    return value


def _resource_kinds(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValidationError(
            "resource_kinds must be an array of portable identifiers",
            code="invalid_canvas_resource_kinds",
            details={"field": "resource_kinds"},
        )
    try:
        kinds = tuple(_identifier(item, "resource_kind") for item in value)
    except ValidationError as exc:
        raise ValidationError(
            "resource_kinds must contain portable identifiers",
            code="invalid_canvas_resource_kinds",
            details={"field": "resource_kinds"},
        ) from exc
    folded = [item.casefold() for item in kinds]
    if len(folded) != len(set(folded)):
        raise ValidationError(
            "resource kinds must be unique ignoring case",
            code="duplicate_canvas_resource_kind",
            details={"field": "resource_kinds"},
        )
    return tuple(sorted(kinds, key=lambda item: (item.casefold(), item)))


@dataclass(frozen=True, slots=True)
class CanvasKey:
    """Stable identity for one canvas within an item representation."""

    item_id: str
    representation_id: str
    canvas_id: str

    def __post_init__(self) -> None:
        for name in ("item_id", "representation_id", "canvas_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))

    def as_dict(self) -> dict[str, str]:
        return {
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "canvas_id": self.canvas_id,
        }


@dataclass(frozen=True, slots=True)
class CanvasExtent:
    """Public spatial extent and optional temporal duration.

    Width and height form one pair and use ``unit`` (for example ``px`` or
    ``mm``).  ``duration`` is measured in seconds.  An empty extent is valid
    when a repository cannot yet determine either coordinate space.
    """

    width: int | float | None = None
    height: int | float | None = None
    unit: str = ""
    duration: int | float | None = None

    def __post_init__(self) -> None:
        width = _positive_number(self.width, "extent.width")
        height = _positive_number(self.height, "extent.height")
        duration = _positive_number(self.duration, "extent.duration")
        if not isinstance(self.unit, str):
            raise ValidationError(
                "canvas extent unit must be a string",
                code="invalid_canvas_extent",
                details={"field": "extent.unit"},
            )
        if (width is None) != (height is None):
            raise ValidationError(
                "canvas width and height must be supplied together",
                code="invalid_canvas_extent",
                details={"field": "extent"},
            )
        if width is None:
            if self.unit:
                raise ValidationError(
                    "canvas extent unit requires width and height",
                    code="invalid_canvas_extent",
                    details={"field": "extent.unit"},
                )
        else:
            try:
                _identifier(self.unit, "extent.unit")
            except ValidationError as exc:
                raise ValidationError(
                    "canvas extent unit must be a portable identifier",
                    code="invalid_canvas_extent",
                    details={"field": "extent.unit"},
                ) from exc
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "duration", duration)

    def as_dict(self) -> dict[str, int | float | str]:
        value: dict[str, int | float | str] = {}
        if self.width is not None and self.height is not None:
            value.update(
                {"width": self.width, "height": self.height, "unit": self.unit}
            )
        if self.duration is not None:
            value["duration"] = self.duration
        return value


@dataclass(frozen=True, slots=True)
class CanvasView:
    """Immutable public state for one ordered canvas."""

    key: CanvasKey
    revision: str
    order: int
    label: str = ""
    extent: CanvasExtent = field(default_factory=CanvasExtent)
    available: bool = True
    resource_kinds: tuple[str, ...] = ()
    metadata: JsonMapping = field(default_factory=lambda: _EMPTY_MAPPING)

    def __post_init__(self) -> None:
        if not isinstance(self.key, CanvasKey):
            raise ValidationError(
                "canvas key must be a CanvasKey",
                code="invalid_canvas_identity",
                details={"field": "key"},
            )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        object.__setattr__(self, "order", _non_negative_int(self.order, "order"))
        object.__setattr__(self, "label", _text(self.label, "label", maximum=512))
        if not isinstance(self.extent, CanvasExtent):
            raise ValidationError(
                "canvas extent must be a CanvasExtent",
                code="invalid_canvas_extent",
                details={"field": "extent"},
            )
        if not isinstance(self.available, bool):
            raise ValidationError(
                "canvas availability must be a boolean",
                code="invalid_canvas_availability",
                details={"field": "available"},
            )
        object.__setattr__(self, "resource_kinds", _resource_kinds(self.resource_kinds))
        object.__setattr__(self, "metadata", _metadata(self.metadata))

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.as_dict(),
            "revision": self.revision,
            "order": self.order,
            "label": self.label,
            "extent": self.extent.as_dict(),
            "available": self.available,
            "resource_kinds": list(self.resource_kinds),
            "metadata": _thaw(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CanvasSequenceView:
    """Ordered canvases and revisions for one representation."""

    item_id: str
    representation_id: str
    representation_revision: str
    revision: str
    canvases: tuple[CanvasView, ...]

    def __post_init__(self) -> None:
        item_id = _identifier(self.item_id, "item_id")
        representation_id = _identifier(self.representation_id, "representation_id")
        object.__setattr__(self, "item_id", item_id)
        object.__setattr__(self, "representation_id", representation_id)
        object.__setattr__(
            self,
            "representation_revision",
            _revision(self.representation_revision, "representation_revision"),
        )
        object.__setattr__(self, "revision", _revision(self.revision, "revision"))
        if isinstance(self.canvases, (str, bytes)) or not isinstance(
            self.canvases, Sequence
        ):
            raise ValidationError(
                "canvases must be an array",
                code="invalid_canvas_sequence",
                details={"field": "canvases"},
            )
        canvases = tuple(self.canvases)
        if any(not isinstance(canvas, CanvasView) for canvas in canvases):
            raise ValidationError(
                "canvases contains an invalid view",
                code="invalid_canvas_sequence",
                details={"field": "canvases"},
            )
        for canvas in canvases:
            if (
                canvas.key.item_id != item_id
                or canvas.key.representation_id != representation_id
            ):
                raise ValidationError(
                    "a canvas key does not belong to its sequence",
                    code="canvas_scope_mismatch",
                    details={"canvas_id": canvas.key.canvas_id},
                )
        folded_ids = [canvas.key.canvas_id.casefold() for canvas in canvases]
        if len(folded_ids) != len(set(folded_ids)):
            raise ValidationError(
                "canvas ids must be unique ignoring case",
                code="duplicate_canvas_identity",
                details={
                    "canvas_ids": sorted(
                        (canvas.key.canvas_id for canvas in canvases),
                        key=lambda value: (value.casefold(), value),
                    )
                },
            )
        orders = [canvas.order for canvas in canvases]
        if len(orders) != len(set(orders)):
            raise ValidationError(
                "canvas order values must be unique",
                code="duplicate_canvas_order",
                details={"orders": sorted(orders)},
            )
        object.__setattr__(
            self,
            "canvases",
            tuple(sorted(canvases, key=lambda canvas: canvas.order)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "representation_id": self.representation_id,
            "representation_revision": self.representation_revision,
            "revision": self.revision,
            "canvases": [canvas.as_dict() for canvas in self.canvases],
        }


class CanvasSequenceUnavailableError(EngineError):
    """A representation has no queryable canvas sequence yet."""

    default_code = "canvas_sequence_unavailable"


class CanvasQueryRepositoryPort(Protocol):
    """Return one representation-scoped, mapping-shaped canvas snapshot."""

    def get_sequence_record(
        self,
        item_id: str,
        representation_id: str,
    ) -> JsonMapping | None: ...


class CanvasQueryService:
    """List and inspect canvases without exposing their backing resources."""

    def __init__(self, repository: CanvasQueryRepositoryPort) -> None:
        self._repository = repository

    def list(
        self,
        item_id: str,
        representation_id: str,
    ) -> CanvasSequenceView:
        item = _identifier(item_id, "item_id")
        representation = _identifier(representation_id, "representation_id")
        return self._load_sequence(item, representation)

    def get(self, key: CanvasKey) -> CanvasView:
        if not isinstance(key, CanvasKey):
            raise ValidationError(
                "key must be a CanvasKey",
                code="invalid_canvas_identity",
                details={"field": "key"},
            )
        sequence = self._load_sequence(
            key.item_id,
            key.representation_id,
        )
        canvas = next(
            (value for value in sequence.canvases if value.key == key),
            None,
        )
        if canvas is None:
            raise NotFoundError(
                "the canvas does not exist",
                code="canvas_not_found",
                details=key.as_dict(),
            )
        return canvas

    def _load_sequence(
        self,
        item_id: str,
        representation_id: str,
    ) -> CanvasSequenceView:
        try:
            record = self._repository.get_sequence_record(
                item_id,
                representation_id,
            )
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the canvas repository is unavailable",
                code="canvas_repository_unavailable",
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc
        if record is None:
            raise CanvasSequenceUnavailableError(
                "the representation has no queryable canvas sequence",
                details={
                    "item_id": item_id,
                    "representation_id": representation_id,
                },
            )
        return self._sequence_view(
            record,
            expected_item_id=item_id,
            expected_representation_id=representation_id,
        )

    @staticmethod
    def _sequence_view(
        record: Any,
        *,
        expected_item_id: str,
        expected_representation_id: str,
    ) -> CanvasSequenceView:
        scope = {
            "item_id": expected_item_id,
            "representation_id": expected_representation_id,
        }
        if not isinstance(record, Mapping):
            raise RepositoryError(
                "the canvas repository returned an invalid sequence",
                code="invalid_canvas_snapshot",
                details=scope,
            )
        actual_item_id = record.get("item_id")
        actual_representation_id = record.get("representation_id")
        if (
            actual_item_id != expected_item_id
            or actual_representation_id != expected_representation_id
        ):
            raise RepositoryError(
                "the canvas repository returned a sequence outside its scope",
                code="canvas_repository_scope_mismatch",
                details={
                    **scope,
                    "actual_item_id": (
                        actual_item_id if isinstance(actual_item_id, str) else ""
                    ),
                    "actual_representation_id": (
                        actual_representation_id
                        if isinstance(actual_representation_id, str)
                        else ""
                    ),
                },
            )
        raw_canvases = record.get("canvases")
        if isinstance(raw_canvases, (str, bytes)) or not isinstance(
            raw_canvases, Sequence
        ):
            raise RepositoryError(
                "the canvas repository returned invalid canvases",
                code="invalid_canvas_snapshot",
                details={**scope, "section": "canvases"},
            )
        try:
            representation_revision = _revision(
                record.get("representation_revision"),
                "representation_revision",
            )
            canvases = tuple(
                CanvasQueryService._canvas_view(
                    raw,
                    item_id=expected_item_id,
                    representation_id=expected_representation_id,
                )
                for raw in raw_canvases
            )
            ordered = tuple(sorted(canvases, key=lambda canvas: canvas.order))
            sequence_revision = _derived_revision(
                "cs",
                {
                    **scope,
                    "representation_revision": representation_revision,
                    "canvases": [canvas.as_dict() for canvas in ordered],
                },
            )
            return CanvasSequenceView(
                item_id=expected_item_id,
                representation_id=expected_representation_id,
                representation_revision=representation_revision,
                revision=sequence_revision,
                canvases=ordered,
            )
        except ValidationError as exc:
            code = (
                exc.code
                if exc.code
                in {"duplicate_canvas_identity", "duplicate_canvas_order"}
                else "invalid_canvas_snapshot"
            )
            details = dict(scope)
            if "field" in exc.details:
                details["field"] = exc.details["field"]
            if code == "duplicate_canvas_identity":
                details["canvas_ids"] = list(exc.details.get("canvas_ids") or ())
            elif code == "duplicate_canvas_order":
                details["orders"] = list(exc.details.get("orders") or ())
            raise RepositoryError(
                "the canvas repository returned an invalid sequence",
                code=code,
                details=details,
            ) from exc

    @staticmethod
    def _canvas_view(
        raw: Any,
        *,
        item_id: str,
        representation_id: str,
    ) -> CanvasView:
        if not isinstance(raw, Mapping):
            raise ValidationError(
                "a canvas record must be an object",
                code="invalid_canvas_snapshot",
                details={"field": "canvases"},
            )
        canvas_id = _identifier(raw.get("canvas_id"), "canvas_id")
        order = _non_negative_int(raw.get("order"), "order")
        label = _text(raw.get("label", ""), "label", maximum=512)
        available = raw.get("available")
        if not isinstance(available, bool):
            raise ValidationError(
                "canvas availability must be a boolean",
                code="invalid_canvas_availability",
                details={"field": "available"},
            )
        raw_extent = raw.get("extent", {})
        if not isinstance(raw_extent, Mapping):
            raise ValidationError(
                "canvas extent must be an object",
                code="invalid_canvas_extent",
                details={"field": "extent"},
            )
        extent = CanvasExtent(
            width=raw_extent.get("width"),
            height=raw_extent.get("height"),
            unit=raw_extent.get("unit", ""),
            duration=raw_extent.get("duration"),
        )
        resource_kinds = _resource_kinds(raw.get("resource_kinds", ()))
        metadata = _metadata(raw.get("metadata", {}))
        source_revision = raw.get("revision", "")
        if source_revision:
            source_revision = _revision(source_revision, "canvas_source_revision")
        elif not isinstance(source_revision, str):
            raise ValidationError(
                "canvas source revision must be a string",
                code="invalid_canvas_revision",
                details={"field": "revision"},
            )
        key = CanvasKey(item_id, representation_id, canvas_id)
        public_state = {
            "key": key.as_dict(),
            "source_revision": source_revision,
            "order": order,
            "label": label,
            "extent": extent.as_dict(),
            "available": available,
            "resource_kinds": list(resource_kinds),
            "metadata": _thaw(metadata),
        }
        return CanvasView(
            key=key,
            revision=_derived_revision("cv", public_state),
            order=order,
            label=label,
            extent=extent,
            available=available,
            resource_kinds=resource_kinds,
            metadata=metadata,
        )


__all__ = [
    "CanvasExtent",
    "CanvasKey",
    "CanvasQueryRepositoryPort",
    "CanvasQueryService",
    "CanvasSequenceUnavailableError",
    "CanvasSequenceView",
    "CanvasView",
]
