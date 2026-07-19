"""Strict JSON value handling shared by the Knowledge engine slice."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ..errors import ValidationError


EMPTY_JSON_OBJECT: Mapping[str, Any] = MappingProxyType({})


def freeze_json(
    value: Any,
    *,
    path: str = "$",
    active: set[int] | None = None,
) -> Any:
    """Detach and recursively freeze one strict JSON value.

    Knowledge contracts cross process and provider boundaries, so accepting
    arbitrary Python objects here would make revisions transport-dependent.
    Booleans remain distinct from integers and non-finite floats are refused.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        raise ValidationError(
            "knowledge data contains a non-finite number",
            code="invalid_knowledge_json",
            details={"path": path},
        )

    if active is None:
        active = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise ValidationError(
                "knowledge data contains a reference cycle",
                code="invalid_knowledge_json",
                details={"path": path},
            )
        active.add(identity)
        try:
            frozen: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValidationError(
                        "knowledge object keys must be strings",
                        code="invalid_knowledge_json",
                        details={
                            "path": path,
                            "key_type": type(key).__name__,
                        },
                    )
                frozen[key] = freeze_json(
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
            raise ValidationError(
                "knowledge data contains a reference cycle",
                code="invalid_knowledge_json",
                details={"path": path},
            )
        active.add(identity)
        try:
            return tuple(
                freeze_json(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    raise ValidationError(
        "knowledge data contains a non-JSON value",
        code="invalid_knowledge_json",
        details={"path": path, "value_type": type(value).__name__},
    )


def freeze_object(value: Mapping[str, Any], *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(
            "knowledge metadata must be an object",
            code="invalid_knowledge_json",
            details={"path": path, "value_type": type(value).__name__},
        )
    return freeze_json(value, path=path)


def thaw_json(value: Any) -> Any:
    """Return a detached JSON-serializable copy of a frozen value."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def derived_revision(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def require_string(value: Any, field_name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        suffix = "a string" if empty else "a non-empty string"
        raise ValidationError(
            f"{field_name} must be {suffix}",
            code="invalid_knowledge_contract",
            details={"field": field_name},
        )
    return value


def require_non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValidationError(
            f"{field_name} must be a non-negative integer",
            code="invalid_knowledge_contract",
            details={"field": field_name},
        )
    return value


def require_positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError(
            f"{field_name} must be a positive integer",
            code="invalid_knowledge_contract",
            details={"field": field_name},
        )
    return value


__all__ = [
    "EMPTY_JSON_OBJECT",
    "canonical_json",
    "derived_revision",
    "freeze_json",
    "freeze_object",
    "require_non_negative_int",
    "require_positive_int",
    "require_string",
    "thaw_json",
]
