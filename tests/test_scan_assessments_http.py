from __future__ import annotations

import json
from typing import Any

import pytest
from flask import Flask

from librarytool.adapters.filesystem import (
    FilesystemScanAssessmentRepository,
    RecoverableWriteSet,
)
from librarytool.engine import (
    MAX_SCAN_ASSESSMENT_BYTES,
    RepositoryError,
    ScanAssessmentDraft,
    ScanAssessmentIntegrityError,
    ScanAssessmentKey,
    ScanAssessmentService,
)
from librarytool_http import create_scan_assessment_blueprint
from librarytool_http.scan_assessments import (
    SCAN_ASSESSMENT_MUTATION_MAX_BYTES,
)


NAMESPACE = "manual_entries"
SOURCE_ID = "a1111111-1111-4111-8111-111111111111"
URL = f"/api/v1/scan-assessments/{NAMESPACE}/{SOURCE_ID}"
REVIEW_UUID = "b2222222-2222-4222-8222-222222222222"
ROW_SHA256 = "3" * 64
PRIVATE_PATH = r"C:\private\scan-assessments\manifest.json"


def _body(text: str = "# Scan assessment\n\nPreserve the plates.\n") -> dict[str, Any]:
    return {
        "text": text,
        "provenance": {
            "review_record_uuid": REVIEW_UUID,
            "source_database": "book-review.sqlite3",
            "source_snapshot": "reviewed-export-2026-08-26",
            "source_row_sha256": ROW_SHA256,
        },
        "canonical_item_id": "book:canonical-17",
        "capture_id": "capture:17",
    }


def _app(
    service_for_request,
    *,
    source_sha256_for_request=None,
    source_aliases_for_request=None,
) -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(
        create_scan_assessment_blueprint(
            service_for_request,
            source_sha256_for_request=source_sha256_for_request,
            source_aliases_for_request=source_aliases_for_request,
        )
    )
    return app


@pytest.fixture()
def scan_http(tmp_path):
    repository = FilesystemScanAssessmentRepository(RecoverableWriteSet(tmp_path))
    service = ScanAssessmentService(repository)
    with _app(lambda: service).test_client() as client:
        yield client, service


def _create(client, *, body: dict[str, Any] | None = None, operation="create-1"):
    response = client.put(
        URL,
        json=_body() if body is None else body,
        headers={
            "If-None-Match": "*",
            "Idempotency-Key": operation,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response


def test_create_get_and_conditional_get_use_a_strong_private_etag(scan_http):
    client, _service = scan_http

    created = _create(client)
    body = created.get_json()

    assert body["ok"] is True
    assert body["schema"] == "librarytool.scan-assessment-view/1"
    assert body["assessment"]["text"].startswith("# Scan assessment")
    manifest = body["assessment"]["manifest"]
    assert manifest["schema"] == "librarytool.scan-assessment/1"
    assert manifest["namespace"] == NAMESPACE
    assert manifest["source_id"] == SOURCE_ID
    assert manifest["provenance"]["review_record_uuid"] == REVIEW_UUID
    assert manifest["canonical_item_id"] == "book:canonical-17"
    assert created.headers["ETag"] == f'"{manifest["revision"]}"'
    assert created.get_etag() == (manifest["revision"], False)
    assert created.headers["X-Scan-Assessment-Revision"] == manifest["revision"]
    assert created.cache_control.no_store is True
    assert created.headers["X-Content-Type-Options"] == "nosniff"

    fetched = client.get(URL)
    assert fetched.status_code == 200
    assert fetched.get_json() == body
    assert fetched.headers["ETag"] == created.headers["ETag"]
    assert fetched.cache_control.no_store is True

    unchanged = client.get(
        URL,
        headers={"If-None-Match": fetched.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""
    assert unchanged.headers["ETag"] == fetched.headers["ETag"]
    assert unchanged.cache_control.no_store is True
    assert PRIVATE_PATH not in created.get_data(as_text=True)


def test_optional_source_resolver_rejects_stale_bound_reasoning(tmp_path):
    service = ScanAssessmentService(
        FilesystemScanAssessmentRepository(RecoverableWriteSet(tmp_path))
    )
    current = {"sha256": ROW_SHA256}
    app = _app(
        lambda: service,
        source_sha256_for_request=lambda _key: current["sha256"],
    )
    with app.test_client() as client:
        assert _create(client).status_code == 201
        assert client.get(URL).status_code == 200
        current["sha256"] = "4" * 64
        stale = client.get(URL)
        rejected_write = client.put(
            URL,
            json=_body("Different reasoning."),
            headers={
                "If-Match": '"' + "a" * 64 + '"',
                "Idempotency-Key": "stale-source-write",
            },
        )

    assert stale.status_code == 409
    assert stale.get_json()["code"] == "scan_assessment_source_conflict"
    assert rejected_write.status_code == 409
    assert rejected_write.get_json()["code"] == "scan_assessment_source_conflict"


def test_configured_source_resolver_rejects_missing_binding(tmp_path):
    service = ScanAssessmentService(
        FilesystemScanAssessmentRepository(RecoverableWriteSet(tmp_path))
    )
    app = _app(
        lambda: service,
        source_sha256_for_request=lambda _key: ROW_SHA256,
    )
    with app.test_client() as client:
        rejected = client.put(
            URL,
            json={"text": "Unbound reasoning."},
            headers={
                "If-None-Match": "*",
                "Idempotency-Key": "unbound-create",
            },
        )

    assert rejected.status_code == 400
    assert rejected.get_json()["code"] == ("scan_assessment_source_binding_required")


def test_configured_alias_resolver_rejects_spoofed_or_stale_aliases(tmp_path):
    service = ScanAssessmentService(
        FilesystemScanAssessmentRepository(RecoverableWriteSet(tmp_path))
    )
    aliases = {
        "canonical_item_id": "book:canonical-17",
        "capture_id": "capture:17",
    }
    app = _app(
        lambda: service,
        source_sha256_for_request=lambda _key: ROW_SHA256,
        source_aliases_for_request=lambda _key: dict(aliases),
    )
    wrong = _body()
    wrong["canonical_item_id"] = "book:spoofed"
    with app.test_client() as client:
        rejected = client.put(
            URL,
            json=wrong,
            headers={
                "If-None-Match": "*",
                "Idempotency-Key": "spoofed-alias",
            },
        )
        created = _create(client, operation="bound-alias")
        aliases["canonical_item_id"] = "book:replacement"
        stale = client.get(URL)

    assert rejected.status_code == 409
    assert rejected.get_json()["code"] == "scan_assessment_alias_conflict"
    assert created.status_code == 201
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "scan_assessment_alias_conflict"


def test_put_distinguishes_create_update_and_idempotency_conflicts(scan_http):
    client, _service = scan_http
    created = _create(client)

    updated = client.put(
        URL,
        json={"text": "Updated reasoning."},
        headers={
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "update-1",
        },
    )
    assert updated.status_code == 200
    assert updated.get_json()["assessment"]["text"] == "Updated reasoning."
    assert updated.headers["ETag"] != created.headers["ETag"]
    assert updated.get_json()["assessment"]["manifest"]["provenance"] == {
        "review_record_uuid": "",
        "source_database": "",
        "source_snapshot": "",
        "source_row_sha256": "",
    }

    replay = client.put(
        URL,
        json={"text": "Updated reasoning."},
        headers={
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "update-1",
        },
    )
    assert replay.status_code == 200
    assert replay.headers["ETag"] == updated.headers["ETag"]

    stale = client.put(
        URL,
        json={"text": "Stale reasoning."},
        headers={
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "update-stale",
        },
    )
    assert stale.status_code == 412
    assert stale.get_json()["code"] == "scan_assessment_revision_conflict"
    assert stale.get_json()["conflict"] == "scan_assessment_revision_conflict"
    assert (
        stale.get_json()["details"]["current_revision"]
        == (updated.get_json()["assessment"]["manifest"]["revision"])
    )

    reused = client.put(
        URL,
        json={"text": "A different request."},
        headers={
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "update-1",
        },
    )
    assert reused.status_code == 409
    assert reused.get_json()["code"] == ("scan_assessment_operation_id_conflict")

    create_again = client.put(
        URL,
        json={"text": "Another create."},
        headers={
            "If-None-Match": "*",
            "Idempotency-Key": "create-2",
        },
    )
    assert create_again.status_code == 412
    assert create_again.get_json()["code"] == "scan_assessment_exists"


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        (
            {"Idempotency-Key": "missing-precondition"},
            428,
            "scan_assessment_precondition_required",
        ),
        (
            {"If-None-Match": "*"},
            428,
            "scan_assessment_idempotency_key_required",
        ),
        (
            {
                "If-Match": 'W/"sa-' + "0" * 64 + '"',
                "Idempotency-Key": "weak-revision",
            },
            400,
            "invalid_scan_assessment_revision",
        ),
        (
            {
                "If-Match": '"sa-' + "0" * 64 + '"',
                "If-None-Match": "*",
                "Idempotency-Key": "contradictory",
            },
            400,
            "invalid_scan_assessment_precondition",
        ),
        (
            {
                "If-None-Match": '"anything"',
                "Idempotency-Key": "wrong-create-tag",
            },
            400,
            "invalid_scan_assessment_precondition",
        ),
    ],
)
def test_put_requires_explicit_strong_preconditions_and_idempotency(
    scan_http,
    headers,
    status,
    code,
):
    client, _service = scan_http
    response = client.put(URL, json={"text": "Reasoning."}, headers=headers)
    assert response.status_code == status
    assert response.get_json()["code"] == code
    assert response.cache_control.no_store is True


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (
            "/api/v1/scan-assessments/manual_entries/%2e%2e",
            "invalid_scan_assessment_identity",
        ),
        (
            "/api/v1/scan-assessments/manual_entries/%252e%252e",
            "invalid_scan_assessment_identity",
        ),
        (
            "/api/v1/scan-assessments/manual_entries/C:escape",
            "invalid_scan_assessment_identity",
        ),
    ],
)
def test_decoded_source_segments_are_validated(scan_http, path, code):
    client, _service = scan_http
    response = client.get(path)
    assert response.status_code == 400
    assert response.get_json()["code"] == code


def test_mutation_document_is_strict_and_bounded(scan_http):
    client, _service = scan_http
    headers = {
        "If-None-Match": "*",
        "Idempotency-Key": "invalid-body",
        "Content-Type": "application/json",
    }

    duplicate = client.put(
        URL,
        data=b'{"text":"first","text":"second"}',
        headers=headers,
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["code"] == "invalid_scan_assessment_document"

    null_provenance = client.put(
        URL,
        json={"text": "Reasoning.", "provenance": None},
        headers={
            "If-None-Match": "*",
            "Idempotency-Key": "null-provenance",
        },
    )
    assert null_provenance.status_code == 400
    assert null_provenance.get_json()["code"] == ("invalid_scan_assessment_provenance")

    unknown = client.put(
        URL,
        json={"text": "Reasoning.", "path": PRIVATE_PATH},
        headers={
            "If-None-Match": "*",
            "Idempotency-Key": "unknown-body-field",
        },
    )
    assert unknown.status_code == 400
    assert unknown.get_json()["code"] == "invalid_scan_assessment_envelope"
    assert PRIVATE_PATH not in unknown.get_data(as_text=True)

    oversized_text = client.put(
        URL,
        json={"text": "x" * (MAX_SCAN_ASSESSMENT_BYTES + 1)},
        headers={
            "If-None-Match": "*",
            "Idempotency-Key": "oversized-text",
        },
    )
    assert oversized_text.status_code == 413
    assert oversized_text.get_json()["code"] == "scan_assessment_too_large"

    oversized_entity = client.put(
        URL,
        data=b"x" * (SCAN_ASSESSMENT_MUTATION_MAX_BYTES + 1),
        headers=headers,
    )
    assert oversized_entity.status_code == 413
    assert oversized_entity.get_json()["code"] == "scan_assessment_too_large"


def test_optional_provenance_fields_validate_without_accepting_private_paths(
    scan_http,
):
    client, _service = scan_http
    partial = _create(
        client,
        body={
            "text": "Reasoning.",
            "provenance": {"source_snapshot": "reviewed-export"},
        },
        operation="partial-provenance",
    )
    provenance = partial.get_json()["assessment"]["manifest"]["provenance"]
    assert provenance["source_snapshot"] == "reviewed-export"
    assert provenance["review_record_uuid"] == ""

    second_url = "/api/v1/scan-assessments/manual_entries/source-2"
    rejected = client.put(
        second_url,
        json={
            "text": "Reasoning.",
            "provenance": {"source_database": PRIVATE_PATH},
        },
        headers={
            "If-None-Match": "*",
            "Idempotency-Key": "private-provenance",
        },
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["code"] == "private_scan_assessment_locator"
    assert PRIVATE_PATH not in rejected.get_data(as_text=True)


def test_missing_delete_and_integrity_failures_are_clear_and_path_free(scan_http):
    client, _service = scan_http
    missing = client.get(URL)
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "scan_assessment_not_found"

    created = _create(client)
    no_revision = client.delete(
        URL,
        headers={"Idempotency-Key": "delete-no-revision"},
    )
    assert no_revision.status_code == 428
    assert no_revision.get_json()["code"] == "scan_assessment_revision_required"

    deleted = client.delete(
        URL,
        headers={
            "If-Match": created.headers["ETag"],
            "Idempotency-Key": "delete-1",
        },
    )
    assert deleted.status_code == 200
    assert (
        deleted.get_json()["deleted_revision"]
        == created.get_json()["assessment"]["manifest"]["revision"]
    )
    assert deleted.cache_control.no_store is True
    assert client.get(URL).status_code == 404

    class _IntegrityRepository:
        def read(self, _key: ScanAssessmentKey):
            raise ScanAssessmentIntegrityError(
                f"corrupt stored file at {PRIVATE_PATH}",
                code="scan_assessment_hash_mismatch",
                details={"path": PRIVATE_PATH, "artifact": "assessment"},
            )

        def create(self, key, draft, operation_id):
            raise AssertionError

        def update(self, key, draft, expected_revision, operation_id):
            raise AssertionError

        def delete(self, key, expected_revision, operation_id):
            raise AssertionError

    integrity_service = ScanAssessmentService(_IntegrityRepository())
    with _app(lambda: integrity_service).test_client() as integrity_client:
        corrupt = integrity_client.get(URL)
    assert corrupt.status_code == 500
    assert corrupt.get_json()["code"] == "scan_assessment_hash_mismatch"
    assert corrupt.get_json()["details"] == {"artifact": "assessment"}
    assert PRIVATE_PATH not in corrupt.get_data(as_text=True)


def test_retryable_repository_and_missing_service_fail_closed_without_paths():
    class _UnavailableRepository:
        def read(self, _key: ScanAssessmentKey):
            raise RepositoryError(
                f"cannot open {PRIVATE_PATH}",
                code="scan_assessment_read_failed",
                details={"path": PRIVATE_PATH},
                retryable=True,
            )

        def create(self, key, draft, operation_id):
            raise AssertionError

        def update(self, key, draft, expected_revision, operation_id):
            raise AssertionError

        def delete(self, key, expected_revision, operation_id):
            raise AssertionError

    service = ScanAssessmentService(_UnavailableRepository())
    with _app(lambda: service).test_client() as client:
        unavailable = client.get(URL)
    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"] == ("scan-assessment storage is unavailable")
    assert PRIVATE_PATH not in unavailable.get_data(as_text=True)

    with _app(lambda: None).test_client() as client:
        missing = client.get(URL)
    assert missing.status_code == 503
    assert missing.get_json()["code"] == "scan_assessment_module_unavailable"


def test_factory_requires_a_request_scoped_service_callback():
    with pytest.raises(TypeError, match="service_for_request must be callable"):
        create_scan_assessment_blueprint(None)  # type: ignore[arg-type]


def test_draft_shape_matches_the_engine_contract():
    draft = ScanAssessmentDraft(text="Reasoning.")
    assert set(draft.as_dict()) == {
        "text",
        "provenance",
        "canonical_item_id",
        "capture_id",
    }
    assert json.loads(json.dumps(draft.as_dict())) == draft.as_dict()
