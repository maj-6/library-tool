from __future__ import annotations

import httpx
import pytest

from whl_image_processor.settings import Settings
from whl_image_processor.models import JobRecord
from whl_image_processor.store import LeaseLostError, PermanentStoreError, SupabaseJobStore

from conftest import request_document


def _settings(secret: str = "sb_secret_processor_test") -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key=secret,
    )


def _job_row(state: str = "queued", attempt_count: int = 0) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "capture_id": "00000000-0000-0000-0000-000000000002",
        "owner_id": "00000000-0000-0000-0000-000000000003",
        "asset_id": "asset-1",
        "request_id": "request-1",
        "request_revision": 1,
        "source_path": "device/capture/photo_1.jpg",
        "source_sha256": "a" * 64,
        "state": state,
        "attempt_count": attempt_count,
        "request": request_document(),
    }


def test_modern_secret_uses_apikey_without_invalid_bearer_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings(), client) as store:
        assert store.claim_next() is None
    assert seen[0].headers["apikey"] == "sb_secret_processor_test"
    assert "authorization" not in seen[0].headers


def test_legacy_service_role_is_also_sent_as_bearer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings("legacy.jwt.key"), client) as store:
        assert store.claim_next() is None
    assert seen[0].headers["authorization"] == "Bearer legacy.jwt.key"


def test_claim_uses_state_and_attempt_compare_and_swap():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[_job_row()])
        assert request.method == "PATCH"
        claimed = _job_row("running", 1)
        return httpx.Response(200, json=[claimed])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings(), client) as store:
        job = store.claim_next()
    assert job is not None and job.state == "running" and job.attempt_count == 1
    query = requests[1].url.params
    assert query["state"] == "eq.queued"
    assert query["attempt_count"] == "eq.0"


def test_source_download_enforces_declared_size_limit():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "100"}, content=b"x")

    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="sb_secret_test",
        max_source_bytes=20,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(settings, client) as store:
        with pytest.raises(PermanentStoreError, match="MAX_SOURCE_BYTES"):
            store.download_source("device/capture/photo_1.jpg")


def test_source_download_stream_enforces_limit_without_content_length():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 21)

    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="sb_secret_test",
        max_source_bytes=20,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(settings, client) as store:
        with pytest.raises(PermanentStoreError, match="MAX_SOURCE_BYTES"):
            store.download_source("device/capture/photo_1.jpg")


def test_streaming_source_404_remains_a_permanent_store_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b'{"message":"not found"}')

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings(), client) as store:
        with pytest.raises(PermanentStoreError, match="HTTP 404"):
            store.download_source("device/capture/missing.jpg")


@pytest.mark.parametrize(
    "path",
    ["../victim/photo.jpg", "device/../victim.jpg", "/device/capture/photo.jpg", "a//b"],
)
def test_source_download_rejects_noncanonical_paths_before_request(path: str):
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("a noncanonical object path must never reach the privileged client")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings(), client) as store:
        with pytest.raises(PermanentStoreError, match="canonical"):
            store.download_source(path)


@pytest.mark.parametrize("method", ["completed", "failed"])
def test_terminal_write_compares_attempt_count(method: str):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        state = "completed" if method == "completed" else "failed"
        return httpx.Response(200, json=[{**_job_row(state, 3)}])

    job = JobRecord.model_validate(_job_row("running", 3))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings(), client) as store:
        if method == "completed":
            store.mark_completed(job, {"schema": "test"})
        else:
            assert store.mark_failed(job, "bad image", retryable=False) == "failed"

    assert seen[0].url.params["state"] == "eq.running"
    assert seen[0].url.params["attempt_count"] == "eq.3"


def test_stale_terminal_write_is_rejected_after_newer_attempt_claims_job():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    job = JobRecord.model_validate(_job_row("running", 2))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with SupabaseJobStore(_settings(), client) as store:
        with pytest.raises(LeaseLostError):
            store.mark_completed(job, {"schema": "test"})
