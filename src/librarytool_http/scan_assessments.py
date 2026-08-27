"""Flask transport for source-reference keyed scan assessments.

The transport is deliberately injected with a request-scoped service rather
than composing filesystem storage.  This keeps the HTTP package usable by the
Desktop host without giving it authority over the host's mutable data root.
"""

from __future__ import annotations

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
from librarytool.engine.scan_assessments import (
    MAX_SCAN_ASSESSMENT_BYTES,
    ScanAssessmentDraft,
    ScanAssessmentIntegrityError,
    ScanAssessmentKey,
    ScanAssessmentProvenance,
    ScanAssessmentService,
    ScanAssessmentView,
)


# JSON escaping can expand otherwise valid text by as much as six times.  The
# decoded Markdown is independently capped by ScanAssessmentDraft.
SCAN_ASSESSMENT_MUTATION_MAX_BYTES = MAX_SCAN_ASSESSMENT_BYTES * 6 + 64 * 1024

_DRAFT_FIELDS = frozenset({"text", "provenance", "canonical_item_id", "capture_id"})
_PROVENANCE_FIELDS = frozenset(
    {
        "review_record_uuid",
        "source_database",
        "source_snapshot",
        "source_row_sha256",
    }
)
_SAFE_DETAIL_FIELDS = frozenset(
    {
        "artifact",
        "cause_type",
        "current_revision",
        "expected_revision",
        "field",
        "header",
        "headers",
        "maximum_bytes",
        "namespace",
        "result_revision",
        "source_id",
    }
)


def _safe_details(error: EngineError) -> dict[str, Any]:
    """Project only transport-safe diagnostics, never storage locators."""

    return {
        key: value
        for key, value in error.details.items()
        if key in _SAFE_DETAIL_FIELDS
        and (
            value is None
            or isinstance(value, (bool, int, float, str))
            or (
                isinstance(value, (list, tuple))
                and all(isinstance(item, str) for item in value)
            )
        )
    }


def _error_status(error: EngineError) -> int:
    if error.code == "scan_assessment_too_large":
        return 413
    if isinstance(error, NotFoundError):
        return 404
    if isinstance(error, PreconditionRequiredError):
        return 428
    if isinstance(error, ConflictError):
        if error.code in {
            "scan_assessment_exists",
            "scan_assessment_revision_conflict",
        }:
            return 412
        return 409
    if isinstance(error, ValidationError):
        return 400
    if isinstance(error, RepositoryError) and error.retryable:
        return 503
    if isinstance(error, RepositoryError):
        return 500
    if error.retryable and error.code.endswith("_unavailable"):
        return 503
    return 500


def _public_error_message(error: EngineError) -> str:
    if isinstance(error, ScanAssessmentIntegrityError):
        return "the stored scan assessment failed integrity validation"
    if isinstance(error, RepositoryError):
        return "scan-assessment storage is unavailable"
    return error.message


def _error_response(error: EngineError) -> tuple[Response, int]:
    body: dict[str, Any] = {
        "ok": False,
        "error": _public_error_message(error),
        "code": error.code,
        "retryable": error.retryable,
    }
    details = _safe_details(error)
    if details:
        body["details"] = details
    if isinstance(error, (ConflictError, PreconditionRequiredError)):
        body["conflict"] = error.code
    response = jsonify(body)
    _no_store(response)
    return response, _error_status(error)


def _no_store(response: Response) -> Response:
    response.cache_control.no_store = True
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _service(
    service_for_request: Callable[[], ScanAssessmentService],
) -> ScanAssessmentService:
    service = service_for_request()
    if not isinstance(service, ScanAssessmentService):
        raise EngineError(
            "the scan-assessment module is unavailable",
            code="scan_assessment_module_unavailable",
            retryable=True,
        )
    return service


def _key(namespace: str, source_id: str) -> ScanAssessmentKey:
    # Flask supplies decoded route values.  ScanAssessmentKey then applies the
    # portable-segment grammar and rejects percent escapes and path syntax.
    return ScanAssessmentKey(namespace, source_id)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _too_large() -> ValidationError:
    return ValidationError(
        "the scan-assessment mutation document is too large",
        code="scan_assessment_too_large",
        details={"maximum_bytes": SCAN_ASSESSMENT_MUTATION_MAX_BYTES},
    )


def _mutation_document() -> Mapping[str, Any]:
    length = request.content_length
    if length is not None and length > SCAN_ASSESSMENT_MUTATION_MAX_BYTES:
        raise _too_large()
    if request.mimetype != "application/json":
        raise ValidationError(
            "the scan-assessment mutation must use application/json",
            code="invalid_scan_assessment_document",
            details={"field": "content_type"},
        )
    charset = request.mimetype_params.get("charset", "utf-8").casefold()
    if charset not in {"utf-8", "utf8"} or request.content_encoding:
        raise ValidationError(
            "the scan-assessment mutation must use unencoded UTF-8 JSON",
            code="invalid_scan_assessment_document",
            details={"field": "content_type"},
        )
    encoded = request.stream.read(SCAN_ASSESSMENT_MUTATION_MAX_BYTES + 1)
    if len(encoded) > SCAN_ASSESSMENT_MUTATION_MAX_BYTES:
        raise _too_large()
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
            "the scan-assessment mutation document is invalid JSON",
            code="invalid_scan_assessment_document",
            details={"cause_type": type(error).__name__},
        ) from error
    if not isinstance(value, Mapping):
        raise ValidationError(
            "the scan-assessment mutation must be a JSON object",
            code="invalid_scan_assessment_document",
            details={"field": "body"},
        )
    fields = frozenset(value)
    if "text" not in fields or not fields <= _DRAFT_FIELDS:
        raise ValidationError(
            "the scan-assessment mutation fields do not match the schema",
            code="invalid_scan_assessment_envelope",
            details={"field": "body"},
        )
    return value


def _provenance(value: Any) -> ScanAssessmentProvenance:
    if not isinstance(value, Mapping) or not frozenset(value) <= _PROVENANCE_FIELDS:
        raise ValidationError(
            "provenance must be an object containing supported fields",
            code="invalid_scan_assessment_provenance",
            details={"field": "provenance"},
        )
    return ScanAssessmentProvenance(
        review_record_uuid=value.get("review_record_uuid", ""),
        source_database=value.get("source_database", ""),
        source_snapshot=value.get("source_snapshot", ""),
        source_row_sha256=value.get("source_row_sha256", ""),
    )


def _draft() -> ScanAssessmentDraft:
    value = _mutation_document()
    provenance = (
        _provenance(value["provenance"])
        if "provenance" in value
        else ScanAssessmentProvenance()
    )
    return ScanAssessmentDraft(
        text=value["text"],
        provenance=provenance,
        canonical_item_id=value.get("canonical_item_id", ""),
        capture_id=value.get("capture_id", ""),
    )


def _operation_id() -> str:
    operation_id = request.headers.get("Idempotency-Key")
    if operation_id is None or operation_id == "":
        raise PreconditionRequiredError(
            "Idempotency-Key is required",
            code="scan_assessment_idempotency_key_required",
            details={"header": "Idempotency-Key"},
        )
    return operation_id


def _strong_if_match() -> str:
    raw = request.headers.get("If-Match")
    if raw is None or raw == "":
        raise PreconditionRequiredError(
            "If-Match is required",
            code="scan_assessment_revision_required",
            details={"header": "If-Match"},
        )
    if (
        raw != raw.strip()
        or raw.startswith("W/")
        or len(raw) < 3
        or raw[0] != '"'
        or raw[-1] != '"'
        or '"' in raw[1:-1]
        or "," in raw
    ):
        raise ValidationError(
            "If-Match must contain one strong quoted scan-assessment revision",
            code="invalid_scan_assessment_revision",
            details={"header": "If-Match"},
        )
    return raw[1:-1]


def _put_precondition() -> tuple[str, str]:
    if_match = request.headers.get("If-Match")
    if_none_match = request.headers.get("If-None-Match")
    if if_match is not None and if_none_match is not None:
        raise ValidationError(
            "use either If-Match or If-None-Match, not both",
            code="invalid_scan_assessment_precondition",
            details={"headers": ["If-Match", "If-None-Match"]},
        )
    if if_none_match is not None:
        if if_none_match != "*":
            raise ValidationError(
                "If-None-Match must be * when creating a scan assessment",
                code="invalid_scan_assessment_precondition",
                details={"header": "If-None-Match"},
            )
        return "create", ""
    if if_match is not None:
        return "update", _strong_if_match()
    raise PreconditionRequiredError(
        "If-None-Match: * or If-Match is required",
        code="scan_assessment_precondition_required",
        details={"headers": ["If-None-Match", "If-Match"]},
    )


def _view_response(view: ScanAssessmentView, *, status: int = 200) -> Response:
    response = jsonify(
        {
            "ok": True,
            "schema": "librarytool.scan-assessment-view/1",
            "assessment": view.as_dict(),
        }
    )
    response.status_code = status
    response.set_etag(view.revision, weak=False)
    response.headers["X-Scan-Assessment-Revision"] = view.revision
    return _no_store(response)


def _require_current_source_binding(
    key: ScanAssessmentKey,
    source_sha256_for_request: Callable[[ScanAssessmentKey], str | None] | None,
    source_row_sha256: str,
    *,
    mutation: bool = False,
) -> None:
    """Require an exact source binding whenever the host can resolve one.

    A configured resolver means the host has an authoritative catalogue in
    scope.  In that mode an omitted digest is not an unbound convenience: it
    would let a later source change go undetected.  Keep resolver-less engine
    embeddings backwards compatible, while making the Desktop boundary fail
    closed for both new mutations and legacy unbound artifacts.
    """

    if source_sha256_for_request is None:
        return
    if not source_row_sha256:
        error_type = ValidationError if mutation else ConflictError
        raise error_type(
            "the scan assessment must be bound to the current source record",
            code="scan_assessment_source_binding_required",
            details=key.as_dict(),
        )
    current = source_sha256_for_request(key)
    if current != source_row_sha256:
        raise ConflictError(
            "the scan assessment is bound to an older source record",
            code="scan_assessment_source_conflict",
            details=key.as_dict(),
        )


def _require_current_alias_binding(
    key: ScanAssessmentKey,
    source_aliases_for_request: (
        Callable[[ScanAssessmentKey], Mapping[str, str] | None] | None
    ),
    *,
    canonical_item_id: str,
    capture_id: str,
) -> None:
    """Reject client-authored aliases that do not match active authority."""

    if source_aliases_for_request is None:
        return
    expected = source_aliases_for_request(key)
    if expected is None:
        raise ConflictError(
            "the scan-assessment source no longer exists",
            code="scan_assessment_source_conflict",
            details=key.as_dict(),
        )
    if (
        not isinstance(expected, Mapping)
        or frozenset(expected) != {"canonical_item_id", "capture_id"}
        or not all(isinstance(value, str) for value in expected.values())
    ):
        raise RepositoryError(
            "scan-assessment source authority is unavailable",
            code="scan_assessment_source_authority_unavailable",
            retryable=True,
            details=key.as_dict(),
        )
    if (
        canonical_item_id != expected["canonical_item_id"]
        or capture_id != expected["capture_id"]
    ):
        raise ConflictError(
            "the scan-assessment aliases do not match active source authority",
            code="scan_assessment_alias_conflict",
            details=key.as_dict(),
        )


def create_scan_assessment_blueprint(
    service_for_request: Callable[[], ScanAssessmentService],
    *,
    source_sha256_for_request: (
        Callable[[ScanAssessmentKey], str | None] | None
    ) = None,
    source_aliases_for_request: (
        Callable[[ScanAssessmentKey], Mapping[str, str] | None] | None
    ) = None,
) -> Blueprint:
    """Create scan-assessment routes around a request-scoped service."""

    if not callable(service_for_request):
        raise TypeError("service_for_request must be callable")
    if source_sha256_for_request is not None and not callable(
        source_sha256_for_request
    ):
        raise TypeError("source_sha256_for_request must be callable")
    if source_aliases_for_request is not None and not callable(
        source_aliases_for_request
    ):
        raise TypeError("source_aliases_for_request must be callable")
    blueprint = Blueprint("librarytool_scan_assessments_v1", __name__)

    @blueprint.get("/api/v1/scan-assessments/<namespace>/<source_id>")
    def get_scan_assessment(namespace: str, source_id: str):
        try:
            key = _key(namespace, source_id)
            view = _service(service_for_request).get(key)
            _require_current_source_binding(
                key,
                source_sha256_for_request,
                view.manifest.provenance.source_row_sha256,
            )
            _require_current_alias_binding(
                key,
                source_aliases_for_request,
                canonical_item_id=view.manifest.canonical_item_id,
                capture_id=view.manifest.capture_id,
            )
        except EngineError as error:
            return _error_response(error)
        response = _view_response(view)
        return response.make_conditional(request)

    @blueprint.put("/api/v1/scan-assessments/<namespace>/<source_id>")
    def put_scan_assessment(namespace: str, source_id: str):
        try:
            key = _key(namespace, source_id)
            action, expected_revision = _put_precondition()
            operation_id = _operation_id()
            draft = _draft()
            _require_current_source_binding(
                key,
                source_sha256_for_request,
                draft.provenance.source_row_sha256,
                mutation=True,
            )
            _require_current_alias_binding(
                key,
                source_aliases_for_request,
                canonical_item_id=draft.canonical_item_id,
                capture_id=draft.capture_id,
            )
            service = _service(service_for_request)
            if action == "create":
                return _view_response(
                    service.create(key, draft, operation_id),
                    status=201,
                )
            return _view_response(
                service.update(
                    key,
                    draft,
                    expected_revision,
                    operation_id,
                )
            )
        except EngineError as error:
            return _error_response(error)

    @blueprint.delete("/api/v1/scan-assessments/<namespace>/<source_id>")
    def delete_scan_assessment(namespace: str, source_id: str):
        try:
            key = _key(namespace, source_id)
            expected_revision = _strong_if_match()
            operation_id = _operation_id()
            deleted_revision = _service(service_for_request).delete(
                key,
                expected_revision,
                operation_id,
            )
        except EngineError as error:
            return _error_response(error)
        response = jsonify(
            {
                "ok": True,
                "schema": "librarytool.scan-assessment-deleted/1",
                "key": key.as_dict(),
                "deleted_revision": deleted_revision,
            }
        )
        response.headers["X-Scan-Assessment-Revision"] = deleted_revision
        return _no_store(response)

    return blueprint


__all__ = [
    "SCAN_ASSESSMENT_MUTATION_MAX_BYTES",
    "create_scan_assessment_blueprint",
]
