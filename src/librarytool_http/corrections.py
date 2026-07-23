"""Versioned Flask transport for Corrections artifact read models.

The engine owns portable artifact and annotation views.  This transport owns
HTTP concerns only: bounded paging, strong conditional responses, and exchange
of an opaque raster resource reference for bytes.  Private filesystem paths
never enter a JSON response.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from flask import Blueprint, Response, jsonify, request, send_file

from librarytool.engine.errors import (
    ConflictError,
    EngineError,
    NotFoundError,
    PreconditionRequiredError,
    RepositoryError,
    ValidationError,
)
from librarytool.engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterResourceRef,
    ResourceState,
)
from librarytool.engine.runtime import (
    RASTER_ARTIFACT_QUERY_SERVICE,
    SPATIAL_ANNOTATION_QUERY_SERVICE,
    LibraryEngine,
)
from librarytool.engine.spatial_annotations import (
    SpatialAnnotationKey,
    SpatialAnnotationProjectorPort,
    SpatialAnnotationView,
)


ARTIFACT_PAGE_LIMIT = 512
_CURSOR_SCHEMA = "librarytool.corrections-cursor/1"


def _error_status(error: EngineError) -> int:
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


def _query_service(
    engine_for_request: Callable[[], LibraryEngine],
    key: Any,
    label: str,
) -> Any:
    service = engine_for_request().get_service(key)
    if service is None:
        raise EngineError(
            f"the {label} module is unavailable",
            code=f"{label.replace(' ', '_')}_module_unavailable",
            retryable=True,
        )
    return service


def _raster_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> RasterArtifactProjectorPort:
    return _query_service(
        engine_for_request,
        RASTER_ARTIFACT_QUERY_SERVICE,
        "raster artifact",
    )


def _spatial_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> SpatialAnnotationProjectorPort:
    return _query_service(
        engine_for_request,
        SPATIAL_ANNOTATION_QUERY_SERVICE,
        "spatial annotation",
    )


def _collection_revision(prefix: str, rows: Sequence[Mapping[str, Any]]) -> str:
    encoded = json.dumps(
        list(rows),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()


def _conditional_json(body: Mapping[str, Any], revision: str) -> Response:
    response = jsonify(dict(body))
    response.set_etag(revision, weak=False)
    response.cache_control.no_cache = True
    return response.make_conditional(request)


def _limit() -> int:
    raw = request.args.get("limit", "100")
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            "limit must be an integer",
            code="invalid_corrections_page_limit",
            details={"field": "limit"},
        ) from error
    if value < 1 or value > ARTIFACT_PAGE_LIMIT:
        raise ValidationError(
            f"limit must be between 1 and {ARTIFACT_PAGE_LIMIT}",
            code="invalid_corrections_page_limit",
            details={"field": "limit", "maximum": ARTIFACT_PAGE_LIMIT},
        )
    return value


def _encode_cursor(revision: str, offset: int) -> str:
    payload = json.dumps(
        {"schema": _CURSOR_SCHEMA, "revision": revision, "offset": offset},
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_offset(revision: str, total: int) -> int:
    token = request.args.get("cursor", "")
    if not token:
        return 0
    if len(token) > 2048:
        raise ValidationError(
            "cursor is invalid",
            code="invalid_corrections_cursor",
            details={"field": "cursor"},
        )
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(decoded.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValidationError(
            "cursor is invalid",
            code="invalid_corrections_cursor",
            details={"field": "cursor"},
        ) from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "revision", "offset"}
        or value.get("schema") != _CURSOR_SCHEMA
        or isinstance(value.get("offset"), bool)
        or not isinstance(value.get("offset"), int)
        or value["offset"] < 0
        or value["offset"] > total
        or not isinstance(value.get("revision"), str)
    ):
        raise ValidationError(
            "cursor is invalid",
            code="invalid_corrections_cursor",
            details={"field": "cursor"},
        )
    if value["revision"] != revision:
        raise ConflictError(
            "the artifact collection changed while it was being paged",
            code="corrections_collection_changed",
            details={
                "expected_revision": value["revision"],
                "actual_revision": revision,
            },
        )
    return value["offset"]


def _page(
    rows: Sequence[Mapping[str, Any]],
    *,
    revision: str,
) -> tuple[list[Mapping[str, Any]], str | None]:
    limit = _limit()
    offset = _cursor_offset(revision, len(rows))
    values = list(rows[offset : offset + limit])
    next_offset = offset + len(values)
    cursor = (
        _encode_cursor(revision, next_offset)
        if next_offset < len(rows)
        else None
    )
    return values, cursor


def _validated_rasters(
    values: Sequence[RasterArtifactView],
    *,
    item_id: str,
) -> list[RasterArtifactView]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RepositoryError(
            "the raster artifact projector returned an invalid collection",
            code="invalid_raster_artifact_projection",
            details={"item_id": item_id},
        )
    rows = list(values)
    if any(
        not isinstance(value, RasterArtifactView)
        or value.key.item_id != item_id
        for value in rows
    ):
        raise RepositoryError(
            "the raster artifact projector returned an invalid view",
            code="invalid_raster_artifact_projection",
            details={"item_id": item_id},
        )
    identities = [value.key.artifact_id.casefold() for value in rows]
    if len(identities) != len(set(identities)):
        raise RepositoryError(
            "the raster artifact projector returned duplicate identities",
            code="invalid_raster_artifact_projection",
            details={"item_id": item_id},
        )
    return sorted(rows, key=lambda value: value.key.artifact_id)


def _validated_annotations(
    values: Sequence[SpatialAnnotationView],
    *,
    item_id: str,
) -> list[SpatialAnnotationView]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RepositoryError(
            "the spatial annotation projector returned an invalid collection",
            code="invalid_spatial_annotation_projection",
            details={"item_id": item_id},
        )
    rows = list(values)
    if any(
        not isinstance(value, SpatialAnnotationView)
        or value.key.item_id != item_id
        for value in rows
    ):
        raise RepositoryError(
            "the spatial annotation projector returned an invalid view",
            code="invalid_spatial_annotation_projection",
            details={"item_id": item_id},
        )
    identities = [value.key.annotation_id.casefold() for value in rows]
    if len(identities) != len(set(identities)):
        raise RepositoryError(
            "the spatial annotation projector returned duplicate identities",
            code="invalid_spatial_annotation_projection",
            details={"item_id": item_id},
        )
    return sorted(rows, key=lambda value: value.key.annotation_id)


def _raster_list(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
) -> Response:
    service = _raster_service(engine_for_request)
    rows = _validated_rasters(
        service.list_raster_artifacts(item_id),
        item_id=item_id,
    )
    representation_id = request.args.get("representation_id", "")
    canvas_id = request.args.get("canvas_id", "")
    if representation_id:
        rows = [
            value
            for value in rows
            if value.source.representation_id == representation_id
        ]
    if canvas_id:
        rows = [value for value in rows if value.source.canvas_id == canvas_id]
    serialized = [value.as_dict() for value in rows]
    revision = _collection_revision("rac-", serialized)
    page, next_cursor = _page(serialized, revision=revision)
    return _conditional_json(
        {
            "ok": True,
            "schema": "librarytool.raster-artifacts/1",
            "item_id": item_id,
            "revision": revision,
            "artifacts": page,
            "next_cursor": next_cursor,
            "total": len(serialized),
        },
        revision,
    )


def _raster_detail(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    artifact_id: str,
) -> Response:
    key = RasterArtifactKey(item_id, artifact_id)
    value = _raster_service(engine_for_request).get_raster_artifact(key)
    if value is None:
        raise NotFoundError(
            "the raster artifact does not exist",
            code="raster_artifact_not_found",
            details=key.as_dict(),
        )
    if not isinstance(value, RasterArtifactView) or value.key != key:
        raise RepositoryError(
            "the raster artifact projector returned an invalid view",
            code="invalid_raster_artifact_projection",
            details=key.as_dict(),
        )
    return _conditional_json(
        {
            "ok": True,
            "schema": "librarytool.raster-artifact/1",
            "artifact": value.as_dict(),
        },
        value.revision,
    )


def _spatial_list(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
) -> Response:
    representation_id = request.args.get("representation_id", "")
    canvas_id = request.args.get("canvas_id", "")
    values = _spatial_service(engine_for_request).list_spatial_annotations(
        item_id,
        representation_id=representation_id,
        canvas_id=canvas_id,
    )
    rows = _validated_annotations(values, item_id=item_id)
    serialized = [value.as_dict() for value in rows]
    revision = _collection_revision("sac-", serialized)
    page, next_cursor = _page(serialized, revision=revision)
    return _conditional_json(
        {
            "ok": True,
            "schema": "librarytool.spatial-annotations/1",
            "item_id": item_id,
            "revision": revision,
            "annotations": page,
            "next_cursor": next_cursor,
            "total": len(serialized),
        },
        revision,
    )


def _spatial_detail(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    annotation_id: str,
) -> Response:
    key = SpatialAnnotationKey(item_id, annotation_id)
    value = _spatial_service(engine_for_request).get_spatial_annotation(key)
    if value is None:
        raise NotFoundError(
            "the spatial annotation does not exist",
            code="spatial_annotation_not_found",
            details=key.as_dict(),
        )
    if not isinstance(value, SpatialAnnotationView) or value.key != key:
        raise RepositoryError(
            "the spatial annotation projector returned an invalid view",
            code="invalid_spatial_annotation_projection",
            details=key.as_dict(),
        )
    return _conditional_json(
        {
            "ok": True,
            "schema": "librarytool.spatial-annotation/1",
            "annotation": value.as_dict(),
        },
        value.revision,
    )


def _resource_response(
    engine_for_request: Callable[[], LibraryEngine],
    resolver_for_request: Callable[[], Any] | None,
    item_id: str,
    artifact_id: str,
) -> Response:
    key = RasterArtifactKey(item_id, artifact_id)
    artifact = _raster_service(engine_for_request).get_raster_artifact(key)
    if artifact is None:
        raise NotFoundError(
            "the raster artifact does not exist",
            code="raster_artifact_not_found",
            details=key.as_dict(),
        )
    if not isinstance(artifact, RasterArtifactView) or artifact.key != key:
        raise RepositoryError(
            "the raster artifact projector returned an invalid view",
            code="invalid_raster_artifact_projection",
            details=key.as_dict(),
        )
    if (
        artifact.resource_state is not ResourceState.AVAILABLE
        or not isinstance(artifact.resource, RasterResourceRef)
    ):
        raise NotFoundError(
            "the raster artifact has no available resource",
            code="raster_resource_not_found",
            details=key.as_dict(),
        )
    requested_revision = request.args.get("revision", "")
    if not requested_revision:
        raise PreconditionRequiredError(
            "a raster resource revision is required",
            code="raster_resource_revision_required",
            details={"query": "revision", **key.as_dict()},
        )
    if requested_revision != artifact.resource.revision:
        raise ConflictError(
            "the raster resource revision changed",
            code="raster_resource_revision_conflict",
            details={
                **key.as_dict(),
                "expected_revision": requested_revision,
                "actual_revision": artifact.resource.revision,
            },
        )
    if resolver_for_request is None:
        raise EngineError(
            "the raster resource resolver is unavailable",
            code="raster_resource_resolver_unavailable",
            retryable=True,
        )
    resolver = resolver_for_request()
    resolve = getattr(resolver, "resolve_raster_resource", None)
    if not callable(resolve):
        raise EngineError(
            "the raster resource resolver is unavailable",
            code="raster_resource_resolver_unavailable",
            retryable=True,
        )
    resolved = resolve(item_id, artifact.resource)
    if resolved is None:
        raise ConflictError(
            "the raster resource is no longer available at its pinned revision",
            code="raster_resource_stale",
            details={
                **key.as_dict(),
                "resource_revision": artifact.resource.revision,
            },
        )
    stream = getattr(resolved, "stream", None)
    media_type = getattr(resolved, "media_type", None)
    content_sha256 = getattr(resolved, "content_sha256", None)
    size = getattr(resolved, "size", None)
    revision = getattr(resolved, "revision", None)
    if (
        not callable(getattr(stream, "read", None))
        or not callable(getattr(stream, "seek", None))
        or not callable(getattr(stream, "close", None))
        or not isinstance(media_type, str)
        or media_type != artifact.media_type
        or not isinstance(content_sha256, str)
        or content_sha256 != artifact.content_sha256
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or revision != artifact.resource.revision
    ):
        raise RepositoryError(
            "the raster resource resolver returned an invalid result",
            code="invalid_raster_resource_resolution",
            details=key.as_dict(),
        )
    try:
        response = send_file(
            stream,
            mimetype=media_type,
            conditional=False,
            etag=content_sha256,
            max_age=0,
        )
    except BaseException:
        stream.close()
        raise
    response.content_length = size
    response.call_on_close(stream.close)
    response.cache_control.private = True
    response.cache_control.no_cache = True
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Resource-Revision"] = revision
    response.headers["Content-Digest"] = (
        "sha-256=:"
        + base64.b64encode(bytes.fromhex(content_sha256)).decode("ascii")
        + ":"
    )
    # The immutable snapshot makes validators trustworthy. Byte ranges remain
    # disabled until the transport can emit a digest for each partial message.
    return response.make_conditional(request)


def create_corrections_blueprint(
    engine_for_request: Callable[[], LibraryEngine],
    *,
    raster_resource_resolver_for_request: Callable[[], Any] | None = None,
) -> Blueprint:
    """Create the optional Corrections read transport."""

    if not callable(engine_for_request):
        raise TypeError("engine_for_request must be callable")
    if (
        raster_resource_resolver_for_request is not None
        and not callable(raster_resource_resolver_for_request)
    ):
        raise TypeError(
            "raster_resource_resolver_for_request must be callable or None"
        )

    blueprint = Blueprint("librarytool_corrections", __name__)

    @blueprint.get("/api/v1/items/<item_id>/raster-artifacts")
    def list_raster_artifacts(item_id: str):
        try:
            return _raster_list(engine_for_request, item_id)
        except EngineError as error:
            return _error_response(error)

    @blueprint.get(
        "/api/v1/items/<item_id>/raster-artifacts/<artifact_id>"
    )
    def get_raster_artifact(item_id: str, artifact_id: str):
        try:
            return _raster_detail(
                engine_for_request,
                item_id,
                artifact_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.get(
        "/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/resource"
    )
    def get_raster_resource(item_id: str, artifact_id: str):
        try:
            return _resource_response(
                engine_for_request,
                raster_resource_resolver_for_request,
                item_id,
                artifact_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.get("/api/v1/items/<item_id>/spatial-annotations")
    def list_spatial_annotations(item_id: str):
        try:
            return _spatial_list(engine_for_request, item_id)
        except EngineError as error:
            return _error_response(error)

    @blueprint.get(
        "/api/v1/items/<item_id>/spatial-annotations/<annotation_id>"
    )
    def get_spatial_annotation(item_id: str, annotation_id: str):
        try:
            return _spatial_detail(
                engine_for_request,
                item_id,
                annotation_id,
            )
        except EngineError as error:
            return _error_response(error)

    return blueprint


__all__ = [
    "ARTIFACT_PAGE_LIMIT",
    "create_corrections_blueprint",
]
