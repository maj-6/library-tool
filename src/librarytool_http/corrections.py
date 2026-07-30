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
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from typing import Any
from urllib.parse import quote

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
    AssertArtifactMetadataCommand,
    AssignImageCategoryCommand,
    AssignRegionRoleCommand,
    ClearManualCaptionCommand,
    ClearImageCategoryCommand,
    ClearRegionRoleCommand,
    CorrectionCommandResult,
    CorrectionReviewSnapshot,
    CorrectionService,
    MarkAttentionCommand,
    ReopenCorrectionsCommand,
    ResolveCorrectionsCommand,
    SetManualCaptionCommand,
)
from librarytool.engine.correction_transforms import (
    CorrectionTransformCommand,
    CorrectionTransformService,
    QueuedCorrectionTransform,
)
from librarytool.engine.correction_ocr import CorrectionOcrProposalQueryService
from librarytool.engine.item_commands import (
    ItemCommandResult,
    ItemPatch,
    UpdateItemCommand,
)
from librarytool.engine.items import ItemQueryService, ItemView
from librarytool.engine.raster_artifacts import (
    RasterArtifactKey,
    RasterArtifactProjectorPort,
    RasterArtifactView,
    RasterResourceRef,
    ResourceState,
)
from librarytool.engine.runtime import (
    CORRECTION_CAPTION_SERVICE,
    CORRECTION_METADATA_SERVICE,
    CORRECTION_REVIEW_SERVICE,
    CORRECTION_SERVICE,
    CORRECTION_OCR_PROPOSAL_QUERY_SERVICE,
    CORRECTION_TRANSFORM_SERVICE,
    ITEM_QUERY_SERVICE,
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
CORRECTION_REVIEW_HISTORY_PAGE_LIMIT = 100
CORRECTION_REVIEW_HISTORY_TAIL_LIMIT = 8
CORRECTION_MUTATION_MAX_BYTES = 64 * 1024
CORRECTION_ITEM_DETAIL_MAX_BYTES = 1024 * 1024
CORRECTION_ITEM_METADATA_FIELD_LIMIT = 1_024
CORRECTION_ITEM_METADATA_NODE_LIMIT = 4_096
CORRECTION_ITEM_METADATA_DEPTH_LIMIT = 32
CORRECTION_ITEM_SCHEMA = "librarytool.corrections-item/1"
CORRECTION_ITEM_MUTATION_SCHEMA = (
    "librarytool.corrections-item-mutation/1"
)
CORRECTION_TRANSFORM_QUEUE_SCHEMA = (
    "librarytool.correction-transform-queue-receipt/1"
)
CORRECTIONS_INDEX_SCHEMA = "librarytool.corrections-index/2"
CORRECTIONS_INDEX_BOOK_LIMIT = 100_000
CORRECTIONS_INDEX_CAPTURE_LIMIT = 100_000
CORRECTIONS_INDEX_TOTAL_CAPTURE_LIMIT = 250_000
CORRECTIONS_INDEX_ISSUE_LIMIT = 1_024
CORRECTIONS_INDEX_MAX_BYTES = 64 * 1024 * 1024
_CURSOR_SCHEMA = "librarytool.corrections-cursor/1"
_REVIEW_HISTORY_CURSOR_SCHEMA = (
    "librarytool.correction-review-history-cursor/1"
)
_CORRECTION_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CORRECTIONS_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,255}$"
)
_ITEM_OPERATION_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
_OCR_PROPOSAL_REF_RE = re.compile(r"^cop-[0-9a-f]{40}$")
_UNSAFE_TEXT_RE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]"
)
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


def _item_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> ItemQueryService:
    return _query_service(
        engine_for_request,
        ITEM_QUERY_SERVICE,
        "item query",
    )


def _correction_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionService:
    return _query_service(
        engine_for_request,
        CORRECTION_SERVICE,
        "correction command",
    )


def _caption_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionService:
    return _query_service(
        engine_for_request,
        CORRECTION_CAPTION_SERVICE,
        "correction caption",
    )


def _metadata_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionService:
    return _query_service(
        engine_for_request,
        CORRECTION_METADATA_SERVICE,
        "correction metadata",
    )


def _review_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionService:
    return _query_service(
        engine_for_request,
        CORRECTION_REVIEW_SERVICE,
        "correction review",
    )


def _correction_transform_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionTransformService:
    return _query_service(
        engine_for_request,
        CORRECTION_TRANSFORM_SERVICE,
        "correction transform",
    )


def _correction_ocr_proposal_service(
    engine_for_request: Callable[[], LibraryEngine],
) -> CorrectionOcrProposalQueryService:
    return _query_service(
        engine_for_request,
        CORRECTION_OCR_PROPOSAL_QUERY_SERVICE,
        "correction ocr proposal",
    )


def _correction_ocr_proposal_detail(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    proposal_ref: str,
) -> Response:
    if _OCR_PROPOSAL_REF_RE.fullmatch(proposal_ref) is None:
        raise ValidationError(
            "the OCR proposal reference is invalid",
            code="invalid_correction_ocr_proposal_ref",
        )
    proposal = _correction_ocr_proposal_service(
        engine_for_request
    ).get_proposal(item_id, proposal_ref)
    if proposal is None:
        raise NotFoundError(
            "the OCR proposal does not exist",
            code="correction_ocr_proposal_not_found",
            details={
                "item_id": item_id,
                "proposal_ref": proposal_ref,
            },
        )
    response = jsonify(
        {
            "ok": True,
            "schema": "librarytool.correction-ocr-proposal/1",
            "proposal": proposal.as_dict(),
        }
    )
    response.cache_control.private = True
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.set_etag(proposal.content_sha256, weak=False)
    return response


def _correction_actor_id(
    actor_id_for_request: Callable[[], str],
) -> str:
    try:
        value = actor_id_for_request()
    except EngineError:
        raise
    except Exception as exc:
        raise RepositoryError(
            "the local correction actor is unavailable",
            code="correction_actor_unavailable",
            retryable=True,
        ) from exc
    if not isinstance(value, str) or not _CORRECTION_ACTOR_RE.fullmatch(value):
        raise RepositoryError(
            "the local correction actor is invalid",
            code="invalid_correction_actor",
        )
    return value


def _correction_workspace_id(
    workspace_id_for_request: Callable[[], str],
) -> str:
    try:
        value = workspace_id_for_request()
    except EngineError:
        raise
    except Exception as exc:
        raise RepositoryError(
            "the active Corrections workspace is unavailable",
            code="corrections_workspace_unavailable",
            retryable=True,
        ) from exc
    if (
        not isinstance(value, str)
        or len(value) > 256
        or not _CORRECTIONS_IDENTIFIER_RE.fullmatch(value)
    ):
        raise RepositoryError(
            "the active Corrections workspace identity is invalid",
            code="invalid_corrections_workspace_authority",
        )
    return value


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


def _mapping_field(
    document: Mapping[str, Any],
    name: str,
) -> Mapping[str, Any]:
    value = document[name]
    if not isinstance(value, Mapping):
        raise ValidationError(
            f"{name} must be an object",
            code="invalid_correction_mutation_document",
            details={"field": name},
        )
    return value


def _string_array_field(
    document: Mapping[str, Any],
    name: str,
) -> tuple[str, ...]:
    value = document[name]
    if not isinstance(value, list) or any(
        not isinstance(entry, str) or not entry for entry in value
    ):
        raise ValidationError(
            f"{name} must be an array of non-empty strings",
            code="invalid_correction_mutation_document",
            details={"field": name},
        )
    return tuple(value)


def _correction_item_id(item_id: str) -> str:
    if (
        not isinstance(item_id, str)
        or not _CORRECTIONS_IDENTIFIER_RE.fullmatch(item_id)
    ):
        raise ValidationError(
            "the Corrections item id is invalid",
            code="invalid_correction_item_id",
            details={"field": "item_id"},
        )
    return item_id


def _correction_item_operation_id(item_id: str) -> str:
    value = request.headers.get("Idempotency-Key")
    if value is None or value == "":
        raise PreconditionRequiredError(
            "an idempotency key is required",
            code="idempotency_key_required",
            details={
                "header": "Idempotency-Key",
                "item_id": item_id,
            },
        )
    if _ITEM_OPERATION_RE.fullmatch(value) is None:
        raise ValidationError(
            "Idempotency-Key must contain one portable operation id",
            code="invalid_operation_id",
            details={
                "header": "Idempotency-Key",
                "item_id": item_id,
            },
        )
    return value


def _is_correction_item_revision(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 512
        and value == value.strip()
        and all(
            0x21 <= ord(character) <= 0x7E
            and character not in {'"', "\\"}
            for character in value
        )
    )


def _correction_item_record_match(item_id: str) -> str:
    raw = request.headers.get("If-Record-Match")
    if raw is None or raw == "":
        raise PreconditionRequiredError(
            "an item revision is required",
            code="item_revision_required",
            details={
                "header": "If-Record-Match",
                "item_id": item_id,
            },
        )
    value = raw[1:-1] if len(raw) >= 2 else ""
    if (
        raw != raw.strip()
        or raw.startswith("W/")
        or len(raw) < 3
        or raw[0] != '"'
        or raw[-1] != '"'
        or not _is_correction_item_revision(value)
    ):
        raise ValidationError(
            "If-Record-Match must contain one strong quoted item revision",
            code="invalid_item_revision",
            details={
                "header": "If-Record-Match",
                "item_id": item_id,
            },
        )
    return value


def _correction_item_metadata_budget(value: Mapping[str, Any]) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > CORRECTION_ITEM_METADATA_NODE_LIMIT:
            raise ValidationError(
                "the Corrections item metadata patch is too complex",
                code="correction_item_metadata_too_complex",
                details={
                    "maximum_nodes": (
                        CORRECTION_ITEM_METADATA_NODE_LIMIT
                    )
                },
            )
        if depth > CORRECTION_ITEM_METADATA_DEPTH_LIMIT:
            raise ValidationError(
                "the Corrections item metadata patch is nested too deeply",
                code="correction_item_metadata_too_complex",
                details={
                    "maximum_depth": (
                        CORRECTION_ITEM_METADATA_DEPTH_LIMIT
                    )
                },
            )
        if isinstance(current, Mapping):
            if len(current) > CORRECTION_ITEM_METADATA_FIELD_LIMIT:
                raise ValidationError(
                    "the Corrections item metadata patch has too many fields",
                    code="correction_item_metadata_too_complex",
                    details={
                        "maximum_fields": (
                            CORRECTION_ITEM_METADATA_FIELD_LIMIT
                        )
                    },
                )
            stack.extend(
                (item, depth + 1) for item in current.values()
            )
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _correction_item_patch(item_id: str) -> ItemPatch:
    document = _mutation_document(frozenset({"patch"}))
    raw = document["patch"]
    fields = {"title", "metadata_set", "metadata_remove"}
    if (
        not isinstance(raw, Mapping)
        or set(raw) != fields
        or (
            raw.get("title") is not None
            and not isinstance(raw.get("title"), str)
        )
        or not isinstance(raw.get("metadata_set"), Mapping)
        or not isinstance(raw.get("metadata_remove"), list)
        or len(raw["metadata_set"])
        > CORRECTION_ITEM_METADATA_FIELD_LIMIT
        or len(raw["metadata_remove"])
        > CORRECTION_ITEM_METADATA_FIELD_LIMIT
    ):
        raise ValidationError(
            "the Corrections item patch does not match its schema",
            code="invalid_correction_item_patch",
        )
    _correction_item_metadata_budget(raw["metadata_set"])
    try:
        patch = ItemPatch(
            title=raw["title"],
            metadata_set=raw["metadata_set"],
            metadata_remove=tuple(raw["metadata_remove"]),
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise ValidationError(
            "the Corrections item patch is invalid",
            code="invalid_correction_item_patch",
            details={"cause_type": type(error).__name__},
        ) from error
    if patch.title is not None and patch.title != patch.title.strip():
        raise ValidationError(
            "the Corrections item title cannot have outer whitespace",
            code="invalid_correction_item_patch",
            details={"field": "title"},
        )
    if patch.is_empty:
        raise ValidationError(
            "the Corrections item patch has no changes",
            code="empty_item_patch",
            details={"item_id": item_id},
        )
    return patch


def _correction_item_query_service(
    engine_for_request: Callable[[], LibraryEngine],
    item_service_for_request: Callable[[], ItemQueryService] | None,
) -> Any:
    if item_service_for_request is None:
        return _item_service(engine_for_request)
    try:
        service = item_service_for_request()
    except EngineError as error:
        raise RepositoryError(
            "the Corrections item query is unavailable",
            code="corrections_item_query_unavailable",
            retryable=error.retryable,
        ) from error
    except Exception as error:
        raise RepositoryError(
            "the Corrections item query is unavailable",
            code="corrections_item_query_unavailable",
            details={"cause_type": type(error).__name__},
            retryable=True,
        ) from error
    if not callable(getattr(service, "get_item", None)):
        raise RepositoryError(
            "the Corrections item query is unavailable",
            code="corrections_item_query_unavailable",
            retryable=True,
        )
    return service


def _correction_item_not_found(item_id: str) -> NotFoundError:
    return NotFoundError(
        "the Corrections item does not exist",
        code="correction_item_not_found",
        details={"item_id": item_id},
    )


def _correction_item_view(
    engine_for_request: Callable[[], LibraryEngine],
    item_service_for_request: Callable[[], ItemQueryService] | None,
    item_id: str,
) -> dict[str, Any]:
    service = _correction_item_query_service(
        engine_for_request,
        item_service_for_request,
    )
    try:
        item = service.get_item(item_id)
    except NotFoundError as error:
        raise _correction_item_not_found(item_id) from error
    except EngineError as error:
        raise RepositoryError(
            "the Corrections item query failed",
            code="corrections_item_query_unavailable",
            retryable=error.retryable,
        ) from error
    except Exception as error:
        raise RepositoryError(
            "the Corrections item query failed",
            code="corrections_item_query_unavailable",
            details={"cause_type": type(error).__name__},
            retryable=True,
        ) from error

    if not isinstance(item, ItemView) or item.item_id != item_id:
        raise RepositoryError(
            "the Corrections item query returned an invalid view",
            code="invalid_correction_item_projection",
            details={"item_id": item_id},
        )
    kind = item.kind.casefold() if isinstance(item.kind, str) else ""
    if kind not in {"book", "capture"}:
        raise _correction_item_not_found(item_id)
    if (
        not _is_correction_item_revision(item.record_revision)
        or not isinstance(item.title, str)
        or len(item.title) > 4096
        or _UNSAFE_TEXT_RE.search(item.title)
    ):
        raise RepositoryError(
            "the Corrections item query returned an invalid view",
            code="invalid_correction_item_projection",
            details={"item_id": item_id},
        )
    try:
        serialized = item.as_dict()
        metadata = serialized["metadata"]
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata is not an object")
        public = {
            "id": item_id,
            "kind": kind,
            "title": item.title,
            "metadata": dict(metadata),
            "record_revision": item.record_revision,
        }
        encoded = json.dumps(
            public,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise RepositoryError(
            "the Corrections item query returned an invalid view",
            code="invalid_correction_item_projection",
            details={"item_id": item_id},
        ) from error
    if len(encoded) > CORRECTION_ITEM_DETAIL_MAX_BYTES:
        raise RepositoryError(
            "the Corrections item detail exceeds its size budget",
            code="invalid_correction_item_projection",
            details={
                "item_id": item_id,
                "maximum_bytes": CORRECTION_ITEM_DETAIL_MAX_BYTES,
            },
        )
    return public


def _correction_item_response(
    item: Mapping[str, Any],
    *,
    replayed: bool | None = None,
) -> Response:
    body: dict[str, Any] = {
        "ok": True,
        "schema": (
            CORRECTION_ITEM_SCHEMA
            if replayed is None
            else CORRECTION_ITEM_MUTATION_SCHEMA
        ),
        "item": dict(item),
    }
    if replayed is not None:
        body["replayed"] = replayed
    response = jsonify(body)
    projection_revision = "cid-" + hashlib.sha256(
        json.dumps(
            dict(item),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    response.set_etag(projection_revision, weak=False)
    response.headers["X-Record-Revision"] = item["record_revision"]
    response.cache_control.private = True
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response if replayed is not None else response.make_conditional(
        request
    )


def _get_correction_item(
    engine_for_request: Callable[[], LibraryEngine],
    item_service_for_request: Callable[[], ItemQueryService] | None,
    item_id: str,
) -> Response:
    item_id = _correction_item_id(item_id)
    if request.args:
        raise ValidationError(
            "the Corrections item detail does not accept query parameters",
            code="invalid_correction_item_request",
        )
    return _correction_item_response(
        _correction_item_view(
            engine_for_request,
            item_service_for_request,
            item_id,
        )
    )


def _correction_item_update_service(
    service_for_request: Callable[[], Any] | None,
) -> Any:
    if service_for_request is None:
        raise RepositoryError(
            "the Corrections item update module is unavailable",
            code="correction_item_update_module_unavailable",
            retryable=True,
        )
    try:
        service = service_for_request()
    except EngineError as error:
        raise RepositoryError(
            "the Corrections item update module is unavailable",
            code="correction_item_update_module_unavailable",
            retryable=error.retryable,
        ) from error
    except Exception as error:
        raise RepositoryError(
            "the Corrections item update module is unavailable",
            code="correction_item_update_module_unavailable",
            details={"cause_type": type(error).__name__},
            retryable=True,
        ) from error
    if not callable(getattr(service, "update", None)):
        raise RepositoryError(
            "the Corrections item update module is unavailable",
            code="correction_item_update_module_unavailable",
            retryable=True,
        )
    return service


def _correction_item_update_error(
    error: EngineError,
    *,
    item_id: str,
    expected_revision: str,
    operation_id: str,
) -> EngineError:
    if isinstance(error, NotFoundError):
        return _correction_item_not_found(item_id)
    if isinstance(error, ConflictError):
        if error.code == "operation_id_conflict":
            return ConflictError(
                "the idempotency key was already used for another edit",
                code="operation_id_conflict",
                details={"operation_id": operation_id},
            )
        if error.code == "item_revision_conflict":
            details = {
                "item_id": item_id,
                "expected_revision": expected_revision,
            }
            current_revision = error.details.get("current_revision")
            if _is_correction_item_revision(current_revision):
                details["current_revision"] = current_revision
            return ConflictError(
                "the Corrections item changed elsewhere",
                code="item_revision_conflict",
                details=details,
            )
        return ConflictError(
            "the Corrections item update conflicted",
            code="correction_item_update_conflict",
            details={"item_id": item_id},
        )
    if isinstance(error, ValidationError):
        details: dict[str, Any] = {"item_id": item_id}
        for key in ("field", "reason", "cause_type", "kind"):
            value = error.details.get(key)
            if (
                isinstance(value, str)
                and len(value) <= 256
                and not _UNSAFE_TEXT_RE.search(value)
            ):
                details[key] = value
        fields = error.details.get("fields")
        if (
            isinstance(fields, (list, tuple))
            and len(fields) <= CORRECTION_ITEM_METADATA_FIELD_LIMIT
            and all(
                isinstance(value, str)
                and len(value) <= 256
                and not _UNSAFE_TEXT_RE.search(value)
                for value in fields
            )
        ):
            details["fields"] = list(fields)
        safe_codes = {
            "empty_item_patch",
            "invalid_item_metadata",
            "managed_item_fields_not_writable",
            "unsupported_item_kind",
        }
        return ValidationError(
            "the Corrections item update is invalid",
            code=(
                error.code
                if error.code in safe_codes
                else "invalid_correction_item_update"
            ),
            details=details,
        )
    return RepositoryError(
        "the Corrections item update failed",
        code="correction_item_update_unavailable",
        retryable=error.retryable,
    )


def _update_correction_item(
    engine_for_request: Callable[[], LibraryEngine],
    item_service_for_request: Callable[[], ItemQueryService] | None,
    update_service_for_request: Callable[[], Any] | None,
    item_id: str,
) -> Response:
    item_id = _correction_item_id(item_id)
    if request.args:
        raise ValidationError(
            "the Corrections item update does not accept query parameters",
            code="invalid_correction_item_request",
        )
    operation_id = _correction_item_operation_id(item_id)
    expected_revision = _correction_item_record_match(item_id)
    patch = _correction_item_patch(item_id)
    service = _correction_item_update_service(
        update_service_for_request
    )
    command = UpdateItemCommand(
        item_id=item_id,
        expected_revision=expected_revision,
        patch=patch,
        operation_id=operation_id,
    )
    try:
        result = service.update(command)
    except EngineError as error:
        raise _correction_item_update_error(
            error,
            item_id=item_id,
            expected_revision=expected_revision,
            operation_id=operation_id,
        ) from error
    except Exception as error:
        raise RepositoryError(
            "the Corrections item update failed",
            code="correction_item_update_unavailable",
            details={"cause_type": type(error).__name__},
            retryable=True,
        ) from error
    if (
        not isinstance(result, ItemCommandResult)
        or result.receipt.action != "update"
        or result.receipt.operation_id != operation_id
        or result.receipt.before_revision != expected_revision
    ):
        raise RepositoryError(
            "the Corrections item update returned an invalid result",
            code="invalid_correction_item_update_result",
        )
    item = _correction_item_view(
        engine_for_request,
        item_service_for_request,
        item_id,
    )
    if not result.replayed and item["record_revision"] == expected_revision:
        raise RepositoryError(
            "the Corrections item projection did not advance",
            code="stale_correction_item_projection",
            details={"item_id": item_id},
            retryable=True,
        )
    return _correction_item_response(
        item,
        replayed=result.replayed,
    )


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


def _set_manual_caption(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    artifact_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Artifact-Match")
    document = _mutation_document(frozenset({"text", "language"}))
    return _mutation_response(
        _caption_service(engine_for_request).set_manual_caption(
            SetManualCaptionCommand(
                item_id=item_id,
                artifact_id=artifact_id,
                expected_artifact_revision=expected_revision,
                text=_string_field(document, "text"),
                operation_id=operation_id,
                language=_string_field(document, "language", allow_empty=True),
            )
        )
    )


def _clear_manual_caption(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    artifact_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Artifact-Match")
    _mutation_document(frozenset())
    return _mutation_response(
        _caption_service(engine_for_request).clear_manual_caption(
            ClearManualCaptionCommand(
                item_id=item_id,
                artifact_id=artifact_id,
                expected_artifact_revision=expected_revision,
                operation_id=operation_id,
            )
        )
    )


def _assert_artifact_metadata(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
    artifact_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Artifact-Match")
    document = _mutation_document(frozenset({"assertions", "clear_names"}))
    try:
        command = AssertArtifactMetadataCommand(
            item_id=item_id,
            artifact_id=artifact_id,
            expected_artifact_revision=expected_revision,
            operation_id=operation_id,
            assertions=_mapping_field(document, "assertions"),
            clear_names=_string_array_field(document, "clear_names"),
        )
    except (TypeError, ValueError) as error:
        raise ValidationError(
            "the metadata assertion document is invalid",
            code="invalid_correction_mutation_document",
        ) from error
    return _mutation_response(
        _metadata_service(engine_for_request).assert_artifact_metadata(
            command
        )
    )


def _mark_attention(
    engine_for_request: Callable[[], LibraryEngine],
    actor_id_for_request: Callable[[], str],
    item_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Review-Match")
    document = _mutation_document(frozenset({"reason", "comment"}))
    return _mutation_response(
        _review_service(engine_for_request).mark_attention(
            MarkAttentionCommand(
                item_id=item_id,
                expected_review_revision=expected_revision,
                reason=_string_field(document, "reason"),
                actor_id=_correction_actor_id(actor_id_for_request),
                operation_id=operation_id,
                comment=_string_field(document, "comment", allow_empty=True),
            )
        )
    )


def _resolve_corrections(
    engine_for_request: Callable[[], LibraryEngine],
    actor_id_for_request: Callable[[], str],
    item_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Review-Match")
    document = _mutation_document(frozenset({"comment"}))
    return _mutation_response(
        _review_service(engine_for_request).resolve(
            ResolveCorrectionsCommand(
                item_id=item_id,
                expected_review_revision=expected_revision,
                actor_id=_correction_actor_id(actor_id_for_request),
                operation_id=operation_id,
                comment=_string_field(document, "comment", allow_empty=True),
            )
        )
    )


def _reopen_corrections(
    engine_for_request: Callable[[], LibraryEngine],
    actor_id_for_request: Callable[[], str],
    item_id: str,
) -> Response:
    operation_id = _operation_id()
    expected_revision = _strong_revision("If-Review-Match")
    document = _mutation_document(frozenset({"comment"}))
    return _mutation_response(
        _review_service(engine_for_request).reopen(
            ReopenCorrectionsCommand(
                item_id=item_id,
                expected_review_revision=expected_revision,
                actor_id=_correction_actor_id(actor_id_for_request),
                operation_id=operation_id,
                comment=_string_field(document, "comment", allow_empty=True),
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


def _limit(*, maximum: int = ARTIFACT_PAGE_LIMIT) -> int:
    raw = request.args.get("limit", "100")
    try:
        value = int(raw, 10)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            "limit must be an integer",
            code="invalid_corrections_page_limit",
            details={"field": "limit"},
        ) from error
    if value < 1 or value > maximum:
        raise ValidationError(
            f"limit must be between 1 and {maximum}",
            code="invalid_corrections_page_limit",
            details={"field": "limit", "maximum": maximum},
        )
    return value


def _encode_cursor(
    revision: str,
    offset: int,
    *,
    schema: str = _CURSOR_SCHEMA,
) -> str:
    payload = json.dumps(
        {"schema": schema, "revision": revision, "offset": offset},
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_offset(
    revision: str,
    total: int,
    *,
    schema: str = _CURSOR_SCHEMA,
    conflict_code: str = "corrections_collection_changed",
    conflict_message: str = (
        "the artifact collection changed while it was being paged"
    ),
) -> int:
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
        or value.get("schema") != schema
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
            conflict_message,
            code=conflict_code,
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
    maximum: int = ARTIFACT_PAGE_LIMIT,
    cursor_schema: str = _CURSOR_SCHEMA,
    conflict_code: str = "corrections_collection_changed",
    conflict_message: str = (
        "the artifact collection changed while it was being paged"
    ),
) -> tuple[list[Mapping[str, Any]], str | None]:
    limit = _limit(maximum=maximum)
    offset = _cursor_offset(
        revision,
        len(rows),
        schema=cursor_schema,
        conflict_code=conflict_code,
        conflict_message=conflict_message,
    )
    values = list(rows[offset : offset + limit])
    next_offset = offset + len(values)
    cursor = (
        _encode_cursor(
            revision,
            next_offset,
            schema=cursor_schema,
        )
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


def _index_review_summary(
    review: CorrectionReviewSnapshot,
) -> dict[str, Any]:
    return {
        "revision": review.revision,
        "state": review.state.value,
        "reason": review.reason,
        "history_count": len(review.history),
        "latest_event": (
            review.history[-1].as_dict() if review.history else None
        ),
    }


def _invalid_index_projection(
    message: str,
    *,
    item_id: str = "",
    details: Mapping[str, Any] | None = None,
) -> RepositoryError:
    error_details = dict(details or {})
    if item_id:
        error_details["item_id"] = item_id
    return RepositoryError(
        message,
        code="invalid_corrections_index_projection",
        details=error_details,
    )


def _index_text(
    value: Any,
    *,
    maximum: int,
    allow_empty: bool = True,
) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and not _UNSAFE_TEXT_RE.search(value)
        and (allow_empty or bool(value.strip()))
    )


def _attention_key(item_id: str) -> str:
    direct = f"attention:{item_id}"
    if len(direct) <= 256:
        return direct
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return f"attention:{digest}"


def _index_encoded_size(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _capture_import_state(value: RasterArtifactView) -> str:
    if value.resource_state is ResourceState.MISSING:
        return "missing"
    if value.resource_state is ResourceState.UNAVAILABLE:
        return "unavailable"
    return "ready"


def _capture_rank(value: RasterArtifactView) -> tuple[int, str]:
    variant = value.resource.variant if value.resource is not None else ""
    is_display = (
        variant == "display"
        or value.key.artifact_id.endswith(":display")
    )
    return (0 if is_display else 1, value.key.artifact_id)


def _capture_rows(
    item_id: str,
    values: Sequence[RasterArtifactView],
) -> list[dict[str, Any]]:
    by_order: dict[int, list[RasterArtifactView]] = {}
    for value in values:
        order = value.extensions.get("capture_order")
        if (
            isinstance(order, bool)
            or not isinstance(order, int)
            or order < 0
        ):
            continue
        by_order.setdefault(order, []).append(value)

    rows: list[dict[str, Any]] = []
    for order in sorted(by_order):
        value = min(by_order[order], key=_capture_rank)
        source = value.source
        row: dict[str, Any] = {
            "artifact_id": value.key.artifact_id,
            "revision": value.revision,
            "capture_order": order,
            "label": value.label,
            "effective_category": value.effective_category,
            "resource_state": value.resource_state.value,
            "import_state": _capture_import_state(value),
            "freshness": value.freshness.value,
            "thumbnail": None,
        }
        if source.representation_id:
            row["representation_id"] = source.representation_id
        if source.canvas_id:
            row["canvas_id"] = source.canvas_id
        if (
            value.resource_state is ResourceState.AVAILABLE
            and value.resource is not None
        ):
            thumbnail_url = (
                f"/api/v1/items/{quote(item_id, safe='')}/"
                "raster-artifacts/"
                f"{quote(value.key.artifact_id, safe='')}/resource?"
                f"revision={quote(value.resource.revision, safe='')}"
            )
            if not _index_text(
                thumbnail_url,
                maximum=4096,
                allow_empty=False,
            ):
                raise _invalid_index_projection(
                    "the Corrections thumbnail reference is invalid",
                    item_id=item_id,
                )
            row["thumbnail"] = {
                "url": thumbnail_url,
                "alt": value.label or f"Capture {order + 1}",
                "width": value.dimensions.width,
                "height": value.dimensions.height,
            }
        rows.append(row)
    if len(rows) > CORRECTIONS_INDEX_CAPTURE_LIMIT:
        raise _invalid_index_projection(
            "the Corrections index contains too many captures",
            item_id=item_id,
        )
    return rows


def _book_import_state(captures: Sequence[Mapping[str, Any]]) -> str:
    if not captures:
        return "ready"
    states = [value["import_state"] for value in captures]
    if all(value == "missing" for value in states):
        return "missing"
    if all(value == "unavailable" for value in states):
        return "unavailable"
    if any(value != "ready" for value in states):
        return "partial"
    return "ready"


def _item_has_capture_inventory(item: ItemView) -> bool:
    if item.kind.casefold() == "capture":
        return True
    origin = item.metadata.get("origin")
    return origin in {"captured_entry", "promoted_capture"} or bool(
        item.metadata.get("capture_id")
    )


def _index_capture_inventory(
    rasters: Any,
    item: ItemView,
) -> tuple[list[dict[str, Any]], str, tuple[str, ...]]:
    """Project one independently fallible capture inventory for the index."""

    captures = _capture_rows(
        item.item_id,
        _validated_rasters(
            rasters.list_raster_artifacts(item.item_id),
            item_id=item.item_id,
        ),
    )
    if _item_has_capture_inventory(item) and not captures:
        return (
            [],
            "missing",
            ("Captured image manifest is missing",),
        )
    return captures, _book_import_state(captures), ()


def _book_issues(
    item: ItemView,
    captures: Sequence[Mapping[str, Any]],
    *,
    inventory_issues: Sequence[str] = (),
) -> list[str]:
    issues = [
        value
        for value in item.workbench_state.issues
        if not (
            value == "representation.missing"
            and captures
        )
    ]
    issues.extend(inventory_issues)
    missing = sum(
        value["resource_state"] == "missing" for value in captures
    )
    unavailable = sum(
        value["resource_state"] == "unavailable" for value in captures
    )
    if missing:
        issues.append(
            f"{missing} captured image"
            f"{' is' if missing == 1 else 's are'} missing"
        )
    if unavailable:
        issues.append(
            f"{unavailable} captured image"
            f"{' is' if unavailable == 1 else 's are'} unavailable"
        )
    values = list(dict.fromkeys(issues))
    if (
        len(values) > CORRECTIONS_INDEX_ISSUE_LIMIT
        or any(
            not _index_text(value, maximum=2048, allow_empty=False)
            for value in values
        )
    ):
        raise _invalid_index_projection(
            "the item query returned invalid Corrections issues",
            item_id=item.item_id,
        )
    return values


def _validated_items(value: Any) -> list[ItemView]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RepositoryError(
            "the item query returned an invalid collection",
            code="invalid_corrections_index_projection",
        )
    rows = list(value)
    if len(rows) > CORRECTIONS_INDEX_BOOK_LIMIT:
        raise _invalid_index_projection(
            "the item query returned too many Corrections books"
        )
    if any(not isinstance(item, ItemView) for item in rows):
        raise RepositoryError(
            "the item query returned an invalid view",
            code="invalid_corrections_index_projection",
        )
    if any(
        not isinstance(item.item_id, str)
        or not _CORRECTIONS_IDENTIFIER_RE.fullmatch(item.item_id)
        or not _index_text(item.title, maximum=2048)
        for item in rows
    ):
        raise _invalid_index_projection(
            "the item query returned a nonportable Corrections book"
        )
    identities = [item.item_id.casefold() for item in rows]
    if len(identities) != len(set(identities)):
        raise RepositoryError(
            "the item query returned duplicate identities",
            code="invalid_corrections_index_projection",
        )
    return rows


def _validate_corrections_workspace(
    workspace_id_for_request: Callable[[], str],
) -> None:
    workspace_ids = request.args.getlist("workspace_id")
    workspace_id = workspace_ids[0] if len(workspace_ids) == 1 else ""
    if (
        set(request.args) != {"workspace_id"}
        or not workspace_id
        or (
        len(workspace_id) > 256
        or not _CORRECTIONS_IDENTIFIER_RE.fullmatch(workspace_id)
        )
    ):
        raise ValidationError(
            "workspace_id must be a portable identifier",
            code="invalid_corrections_workspace",
            details={"field": "workspace_id"},
        )
    active_workspace_id = _correction_workspace_id(
        workspace_id_for_request
    )
    if workspace_id != active_workspace_id:
        raise ConflictError(
            "the requested Corrections workspace is not active",
            code="corrections_workspace_mismatch",
            details={
                "requested_workspace_id": workspace_id,
                "active_workspace_id": active_workspace_id,
            },
        )


def _corrections_index_projection(
    engine_for_request: Callable[[], LibraryEngine],
    item_service_for_request: Callable[[], ItemQueryService] | None = None,
) -> Response:
    reviews = _review_service(engine_for_request)
    rasters = _raster_service(engine_for_request)
    books: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    capture_count = 0
    projected_bytes = 64
    item_service = (
        _item_service(engine_for_request)
        if item_service_for_request is None
        else item_service_for_request()
    )
    if not callable(getattr(item_service, "list_items", None)):
        raise RepositoryError(
            "the Corrections item query is unavailable",
            code="corrections_item_query_unavailable",
            retryable=True,
        )
    items = _validated_items(item_service.list_items())
    for item in items:
        item_kind = item.kind.casefold()
        if item_kind not in {"book", "capture"}:
            continue
        review = reviews.get_review(item.item_id)
        if not isinstance(review, CorrectionReviewSnapshot):
            raise RepositoryError(
                "the correction service returned an invalid review",
                code="invalid_correction_review",
                details={"item_id": item.item_id},
            )
        captures, import_state, inventory_issues = (
            _index_capture_inventory(rasters, item)
        )
        capture_count += len(captures)
        if capture_count > CORRECTIONS_INDEX_TOTAL_CAPTURE_LIMIT:
            raise _invalid_index_projection(
                "the Corrections index contains too many captures",
                item_id=item.item_id,
                details={
                    "maximum_captures": (
                        CORRECTIONS_INDEX_TOTAL_CAPTURE_LIMIT
                    )
                },
            )
        review_summary = _index_review_summary(review)
        without_revision = {
            "id": item.item_id,
            "kind": item_kind,
            "title": item.title,
            "import_state": import_state,
            "issues": _book_issues(
                item,
                captures,
                inventory_issues=inventory_issues,
            ),
            "review": review_summary,
            "captures": captures,
        }
        book = {
            "id": item.item_id,
            "revision": _collection_revision(
                "crb-",
                (
                    {
                        "item_revision": item.revision,
                        **without_revision,
                    },
                ),
            ),
            **{
                key: value
                for key, value in without_revision.items()
                if key != "id"
            },
        }
        projected_bytes += _index_encoded_size(book) + 1
        if projected_bytes > CORRECTIONS_INDEX_MAX_BYTES:
            raise _invalid_index_projection(
                "the Corrections index exceeds its serialized size budget",
                item_id=item.item_id,
                details={"maximum_bytes": CORRECTIONS_INDEX_MAX_BYTES},
            )
        books.append(book)
        if review.state.value != "clear":
            entry = {
                "key": _attention_key(item.item_id),
                "target": {
                    "kind": "book",
                    "item_id": item.item_id,
                },
                "review": review_summary,
            }
            projected_bytes += _index_encoded_size(entry) + 1
            if projected_bytes > CORRECTIONS_INDEX_MAX_BYTES:
                raise _invalid_index_projection(
                    "the Corrections index exceeds its serialized size budget",
                    item_id=item.item_id,
                    details={"maximum_bytes": CORRECTIONS_INDEX_MAX_BYTES},
                )
            attention.append(entry)

    books.sort(key=lambda value: (value["title"].casefold(), value["id"]))
    attention.sort(key=lambda value: value["key"])
    revision = _collection_revision(
        "cri-",
        ({"books": books, "attention": attention},),
    )
    body = {
        "ok": True,
        "schema": CORRECTIONS_INDEX_SCHEMA,
        "revision": revision,
        "books": books,
        "attention": attention,
    }
    if _index_encoded_size(body) > CORRECTIONS_INDEX_MAX_BYTES:
        raise _invalid_index_projection(
            "the Corrections index exceeds its serialized size budget",
            details={"maximum_bytes": CORRECTIONS_INDEX_MAX_BYTES},
        )
    return _conditional_json(body, revision)


def _corrections_index(
    engine_for_request: Callable[[], LibraryEngine],
    workspace_id_for_request: Callable[[], str],
    item_service_for_request: Callable[[], ItemQueryService] | None = None,
    index_context_for_request: Callable[[], Any] | None = None,
) -> Response:
    _validate_corrections_workspace(workspace_id_for_request)
    operation_context = (
        nullcontext()
        if index_context_for_request is None
        else index_context_for_request()
    )
    with operation_context:
        return _corrections_index_projection(
            engine_for_request,
            item_service_for_request,
        )


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


def _review_detail(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
) -> Response:
    review = _review_service(engine_for_request).get_review(item_id)
    if not isinstance(review, CorrectionReviewSnapshot):
        raise RepositoryError(
            "the correction service returned an invalid review",
            code="invalid_correction_review",
            details={"item_id": item_id},
        )
    return _conditional_json(
        {
            "ok": True,
            "schema": "librarytool.correction-review/1",
            "item_id": item_id,
            "review": {
                "revision": review.revision,
                "state": review.state.value,
                "reason": review.reason,
                "history_count": len(review.history),
                "history_tail": [
                    event.as_dict()
                    for event in review.history[
                        -CORRECTION_REVIEW_HISTORY_TAIL_LIMIT:
                    ]
                ],
            },
        },
        review.revision,
    )


def _review_history(
    engine_for_request: Callable[[], LibraryEngine],
    item_id: str,
) -> Response:
    expected_revision = _strong_revision("If-Review-Match")
    review = _review_service(engine_for_request).get_review(item_id)
    if not isinstance(review, CorrectionReviewSnapshot):
        raise RepositoryError(
            "the correction service returned an invalid review",
            code="invalid_correction_review",
            details={"item_id": item_id},
        )
    if review.revision != expected_revision:
        raise ConflictError(
            "the correction review changed",
            code="review_revision_conflict",
            details={
                "expected_revision": expected_revision,
                "actual_revision": review.revision,
            },
        )
    limit = _limit(maximum=CORRECTION_REVIEW_HISTORY_PAGE_LIMIT)
    offset = _cursor_offset(
        review.revision,
        len(review.history),
        schema=_REVIEW_HISTORY_CURSOR_SCHEMA,
        conflict_code="review_revision_conflict",
        conflict_message="the correction review changed while it was being paged",
    )
    if request.args.get("cursor", "") and offset == len(review.history):
        raise ValidationError(
            "cursor is invalid",
            code="invalid_corrections_cursor",
            details={"field": "cursor"},
        )
    selected = review.history[offset : offset + limit]
    next_offset = offset + len(selected)
    next_cursor = (
        _encode_cursor(
            review.revision,
            next_offset,
            schema=_REVIEW_HISTORY_CURSOR_SCHEMA,
        )
        if next_offset < len(review.history)
        else None
    )
    page = [event.as_dict() for event in selected]
    return _conditional_json(
        {
            "ok": True,
            "schema": "librarytool.correction-review-history/1",
            "item_id": item_id,
            "review_revision": review.revision,
            "review_state": review.state.value,
            "events": page,
            "next_cursor": next_cursor,
            "total": len(review.history),
        },
        review.revision,
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
    correction_index_context_for_request: Callable[[], Any] | None = None,
    correction_item_service_for_request: (
        Callable[[], ItemQueryService] | None
    ) = None,
    correction_item_update_service_for_request: (
        Callable[[], Any] | None
    ) = None,
    correction_actor_id_for_request: Callable[[], str] | None = None,
    correction_workspace_id_for_request: Callable[[], str] | None = None,
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
    """Create the optional Corrections transport.

    ``correction_item_update_service_for_request`` returns an adapter with an
    ``update(UpdateItemCommand) -> ItemCommandResult`` method.  Commands carry
    the public Corrections item id; the adapter may resolve that id to a
    capture-only/manual record or a promoted catalogue record before invoking
    a generic item command service.  The composition must install the policy
    appropriate to that backing record.  In particular, attempts to write
    server-managed metadata must raise ``ValidationError`` with code
    ``managed_item_fields_not_writable``.  Product-specific managed fields do
    not belong in this transport because book and capture stores can differ.

    ``correction_index_context_for_request`` may pin one composition-owned
    authority snapshot around the complete index projection.  This lets the
    item query and its dependent review/raster reads share the same bounded
    operation state without installing a process-global transport cache.
    """

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
        correction_index_context_for_request is not None
        and not callable(correction_index_context_for_request)
    ):
        raise TypeError(
            "correction_index_context_for_request must be callable or None"
        )
    if (
        correction_item_service_for_request is not None
        and not callable(correction_item_service_for_request)
    ):
        raise TypeError(
            "correction_item_service_for_request must be callable or None"
        )
    if (
        correction_item_update_service_for_request is not None
        and not callable(correction_item_update_service_for_request)
    ):
        raise TypeError(
            "correction_item_update_service_for_request must be callable "
            "or None"
        )
    if (
        correction_actor_id_for_request is not None
        and not callable(correction_actor_id_for_request)
    ):
        raise TypeError(
            "correction_actor_id_for_request must be callable or None"
        )
    if (
        correction_workspace_id_for_request is not None
        and not callable(correction_workspace_id_for_request)
    ):
        raise TypeError(
            "correction_workspace_id_for_request must be callable or None"
        )
    if (
        correction_transform_submitter is not None
        and not callable(correction_transform_submitter)
    ):
        raise TypeError(
            "correction_transform_submitter must be callable or None"
        )
    actor_id_for_request = (
        correction_actor_id_for_request
        if correction_actor_id_for_request is not None
        else lambda: "local-desktop"
    )
    workspace_id_for_request = (
        correction_workspace_id_for_request
        if correction_workspace_id_for_request is not None
        else lambda: "local-library"
    )

    blueprint = Blueprint("librarytool_corrections", __name__)

    @blueprint.get("/api/v1/corrections/items/<item_id>")
    def get_correction_item(item_id: str):
        try:
            return _get_correction_item(
                engine_for_request,
                correction_item_service_for_request,
                item_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.patch("/api/v1/corrections/items/<item_id>")
    def update_correction_item(item_id: str):
        try:
            return _update_correction_item(
                engine_for_request,
                correction_item_service_for_request,
                correction_item_update_service_for_request,
                item_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.get("/api/v1/corrections/index")
    def get_corrections_index():
        try:
            return _corrections_index(
                engine_for_request,
                workspace_id_for_request,
                correction_item_service_for_request,
                correction_index_context_for_request,
            )
        except EngineError as error:
            return _error_response(error)

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

    @blueprint.get(
        "/api/v1/items/<item_id>/ocr-proposals/<proposal_ref>"
    )
    def get_correction_ocr_proposal(item_id: str, proposal_ref: str):
        try:
            return _correction_ocr_proposal_detail(
                engine_for_request,
                item_id,
                proposal_ref,
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

    @blueprint.put("/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/caption")
    def set_manual_caption(item_id: str, artifact_id: str):
        try:
            return _set_manual_caption(
                engine_for_request,
                item_id,
                artifact_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.delete(
        "/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/caption"
    )
    def clear_manual_caption(item_id: str, artifact_id: str):
        try:
            return _clear_manual_caption(
                engine_for_request,
                item_id,
                artifact_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.patch(
        "/api/v1/items/<item_id>/raster-artifacts/<artifact_id>/metadata"
    )
    def assert_artifact_metadata(item_id: str, artifact_id: str):
        try:
            return _assert_artifact_metadata(
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

    @blueprint.get("/api/v1/items/<item_id>/corrections/review")
    def get_correction_review(item_id: str):
        try:
            return _review_detail(engine_for_request, item_id)
        except EngineError as error:
            return _error_response(error)

    @blueprint.get(
        "/api/v1/items/<item_id>/corrections/review/history"
    )
    def get_correction_review_history(item_id: str):
        try:
            return _review_history(engine_for_request, item_id)
        except EngineError as error:
            return _error_response(error)

    @blueprint.put("/api/v1/items/<item_id>/corrections/review/attention")
    def mark_attention(item_id: str):
        try:
            return _mark_attention(
                engine_for_request,
                actor_id_for_request,
                item_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.post("/api/v1/items/<item_id>/corrections/review/resolve")
    def resolve_corrections(item_id: str):
        try:
            return _resolve_corrections(
                engine_for_request,
                actor_id_for_request,
                item_id,
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.post("/api/v1/items/<item_id>/corrections/review/reopen")
    def reopen_corrections(item_id: str):
        try:
            return _reopen_corrections(
                engine_for_request,
                actor_id_for_request,
                item_id,
            )
        except EngineError as error:
            return _error_response(error)

    return blueprint


__all__ = [
    "ARTIFACT_PAGE_LIMIT",
    "CORRECTION_REVIEW_HISTORY_PAGE_LIMIT",
    "CORRECTION_REVIEW_HISTORY_TAIL_LIMIT",
    "CORRECTION_MUTATION_MAX_BYTES",
    "CORRECTION_ITEM_DETAIL_MAX_BYTES",
    "CORRECTION_ITEM_METADATA_FIELD_LIMIT",
    "CORRECTION_ITEM_METADATA_NODE_LIMIT",
    "CORRECTION_ITEM_METADATA_DEPTH_LIMIT",
    "CORRECTION_ITEM_SCHEMA",
    "CORRECTION_ITEM_MUTATION_SCHEMA",
    "CORRECTION_TRANSFORM_QUEUE_SCHEMA",
    "CORRECTIONS_INDEX_SCHEMA",
    "create_corrections_blueprint",
]
