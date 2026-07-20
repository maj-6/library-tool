"""Flask transport for the optional revisioned text-layer aggregate.

This module intentionally lives outside :mod:`librarytool`, whose complete
source tree is framework-neutral and safe for non-Flask hosts to package.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
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
from librarytool.engine.runtime import (
    TEXT_LAYER_AGGREGATE_SERVICE,
    LibraryEngine,
)
from librarytool.engine.text_layer_aggregate import (
    ReplaceTextLayerUnitCommand,
    TextLayerAggregateService,
    TextLayerProvenance,
    TextLayerUnitReplacement,
)


TEXT_LAYER_MUTATION_MAX_BYTES = 1024 * 1024
TEXT_LAYER_DETAIL_MAX_BYTES = 16 * 1024 * 1024


def _error_status(error: EngineError) -> int:
    if error.code in {
        "text_layer_mutation_too_large",
        "text_layer_detail_too_large",
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


def _service(engine_for_request: Callable[[], LibraryEngine]) -> (
    TextLayerAggregateService
):
    engine = engine_for_request()
    service = engine.get_service(TEXT_LAYER_AGGREGATE_SERVICE)
    if service is None:
        raise EngineError(
            "the text layer aggregate module is unavailable",
            code="text_layer_module_unavailable",
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
    return "tlc-" + hashlib.sha256(encoded).hexdigest()


def _conditional_json(body: Mapping[str, Any], revision: str) -> Response:
    response = jsonify(dict(body))
    response.set_etag(revision, weak=False)
    response.cache_control.no_cache = True
    return response.make_conditional(request)


def _detail_json(
    body: Mapping[str, Any],
    *,
    revision: str,
) -> Response:
    """Serialize one detail exactly once and reject, rather than truncate."""

    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    )
    payload = bytearray()
    encoded_size = 0
    for chunk in encoder.iterencode(dict(body)):
        encoded = chunk.encode("utf-8")
        encoded_size += len(encoded)
        if encoded_size > TEXT_LAYER_DETAIL_MAX_BYTES:
            raise EngineError(
                "the text layer detail exceeds this transport's response limit",
                code="text_layer_detail_too_large",
                details={"maximum_bytes": TEXT_LAYER_DETAIL_MAX_BYTES},
            )
        payload.extend(encoded)
    response = Response(bytes(payload), mimetype="application/json")
    response.set_etag(revision, weak=False)
    response.cache_control.no_cache = True
    return response.make_conditional(request)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _mutation_document() -> Mapping[str, Any]:
    length = request.content_length
    if length is not None and length > TEXT_LAYER_MUTATION_MAX_BYTES:
        raise ValidationError(
            "the text layer mutation document is too large",
            code="text_layer_mutation_too_large",
            details={"maximum_bytes": TEXT_LAYER_MUTATION_MAX_BYTES},
        )
    if request.mimetype != "application/json":
        raise ValidationError(
            "the text layer mutation must use application/json",
            code="invalid_text_layer_mutation_document",
            details={"content_type": str(request.content_type or "")},
        )
    charset = request.mimetype_params.get("charset", "utf-8").casefold()
    if charset not in {"utf-8", "utf8"} or request.content_encoding:
        raise ValidationError(
            "the text layer mutation must use unencoded UTF-8 JSON",
            code="invalid_text_layer_mutation_document",
            details={"content_type": str(request.content_type or "")},
        )
    encoded = request.stream.read(TEXT_LAYER_MUTATION_MAX_BYTES + 1)
    if len(encoded) > TEXT_LAYER_MUTATION_MAX_BYTES:
        raise ValidationError(
            "the text layer mutation document is too large",
            code="text_layer_mutation_too_large",
            details={"maximum_bytes": TEXT_LAYER_MUTATION_MAX_BYTES},
        )
    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _token: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise ValidationError(
            "the text layer mutation document is invalid JSON",
            code="invalid_text_layer_mutation_document",
            details={"cause_type": type(error).__name__},
        ) from error
    if not isinstance(payload, Mapping) or set(payload) != {"replacement"}:
        raise ValidationError(
            "the mutation must contain exactly one replacement",
            code="invalid_text_layer_mutation_envelope",
            details={"field": "replacement"},
        )
    replacement = payload["replacement"]
    if not isinstance(replacement, Mapping) or set(replacement) != {
        "text",
        "provenance",
    }:
        raise ValidationError(
            "the replacement fields do not match the schema",
            code="invalid_text_layer_unit_replacement",
        )
    return replacement


def _operation_id(*, item_id: str, layer_id: str, selector: str) -> str:
    value = request.headers.get("Idempotency-Key")
    if value is None or value == "":
        raise PreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details={
                "header": "Idempotency-Key",
                "item_id": item_id,
                "layer_id": layer_id,
                "selector": selector,
            },
        )
    return value


def _valid_revision(value: str) -> bool:
    return (
        0 < len(value) <= 512
        and value == value.strip()
        and '"' not in value
        and "\\" not in value
        and all(
            not character.isspace()
            and unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
            for character in value
        )
    )


def _strong_revision(
    header: str,
    *,
    required_code: str,
    invalid_code: str,
    item_id: str,
    layer_id: str,
    selector: str,
) -> str:
    raw = request.headers.get(header)
    details = {
        "header": header,
        "item_id": item_id,
        "layer_id": layer_id,
        "selector": selector,
    }
    if raw is None or raw == "":
        raise PreconditionRequiredError(
            f"{header} is required",
            code=required_code,
            details=details,
        )
    token = raw[1:-1] if len(raw) >= 2 else ""
    if (
        raw != raw.strip()
        or raw.startswith("W/")
        or len(raw) < 3
        or raw[0] != '"'
        or raw[-1] != '"'
        or not _valid_revision(token)
    ):
        raise ValidationError(
            f"{header} must contain one strong quoted revision",
            code=invalid_code,
            details=details,
        )
    return token


def _unit_replacement(
    selector: str,
    document: Mapping[str, Any],
    *,
    item_id: str,
    layer_id: str,
) -> TextLayerUnitReplacement:
    try:
        return TextLayerUnitReplacement(
            selector=selector,
            text=document["text"],
            provenance=TextLayerProvenance.from_dict(document["provenance"]),
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise ValidationError(
            "the text layer unit replacement is invalid",
            code="invalid_text_layer_unit_replacement",
            details={
                "item_id": item_id,
                "layer_id": layer_id,
                "selector": selector,
                "cause_type": type(error).__name__,
            },
        ) from error


def create_text_layer_blueprint(
    engine_for_request: Callable[[], LibraryEngine],
) -> Blueprint:
    """Create a transport adapter without claiming or composing an engine."""

    if not callable(engine_for_request):
        raise TypeError("engine_for_request must be callable")
    blueprint = Blueprint("librarytool_text_layers_v1", __name__)

    @blueprint.get("/api/v1/items/<item_id>/text-layers")
    def list_text_layers(item_id: str):
        try:
            rows = [value.as_dict() for value in _service(engine_for_request).list(
                item_id
            )]
        except EngineError as error:
            return _error_response(error)
        revision = _collection_revision(rows)
        return _conditional_json(
            {
                "ok": True,
                "schema": "librarytool.text-layer-summaries/1",
                "item_id": item_id,
                "text_layers": rows,
                "revision": revision,
            },
            revision,
        )

    @blueprint.get(
        "/api/v1/items/<item_id>/text-layers/<layer_id>"
    )
    def get_text_layer(item_id: str, layer_id: str):
        try:
            view = _service(engine_for_request).get(item_id, layer_id)
            response = _detail_json(
                {
                    "ok": True,
                    "schema": "librarytool.text-layer/1",
                    "text_layer": view.as_dict(),
                },
                revision=view.view_revision,
            )
        except EngineError as error:
            return _error_response(error)
        response.headers["X-Document-Revision"] = (
            view.document.document_revision
        )
        response.headers["X-Content-Revision"] = view.document.content_revision
        response.headers["X-Source-Revision"] = view.source.pinned_revision
        return response

    @blueprint.put(
        "/api/v1/items/<item_id>/text-layers/<layer_id>/units/<selector>"
    )
    def replace_text_layer_unit(item_id: str, layer_id: str, selector: str):
        try:
            service = _service(engine_for_request)
            operation_id = _operation_id(
                item_id=item_id,
                layer_id=layer_id,
                selector=selector,
            )
            unit_revision = _strong_revision(
                "If-Unit-Match",
                required_code="text_layer_unit_revision_required",
                invalid_code="invalid_text_layer_unit_revision",
                item_id=item_id,
                layer_id=layer_id,
                selector=selector,
            )
            source_revision = _strong_revision(
                "If-Source-Match",
                required_code="text_layer_source_revision_required",
                invalid_code="invalid_text_layer_source_revision",
                item_id=item_id,
                layer_id=layer_id,
                selector=selector,
            )
            document = _mutation_document()
            replacement = _unit_replacement(
                selector,
                document,
                item_id=item_id,
                layer_id=layer_id,
            )
            result = service.replace_unit(
                ReplaceTextLayerUnitCommand(
                    item_id=item_id,
                    layer_id=layer_id,
                    replacement=replacement,
                    expected_unit_revision=unit_revision,
                    expected_source_revision=source_revision,
                    operation_id=operation_id,
                )
            )
        except EngineError as error:
            return _error_response(error)
        response = jsonify(
            {
                "ok": True,
                "schema": "librarytool.text-layer-mutation-receipt/1",
                **result.as_dict(),
            }
        )
        response.cache_control.no_store = True
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Document-Revision"] = (
            result.receipt.after_document_revision
        )
        response.headers["X-Content-Revision"] = (
            result.receipt.after_content_revision
        )
        response.headers["X-Source-Revision"] = result.receipt.source_revision
        return response

    return blueprint


__all__ = [
    "TEXT_LAYER_DETAIL_MAX_BYTES",
    "TEXT_LAYER_MUTATION_MAX_BYTES",
    "create_text_layer_blueprint",
]
