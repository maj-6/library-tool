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
from librarytool.engine.corrections import (
    AssignImageCategoryCommand,
    AssignRegionRoleCommand,
    ClearImageCategoryCommand,
    ClearRegionRoleCommand,
    CorrectionCommandResult,
    CorrectionService,
)
from librarytool.engine.correction_transforms import (
    CorrectionTransformCommand,
    CorrectionTransformService,
    QueuedCorrectionTransform,
)
from librarytool.engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterResourceRef,
    ResourceState,
)
from librarytool.engine.runtime import (
    CORRECTION_SERVICE,
    CORRECTION_TRANSFORM_SERVICE,
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
CORRECTION_MUTATION_MAX_BYTES = 64 * 1024
CORRECTION_TRANSFORM_QUEUE_SCHEMA = (
    "librarytool.correction-transform-queue-receipt/1"
)
_CURSOR_SCHEMA = "librarytool.corrections-cursor/1"
_TRANSFORM_COMMAND_FIELDS = frozenset(
    {
        "schema",
        "version",
        "item_id",
        "artifact_id",
        "artifact_revision",
        "source_revision",
        "source_sha256",
        "quad",
        "adjustment",
        "rerun_ocr",
        "operation_id",
    }
)
_RASTER_GROUP_KINDS = {
    "source-images": frozenset(
        {"capture", "captured-image", "page-image", "scan", "source-image"}
    ),
    "extracted-figures": frozenset(
        {"extracted-figure", "figure", "illustration"}
    ),
    "processed-images": frozenset(
        {
            "corrected-image",
            "perspective-corrected",
            "processed-image",
            "processed-source",
        }
    ),
    "generated-images": frozenset(
        {"generated-image", "reworked-figure", "reworked-image"}
    ),
}


def _raster_group(value: RasterArtifactView) -> str:
    kind = value.kind.casefold()
    for group, kinds in _RASTER_GROUP_KINDS.items():
        if kind in kinds:
            return group
    # RasterArtifactView is image-only. Unknown future/plugin image kinds use
    # the same generated-image fallback as the Corrections artifact model, so
    # adding a kind never makes a valid artifact disappear from the tree.
    return "generated-images"


def _error_status(error: EngineError) -> int:
    if error.code == "correction_mutation_too_large":
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


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # Cleanup must not obscure the repository contract violation.
        pass


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


def _correction_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionService:
    return _query_service(
        engine_for_request,
        CORRECTION_SERVICE,
        "correction command",
    )


def _correction_transform_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionTransformService:
    return _query_service(
        engine_for_request,
        CORRECTION_TRANSFORM_SERVICE,
        "correction transform",
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _mutation_document(fields: frozenset[str]) -> Mapping[str, Any]:
    length = request.content_length
    if length is not None and length > CORRECTION_MUTATION_MAX_BYTES:
        raise ValidationError(
            "the correction mutation document is too large",
            code="correction_mutation_too_large",
            details={"maximum_bytes": CORRECTION_MUTATION_MAX_BYTES},
        )
    if request.mimetype != "application/json":
        raise ValidationError(
            "the correction mutation must use application/json",
            code="invalid_correction_mutation_document",
            details={"content_type": str(request.content_type or "")},
        )
    charset = request.mimetype_params.get("charset", "utf-8").casefold()
    if charset not in {"utf-8", "utf8"} or request.content_encoding:
        raise ValidationError(
            "the correction mutation must use unencoded UTF-8 JSON",
            code="invalid_correction_mutation_document",
            details={"content_type": str(request.content_type or "")},
        )
    encoded = request.stream.read(CORRECTION_MUTATION_MAX_BYTES + 1)
    if len(encoded) > CORRECTION_MUTATION_MAX_BYTES:
        raise ValidationError(
            "the correction mutation document is too large",
            code="correction_mutation_too_large",
            details={"maximum_bytes": CORRECTION_MUTATION_MAX_BYTES},
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
            "the correction mutation document is invalid JSON",
            code="invalid_correction_mutation_document",
            details={"cause_type": type(error).__name__},
        ) from error
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValidationError(
            "the correction mutation fields do not match the schema",
            code="invalid_correction_mutation_envelope",
            details={"fields": sorted(fields)},
        )
    return value


def _operation_id() -> str:
    value = request.headers.get("Idempotency-Key")
    if value is None or value == "":
        raise PreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details={"header": "Idempotency-Key"},
        )
    return value


def _strong_revision(header: str) -> str:
    raw = request.headers.get(header)
    if raw is None or raw == "":
        raise PreconditionRequiredError(
            f"{header} is required",
            code="correction_target_revision_required",
            details={"header": header},
        )
    token = raw[1:-1] if len(raw) >= 2 else ""
    if (
        raw != raw.strip()
        or raw.startswith("W/")
        or len(raw) < 3
        or raw[0] != '"'
        or raw[-1] != '"'
        or len(token) > 512
        or any(
            not 0x21 <= ord(character) <= 0x7E or character in {'"', "\\"}
            for character in token
        )
    ):
        raise ValidationError(
            f"{header} must contain one strong quoted revision",
            code="invalid_correction_target_revision",
            details={"header": header},
        )
    return token


def _optional_linked_revision(linked_artifact_id: str) -> str:
    raw = request.headers.get("If-Linked-Artifact-Match")
    if linked_artifact_id:
        return _strong_revision("If-Linked-Artifact-Match")
    if raw not in (None, ""):
        raise ValidationError(
            "a linked-artifact revision requires a linked artifact",
            code="unexpected_linked_artifact_revision",
            details={"header": "If-Linked-Artifact-Match"},
        )
    return ""


def _string_field(
    document: Mapping[str, Any],
    name: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = document[name]
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValidationError(
            f"{name} must be a string",
            code="invalid_correction_mutation_document",
            details={"field": name},
        )
    return value


def _mutation_response(result: CorrectionCommandResult) -> Response:
    if not isinstance(result, CorrectionCommandResult):
        raise RepositoryError(
            "the correction service returned an invalid result",
            code="invalid_correction_mutation_result",
        )
    response = jsonify(
        {
            "ok": True,
            "schema": "librarytool.correction-mutation-receipt/1",
            **result.as_dict(),
        }
    )
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Aggregate-Revision"] = result.receipt.after_aggregate_revision
    return response


def _queue_transform(
    engine_for_request: Callable[[], LibraryEngine],
    submitter: Callable[
        [
            CorrectionTransformService,
            CorrectionTransformCommand,
            QueuedCorrectionTransform,
        ],
        None,
    ]
    | None,
    item_id: str,
    artifact_id: str,
) -> tuple[Response, int]:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Artifact-Match")
    command = CorrectionTransformCommand.from_dict(
        _mutation_document(_TRANSFORM_COMMAND_FIELDS)
    )
    mismatched = []
    if command.item_id != item_id:
        mismatched.append("item_id")
    if command.artifact_id != artifact_id:
        mismatched.append("artifact_id")
    if command.artifact_revision != expected_revision:
        mismatched.append("artifact_revision")
    if command.operation_id != operation_id:
        mismatched.append("operation_id")
    if mismatched:
        raise ValidationError(
            "the correction transform envelope does not match its command",
            code="correction_transform_envelope_mismatch",
            details={"mismatched": mismatched},
        )
    if submitter is None:
        raise RepositoryError(
            "the correction transform executor is unavailable",
            code="correction_transform_executor_unavailable",
            retryable=True,
        )

    service = _correction_transform_service(engine_for_request)
    queued = service.queue(command)
    if queued.job.state.value in {"queued", "cancelling"}:
        try:
            submitter(service, command, queued)
        except EngineError:
            raise
        except Exception as exc:
            raise RepositoryError(
                "the correction transform executor is unavailable",
                code="correction_transform_executor_unavailable",
                details={"cause_type": type(exc).__name__},
                retryable=True,
            ) from exc
    job = queued.job.as_dict()
    input_revisions = dict(job["input_revisions"])
    input_revisions.pop("command_sha256", None)
    job["input_revisions"] = input_revisions
    response = jsonify(
        {
            "ok": True,
            "schema": CORRECTION_TRANSFORM_QUEUE_SCHEMA,
            "replayed": not queued.created,
            "operation_id": command.operation_id,
            "job_id": queued.job_id,
            "job": job,
        }
    )
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    response.headers["Location"] = f"/api/v1/jobs/{queued.job_id}"
    return response, 202 if queued.created else 200


def _assign_category(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    artifact_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Artifact-Match")
    document = _mutation_document(frozenset({"category"}))
    return _mutation_response(
        _correction_service(engine_for_request).assign_category(
            AssignImageCategoryCommand(
                item_id=item_id,
                artifact_id=artifact_id,
                expected_artifact_revision=expected_revision,
                category=_string_field(document, "category"),
                operation_id=operation_id,
            )
        )
    )


def _clear_category(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    artifact_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Artifact-Match")
    _mutation_document(frozenset())
    return _mutation_response(
        _correction_service(engine_for_request).clear_category(
            ClearImageCategoryCommand(
                item_id=item_id,
                artifact_id=artifact_id,
                expected_artifact_revision=expected_revision,
                operation_id=operation_id,
            )
        )
    )


def _assign_role(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    annotation_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Annotation-Match")
    document = _mutation_document(frozenset({"role", "linked_artifact_id"}))
    linked_artifact_id = _string_field(
        document,
        "linked_artifact_id",
        allow_empty=True,
    )
    return _mutation_response(
        _correction_service(engine_for_request).assign_region_role(
            AssignRegionRoleCommand(
                item_id=item_id,
                annotation_id=annotation_id,
                expected_annotation_revision=expected_revision,
                role=_string_field(document, "role"),
                operation_id=operation_id,
                linked_artifact_id=linked_artifact_id,
                expected_linked_artifact_revision=(
                    _optional_linked_revision(linked_artifact_id)
                ),
            )
        )
    )


def _clear_role(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    annotation_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Annotation-Match")
    document = _mutation_document(frozenset({"linked_artifact_id"}))
    linked_artifact_id = _string_field(
        document,
        "linked_artifact_id",
        allow_empty=True,
    )
    return _mutation_response(
        _correction_service(engine_for_request).clear_region_role(
            ClearRegionRoleCommand(
                item_id=item_id,
                annotation_id=annotation_id,
                expected_annotation_revision=expected_revision,
                operation_id=operation_id,
                linked_artifact_id=linked_artifact_id,
                expected_linked_artifact_revision=(
                    _optional_linked_revision(linked_artifact_id)
                ),
            )
        )
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
    group = request.args.get("group", "")
    if representation_id:
        rows = [
            value
            for value in rows
            if value.source.representation_id == representation_id
        ]
    if canvas_id:
        rows = [value for value in rows if value.source.canvas_id == canvas_id]
    if group:
        kinds = _RASTER_GROUP_KINDS.get(group)
        if kinds is None:
            raise ValidationError(
                "raster artifact group is invalid",
                code="invalid_raster_artifact_group",
                details={"field": "group"},
            )
        rows = [value for value in rows if _raster_group(value) == group]
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
    body = {
        "ok": True,
        "schema": "librarytool.raster-artifact/1",
        "artifact": value.as_dict(),
    }
    return _conditional_json(
        body,
        _collection_revision("rad-", (body,)),
    )


def _spatial_list(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
) -> Response:
    representation_id = request.args.get("representation_id", "")
    canvas_id = request.args.get("canvas_id", "")
    canvas_revision = request.args.get("canvas_revision", "")
    values = _spatial_service(engine_for_request).list_spatial_annotations(
        item_id,
        representation_id=representation_id,
        canvas_id=canvas_id,
    )
    rows = _validated_annotations(values, item_id=item_id)
    if canvas_revision:
        rows = [
            value
            for value in rows
            if value.source.canvas_revision == canvas_revision
        ]
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
    body = {
        "ok": True,
        "schema": "librarytool.spatial-annotation/1",
        "annotation": value.as_dict(),
    }
    return _conditional_json(
        body,
        _collection_revision("sad-", (body,)),
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
        _close_stream(stream)
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
        _close_stream(stream)
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
    correction_transform_submitter: Callable[
        [
            CorrectionTransformService,
            CorrectionTransformCommand,
            QueuedCorrectionTransform,
        ],
        None,
    ]
    | None = None,
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
    if (
        correction_transform_submitter is not None
        and not callable(correction_transform_submitter)
    ):
        raise TypeError(
            "correction_transform_submitter must be callable or None"
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

    @blueprint.post(
        "/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/transforms"
    )
    def queue_correction_transform(item_id: str, artifact_id: str):
        try:
            return _queue_transform(
                engine_for_request,
                correction_transform_submitter,
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

    @blueprint.put("/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/category")
    def assign_image_category(item_id: str, artifact_id: str):
        try:
            return _assign_category(
                engine_for_request,
                item_id,
                artifact_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.delete("/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/category")
    def clear_image_category(item_id: str, artifact_id: str):
        try:
            return _clear_category(
                engine_for_request,
                item_id,
                artifact_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.put("/api/v1/items/<item_id>/spatial-annotations/<annotation_id>/role")
    def assign_region_role(item_id: str, annotation_id: str):
        try:
            return _assign_role(
                engine_for_request,
                item_id,
                annotation_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.delete(
        "/api/v1/items/<item_id>/spatial-annotations/<annotation_id>/role"
    )
    def clear_region_role(item_id: str, annotation_id: str):
        try:
            return _clear_role(
                engine_for_request,
                item_id,
                annotation_id,
            )
        except EngineError as error:
            return _error_response(error)

    return blueprint


__all__ = [
    "ARTIFACT_PAGE_LIMIT",
    "CORRECTION_MUTATION_MAX_BYTES",
    "CORRECTION_TRANSFORM_QUEUE_SCHEMA",
    "create_corrections_blueprint",
]
