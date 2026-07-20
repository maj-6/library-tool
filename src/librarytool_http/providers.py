"""Flask transport for optional, read-only provider discovery."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, Response, jsonify, request

from librarytool.engine.errors import EngineError
from librarytool.engine.providers import ProviderDiscoveryService
from librarytool.engine.runtime import (
    PROVIDER_DISCOVERY_SERVICE,
    LibraryEngine,
)


def _service(
    engine_for_request: Callable[[], LibraryEngine],
) -> ProviderDiscoveryService:
    service = engine_for_request().get_service(PROVIDER_DISCOVERY_SERVICE)
    if service is None:
        raise EngineError(
            "provider discovery is unavailable",
            code="provider_discovery_unavailable",
            retryable=False,
        )
    return service


def _error_response(error: EngineError) -> tuple[Response, int]:
    response = jsonify({
        "ok": False,
        "error": error.message,
        "code": error.code,
        "retryable": error.retryable,
    })
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    return response, 503 if error.code == "provider_discovery_unavailable" else 500


def _conditional_json(document: Mapping[str, Any]) -> Response:
    body = {"ok": True, **dict(document)}
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    revision = "providers-" + hashlib.sha256(encoded).hexdigest()
    response = jsonify(body)
    response.set_etag(revision, weak=False)
    response.cache_control.no_cache = True
    return response.make_conditional(request)


def create_provider_discovery_blueprint(
    engine_for_request: Callable[[], LibraryEngine],
) -> Blueprint:
    """Create a transport adapter without composing providers or an engine."""

    if not callable(engine_for_request):
        raise TypeError("engine_for_request must be callable")
    blueprint = Blueprint("librarytool_providers_v1", __name__)

    @blueprint.get("/api/v1/providers")
    def discover_providers():
        try:
            document = _service(engine_for_request).discovery_document()
        except EngineError as error:
            return _error_response(error)
        return _conditional_json(document)

    return blueprint


__all__ = ["create_provider_discovery_blueprint"]
