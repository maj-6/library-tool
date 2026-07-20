from __future__ import annotations

from fastapi.testclient import TestClient

from whl_image_processor import api
from whl_image_processor.settings import Settings
from whl_image_processor.worker import BatchSummary


def _settings(*, admin_token: str) -> Settings:
    return Settings(
        supabase_url="https://project.supabase.co",
        supabase_secret_key="sb_secret_processor_test",
        admin_token=admin_token,
    )


def test_drain_requires_exact_admin_token(monkeypatch):
    calls: list[int] = []

    def fake_run_batch(_settings: Settings, limit: int) -> BatchSummary:
        calls.append(limit)
        return BatchSummary(claimed=1, completed=1, reconciled_captures=2)

    monkeypatch.setattr(api, "run_batch", fake_run_batch)
    token = "processor-test-token-that-is-long-enough"
    with TestClient(api.create_app(_settings(admin_token=token))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/v1/drain", json={"limit": 7}).status_code == 401
        assert (
            client.post(
                "/v1/drain",
                headers={"X-Image-Processor-Token": f"{token}-wrong"},
                json={"limit": 7},
            ).status_code
            == 401
        )
        response = client.post(
            "/v1/drain",
            headers={"X-Image-Processor-Token": token},
            json={"limit": 7},
        )

    assert response.status_code == 200
    assert response.json() == {
        "claimed": 1,
        "completed": 1,
        "retrying": 0,
        "failed": 0,
        "recovered_leases": 0,
        "reconciled_captures": 2,
        "lost_leases": 0,
    }
    assert calls == [7]


def test_drain_is_disabled_without_admin_token(monkeypatch):
    def unexpected_run_batch(*_args: object, **_kwargs: object) -> BatchSummary:
        raise AssertionError("disabled drain API must not run a batch")

    monkeypatch.setattr(api, "run_batch", unexpected_run_batch)
    with TestClient(api.create_app(_settings(admin_token=""))) as client:
        response = client.post("/v1/drain", json={"limit": 1})

    assert response.status_code == 503
    assert response.json() == {"detail": "drain API is disabled"}
