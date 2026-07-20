from __future__ import annotations

import pytest
from pydantic import ValidationError

from whl_image_processor.models import JobRecord, ProcessingRequest


def test_exact_android_v1_request_is_accepted(valid_request):
    parsed = ProcessingRequest.model_validate(valid_request)
    assert parsed.source.role == "title_page"
    assert [operation.outcome for operation in parsed.operations] == [
        "page_dewarp",
        "detected_margin_crop",
        "contrast_normalization",
    ]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("profile", "contrast_strength_percent"), 71),
        (("profile", "resolved_treatment"), "older"),
        (("source", "original_sha256"), "not-a-hash"),
        (("result",), {"status": "done"}),
    ],
)
def test_tampered_or_result_populated_v1_request_is_rejected(valid_request, path, value):
    target = valid_request
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        ProcessingRequest.model_validate(valid_request)


def test_unknown_fields_and_role_recipe_mismatch_are_rejected(valid_request):
    valid_request["future_field"] = True
    with pytest.raises(ValidationError):
        ProcessingRequest.model_validate(valid_request)

    valid_request.pop("future_field")
    valid_request["operations"].pop()
    with pytest.raises(ValidationError):
        ProcessingRequest.model_validate(valid_request)


def test_job_snapshot_must_bind_request_identity_and_checksum(valid_request):
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "capture_id": "00000000-0000-0000-0000-000000000002",
        "owner_id": "00000000-0000-0000-0000-000000000003",
        "asset_id": "asset-1",
        "request_id": "request-1",
        "request_revision": 1,
        "source_path": "device/capture/photo_1.jpg",
        "source_sha256": "a" * 64,
        "state": "running",
        "attempt_count": 1,
        "request": valid_request,
    }
    assert JobRecord.model_validate(row).parsed_request().source.asset_id == "asset-1"
    row["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="checksum"):
        JobRecord.model_validate(row).parsed_request()
