"""Flask transport for named per-workspace processing presets.

This module intentionally lives outside :mod:`librarytool`, whose complete
source tree is framework-neutral and safe for non-Flask hosts to package.

Presets are workspace-scoped rather than item-scoped, so the routes carry no
item identifier.  Concurrency uses the same shape as the other revisioned
resources here: a strong ``If-Preset-Match`` on writes that replace or remove an
existing record, and a collection ETag on the list read.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, Response, jsonify, request

from librarytool.engine.errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.processing_presets import (
    ProcessingPreset,
    ProcessingPresetService,
)
from librarytool.engine.runtime import (
    PROCESSING_PRESET_SERVICE,
    LibraryEngine,
)


PROCESSING_PRESET_MUTATION_MAX_BYTES = 256 * 1024
_PRESET_MATCH_HEADER = "If-Preset-Match"
_MUTATION_FIELDS = frozenset({"preset"})


def _error_status(error: EngineError) -> int:
    if error.code in {
        "processing_preset_mutation_too_large",
        "processing_preset_document_too_large",
    }:
        return 413
    if isinstance(error, NotFoundError):
        return 404
    if isinstance(error, PreconditionRequiredError):
        return 428
    if isinstance(error, ConflictError):
        return 409
    if isinstance(error, ValidationError):
        return 400
    if isinstance(error, RepositoryError) and error.retryable:
        return 503
    if error.retryable and error.code.endswith("_unavailable"):
        return 503
    return 500


def _error_response(error: EngineError) -> tuple[Response, int]:
    body: dict[str, Any] = {
        "ok": False,
        "error": error.message,
        "code": error.code,
        "retryable": error.retryable,
    }
    if error.details:
        body["details"] = dict(error.details)
    if isinstance(error, (ConflictError, PreconditionRequiredError)):
        body["conflict"] = error.code
    response = jsonify(body)
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    return response, _error_status(error)


def _service(
    engine_for_request: Callable[[], LibraryEngine],
) -> ProcessingPresetService:
    engine = engine_for_request()
    service = engine.get_service(PROCESSING_PRESET_SERVICE)
    if service is None:
        raise EngineError(
            "the processing preset module is unavailable",
            code="processing_preset_module_unavailable",
            retryable=True,
        )
    return service


def _collection_revision(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        rows,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "ppc-" + hashlib.sha256(encoded).hexdigest()


def _conditional_json(body: Mapping[str, Any], revision: str) -> Response:
    response = jsonify(dict(body))
    response.set_etag(revision, weak=False)
    response.cache_control.no_cache = True
    return response.make_conditional(request)


def _mutation_response(preset: ProcessingPreset) -> tuple[Response, int]:
    response = jsonify(
        {
            "ok": True,
            "schema": "librarytool.processing-preset/1",
            "preset": preset.as_view(),
        }
    )
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Preset-Revision"] = preset.revision
    return response, 200


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _mutation_document() -> Mapping[str, Any]:
    length = request.content_length
    if length is not None and length > PROCESSING_PRESET_MUTATION_MAX_BYTES:
        raise ValidationError(
            "the processing preset document is too large",
            code="processing_preset_mutation_too_large",
            details={"maximum_bytes": PROCESSING_PRESET_MUTATION_MAX_BYTES},
        )
    if request.mimetype != "application/json":
        raise ValidationError(
            "the processing preset mutation must use application/json",
            code="invalid_processing_preset_document",
            details={"content_type": str(request.content_type or "")},
        )
    charset = request.mimetype_params.get("charset", "utf-8").casefold()
    if charset not in {"utf-8", "utf8"} or request.content_encoding:
        raise ValidationError(
            "the processing preset mutation must use unencoded UTF-8 JSON",
            code="invalid_processing_preset_document",
            details={"content_type": str(request.content_type or "")},
        )
    encoded = request.stream.read(PROCESSING_PRESET_MUTATION_MAX_BYTES + 1)
    if len(encoded) > PROCESSING_PRESET_MUTATION_MAX_BYTES:
        raise ValidationError(
            "the processing preset document is too large",
            code="processing_preset_mutation_too_large",
            details={"maximum_bytes": PROCESSING_PRESET_MUTATION_MAX_BYTES},
        )
    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _token: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValidationError(
            "the processing preset document is invalid JSON",
            code="invalid_processing_preset_document",
            details={"cause_type": type(error).__name__},
        ) from error
    if not isinstance(value, Mapping) or set(value) != _MUTATION_FIELDS:
        raise ValidationError(
            "the processing preset envelope fields do not match the schema",
            code="invalid_processing_preset_envelope",
            details={"fields": sorted(_MUTATION_FIELDS)},
        )
    return value


def _strong_revision() -> str:
    raw = request.headers.get(_PRESET_MATCH_HEADER)
    if raw is None or raw == "":
        raise PreconditionRequiredError(
            f"{_PRESET_MATCH_HEADER} is required",
            code="processing_preset_revision_required",
            details={"header": _PRESET_MATCH_HEADER},
        )
    if (
        raw != raw.strip()
        or raw.startswith("W/")
        or len(raw) < 3
        or raw[0] != '"'
        or raw[-1] != '"'
        or '"' in raw[1:-1]
    ):
        raise ValidationError(
            f"{_PRESET_MATCH_HEADER} must contain one strong quoted revision",
            code="invalid_processing_preset_revision",
            details={"header": _PRESET_MATCH_HEADER},
        )
    return raw[1:-1]


def _preset_from_body(preset_id: str) -> ProcessingPreset:
    document = _mutation_document()
    preset = ProcessingPreset.from_dict(document["preset"])
    if preset.preset_id != preset_id:
        raise ValidationError(
            "the processing preset envelope does not match its document",
            code="processing_preset_envelope_mismatch",
            details={"mismatched": ["preset_id"]},
        )
    return preset


def create_processing_preset_blueprint(
    engine_for_request: Callable[[], LibraryEngine],
) -> Blueprint:
    """Create a transport adapter without claiming or composing an engine."""

    if not callable(engine_for_request):
        raise TypeError("engine_for_request must be callable")
    blueprint = Blueprint("librarytool_processing_presets_v1", __name__)

    @blueprint.get("/api/v1/processing-presets")
    def list_processing_presets():
        try:
            rows = [
                value.as_view()
                for value in _service(engine_for_request).list_presets()
            ]
        except EngineError as error:
            return _error_response(error)
        revision = _collection_revision(rows)
        return _conditional_json(
            {
                "ok": True,
                "schema": "librarytool.processing-presets/1",
                "presets": rows,
                "revision": revision,
            },
            revision,
        )

    @blueprint.put("/api/v1/processing-presets/<preset_id>")
    def create_processing_preset(preset_id: str):
        try:
            return _mutation_response(
                _service(engine_for_request).create_preset(
                    _preset_from_body(preset_id)
                )
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.post("/api/v1/processing-presets/<preset_id>")
    def update_processing_preset(preset_id: str):
        try:
            expected = _strong_revision()
            return _mutation_response(
                _service(engine_for_request).update_preset(
                    _preset_from_body(preset_id),
                    expected,
                )
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.delete("/api/v1/processing-presets/<preset_id>")
    def delete_processing_preset(preset_id: str):
        try:
            expected = _strong_revision()
            _service(engine_for_request).delete_preset(preset_id, expected)
        except EngineError as error:
            return _error_response(error)
        response = jsonify(
            {
                "ok": True,
                "schema": "librarytool.processing-preset-deleted/1",
                "preset_id": preset_id,
            }
        )
        response.cache_control.no_store = True
        response.headers["Pragma"] = "no-cache"
        return response, 200

    return blueprint


__all__ = [
    "PROCESSING_PRESET_MUTATION_MAX_BYTES",
    "create_processing_preset_blueprint",
]
