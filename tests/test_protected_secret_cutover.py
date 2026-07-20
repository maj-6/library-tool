from __future__ import annotations

from contextlib import contextmanager

import pytest

import server
from librarytool.adapters.windows.secret_store import (
    ProtectedEnvelopeSecretStoreRepository,
    SecretStoreUnavailableError,
)
from librarytool.engine.secret_store import SecretStoreService


class _MemoryTransaction:
    def __init__(self, owner):
        self.owner = owner

    def read(self):
        return self.owner.blob

    def replace(self, protected):
        self.owner.blob = bytes(protected)


class _MemoryStore:
    def __init__(self):
        self.blob = None

    @contextmanager
    def transaction(self):
        yield _MemoryTransaction(self)


class _TestProtector:
    prefix = b"test-protected:\x00"

    def ensure_available(self):
        return None

    def protect(self, plaintext):
        return self.prefix + bytes(plaintext)[::-1]

    def unprotect(self, protected):
        if not bytes(protected).startswith(self.prefix):
            raise SecretStoreUnavailableError("unavailable")
        return bytes(protected)[len(self.prefix):][::-1]


class _UnavailableProtector(_TestProtector):
    def ensure_available(self):
        raise SecretStoreUnavailableError("unavailable")


def _repository(storage=None, protector=None):
    return ProtectedEnvelopeSecretStoreRepository(
        storage=storage or _MemoryStore(),
        protector=protector or _TestProtector(),
        registry=server._SECRET_REGISTRY,
        store_id=server._SECRET_STORE_ID,
    )


def _reset_legacy_sources():
    server._SECRETS_PATH.unlink(missing_ok=True)
    server._SECRET_SYNC_STATE_PATH.unlink(missing_ok=True)
    server.lib.CLIENT_STATE_PATH.unlink(missing_ok=True)


def test_cutover_commits_reopens_verifies_then_sanitizes_both_sources():
    _reset_legacy_sources()
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {
        "settings": {"theme": "dark", "aiKey": "client-ai",
                     "mistralKey": "client-mistral"},
    })
    server.lib.save_json(server._SECRETS_PATH, {
        "aiKey": "legacy-file-wins",
        server._MISTRAL_PENDING: True,
    })
    storage = _MemoryStore()
    repo = _repository(storage)

    reopened = server._migrate_legacy_plaintext_secrets(
        repo, reopen_repository=lambda: _repository(storage))

    with reopened.credential_leases.lease(
            server._SECRET_IDS["aiKey"]) as leased:
        assert leased.reveal() == "legacy-file-wins"
    with reopened.credential_leases.lease(
            server._SECRET_IDS["mistralKey"]) as leased:
        assert leased.reveal() == "client-mistral"
    assert server.lib.load_json(server.lib.CLIENT_STATE_PATH, {}) == {
        "settings": {"theme": "dark"},
    }
    assert not server._SECRETS_PATH.exists()
    assert server.lib.load_json(server._SECRET_SYNC_STATE_PATH, {}) == {
        "mistral_pending": True,
    }
    assert b"legacy-file-wins" not in storage.blob
    _reset_legacy_sources()


def test_cutover_reopen_failure_preserves_plaintext_and_restart_finishes():
    _reset_legacy_sources()
    server.lib.save_json(server._SECRETS_PATH, {"aiKey": "keep-until-verified"})
    storage = _MemoryStore()
    repo = _repository(storage)

    with pytest.raises(server.ProtectedSecretCutoverError):
        server._migrate_legacy_plaintext_secrets(
            repo,
            reopen_repository=lambda: (_ for _ in ()).throw(
                OSError("reopen failed")),
        )
    assert server.lib.load_json(server._SECRETS_PATH, {}) == {
        "aiKey": "keep-until-verified",
    }

    reopened = server._migrate_legacy_plaintext_secrets(
        _repository(storage), reopen_repository=lambda: _repository(storage))
    with reopened.credential_leases.lease(
            server._SECRET_IDS["aiKey"]) as leased:
        assert leased.reveal() == "keep-until-verified"
    assert not server._SECRETS_PATH.exists()
    _reset_legacy_sources()


def test_cutover_unavailable_protection_keeps_every_plaintext_source():
    _reset_legacy_sources()
    server.lib.save_json(server.lib.CLIENT_STATE_PATH, {
        "settings": {"aiKey": "client-value"},
    })
    server.lib.save_json(server._SECRETS_PATH, {
        "mistralKey": "file-value",
    })

    with pytest.raises(server.ProtectedSecretCutoverError):
        server._migrate_legacy_plaintext_secrets(
            _repository(protector=_UnavailableProtector()))

    assert server.lib.load_json(server.lib.CLIENT_STATE_PATH, {})[
        "settings"]["aiKey"] == "client-value"
    assert server.lib.load_json(server._SECRETS_PATH, {})[
        "mistralKey"] == "file-value"
    _reset_legacy_sources()


@pytest.fixture()
def protected_http(client, monkeypatch):
    repo = _repository()
    service = SecretStoreService(repo)
    monkeypatch.setattr(server, "_secret_service", lambda: service)
    monkeypatch.setattr(server, "_secret_health", repo.health.get_health)
    return client, repo


def test_versioned_secret_routes_are_masked_cas_and_replay_safe(protected_http):
    client, _repo = protected_http
    listed = client.get("/api/v1/secrets")
    assert listed.status_code == 200
    document = listed.get_json()
    assert document["schema"] == "librarytool.secret-status-list/1"
    assert document["health"] == {
        "available": True, "state": "ready", "writable": True,
    }
    status = next(row for row in document["secrets"]
                  if row["id"] == server._SECRET_IDS["aiKey"])
    assert status["configured"] is False
    assert status["masked_hint"] == ""

    url = "/api/v1/secrets/" + status["id"]
    headers = {
        "If-Match": f'"{status["revision"]}"',
        "Idempotency-Key": "replace-ai-1",
    }
    first = client.put(url, json={"credential": "never-return-this"},
                       headers=headers)
    assert first.status_code == 200
    body = first.get_json()
    assert body["replayed"] is False
    assert body["receipt"]["after"]["configured"] is True
    assert body["receipt"]["after"]["masked_hint"] == "••••"
    assert "never-return-this" not in first.get_data(as_text=True)

    replay = client.put(url, json={"credential": "never-return-this"},
                        headers=headers)
    assert replay.status_code == 200
    assert replay.get_json()["replayed"] is True

    conflict = client.put(
        url, json={"credential": "different"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "operation_id_conflict"
    assert "different" not in conflict.get_data(as_text=True)

    after = body["receipt"]["after"]
    cleared = client.delete(url, headers={
        "If-Match": f'"{after["revision"]}"',
        "Idempotency-Key": "clear-ai-1",
    })
    assert cleared.status_code == 200
    assert cleared.get_json()["receipt"]["after"]["configured"] is False


def test_secret_replacement_rejects_ambiguous_or_unbounded_json(protected_http):
    client, _repo = protected_http
    secret_id = server._SECRET_IDS["aiKey"]
    status = server._secret_service().get_status(secret_id)
    headers = {
        "If-Match": f'"{status.revision}"',
        "Idempotency-Key": "replace-ai-invalid",
    }
    url = "/api/v1/secrets/" + secret_id

    duplicate = client.put(
        url,
        data=b'{"credential":"first","credential":"second"}',
        content_type="application/json",
        headers=headers,
    )
    assert duplicate.status_code == 400
    assert duplicate.get_json()["code"] == "invalid_secret_mutation_document"
    assert "first" not in duplicate.get_data(as_text=True)
    assert "second" not in duplicate.get_data(as_text=True)

    wrong_type = client.put(
        url,
        data=b'{"credential":"value"}',
        content_type="text/plain",
        headers=headers,
    )
    assert wrong_type.status_code == 400
    assert wrong_type.get_json()["code"] == "invalid_secret_mutation_document"

    oversized = client.put(
        url,
        data=b"x" * (server._SECRET_MUTATION_MAX_BYTES + 1),
        content_type="application/json",
        headers=headers,
    )
    assert oversized.status_code == 400
    assert oversized.get_json()["code"] == "secret_mutation_too_large"
    assert server._secret_service().get_status(secret_id).configured is False


def test_plaintext_aggregate_route_is_gone(client):
    for method in (client.get, client.put, client.delete):
        response = method("/api/secrets")
        assert response.status_code == 410
        assert response.get_json()["code"] == "plaintext_secret_api_retired"


def test_degraded_native_store_exposes_status_only_and_never_falls_back(
        client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_secret_health",
        lambda: server.SecretStoreHealth(
            "unsupported", has_vault=None, writable=False),
    )
    monkeypatch.setattr(
        server,
        "_secret_service",
        lambda: pytest.fail("degraded status must not consult plaintext"),
    )

    listed = client.get("/api/v1/secrets")
    assert listed.status_code == 200
    assert listed.get_json()["health"] == {
        "available": False, "state": "unsupported", "writable": False,
    }
    assert listed.get_json()["secrets"] == []

    detail = client.get(
        "/api/v1/secrets/" + server._SECRET_IDS["aiKey"])
    assert detail.status_code == 503
    assert detail.get_json()["code"] == "secret_repository_unavailable"


def test_production_engine_binds_public_secret_capabilities(client):
    document = client.get("/api/v1/capabilities").get_json()
    capabilities = {(row["id"], row["version"])
                    for row in document["capabilities"]}
    assert ("library.secrets.status", 1) in capabilities
    assert ("library.secrets.mutate", 1) in capabilities
    public = server._library_engine().get_service(server.SECRET_STORE_SERVICE)
    assert isinstance(public, SecretStoreService)
    assert not hasattr(public, "lease")
