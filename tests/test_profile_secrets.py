"""Account-owned Mistral sync and its crash-safe protected-store journal."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import server
from librarytool.adapters.windows.secret_store import (
    ProtectedEnvelopeSecretStoreRepository,
    SecretStoreUnavailableError,
)
from librarytool.engine.secret_store import (
    ClearSecretCommand,
    ReplaceSecretCommand,
    SecretStoreService,
)


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


def _repository():
    return ProtectedEnvelopeSecretStoreRepository(
        storage=_MemoryStore(),
        protector=_TestProtector(),
        registry=server._SECRET_REGISTRY,
        store_id=server._SECRET_STORE_ID,
    )


@pytest.fixture()
def mistral_env(monkeypatch):
    server._SECRET_SYNC_STATE_PATH.unlink(missing_ok=True)
    repository = _repository()
    service = SecretStoreService(repository)
    active = {"user_id": "user-a"}
    cfg = {"url": "https://example.supabase.co", "key": "anon"}

    monkeypatch.setattr(server, "_secret_repository", repository)
    monkeypatch.setattr(server, "_secret_service", lambda: service)
    monkeypatch.setattr(server, "_secret_health", repository.health.get_health)
    monkeypatch.setattr(
        server, "_active_mistral_account_id", lambda: active["user_id"])
    monkeypatch.setattr(server, "_auth_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_auth_session", lambda: {
        "user_id": active["user_id"],
        "access_token": "token-" + str(active["user_id"]),
        "refresh_token": "refresh",
    } if active["user_id"] else None)

    @contextmanager
    def auth_execution_cfg():
        yield cfg

    monkeypatch.setattr(server, "_auth_execution_cfg", auth_execution_cfg)

    secret_id = server._SECRET_IDS["mistralKey"]
    sequence = {"value": 0}

    def operation(prefix):
        sequence["value"] += 1
        return f"{prefix}-{sequence['value']}"

    def status():
        return service.get_status(secret_id)

    def reveal():
        with repository.credential_leases.lease(secret_id) as leased:
            return leased.reveal()

    def seed(value, *, owner="user-a", phase="synced"):
        before = status()
        if value:
            result = service.replace(ReplaceSecretCommand(
                secret_id=secret_id,
                expected_revision=before.revision,
                credential=value,
                operation_id=operation("seed-replace"),
            ))
        elif before.configured:
            result = service.clear(ClearSecretCommand(
                secret_id=secret_id,
                expected_revision=before.revision,
                operation_id=operation("seed-clear"),
            ))
        else:
            result = None
        after = result.receipt.after if result else status()
        if phase == "unowned":
            record = server._mistral_stable_record(
                "unowned", after.revision)
        else:
            record = server._mistral_stable_record(
                phase, after.revision, owner_user_id=owner)
        server._save_mistral_sync_record(record)
        return after

    env = SimpleNamespace(
        active=active,
        cfg=cfg,
        repository=repository,
        service=service,
        secret_id=secret_id,
        operation=operation,
        status=status,
        reveal=reveal,
        seed=seed,
    )
    yield env
    server._SECRET_SYNC_STATE_PATH.unlink(missing_ok=True)


def _record(env):
    with server._secrets_lock:
        return server._recover_mistral_sync_record(env.status())


def test_profile_mistral_pull_replaces_owned_protected_value(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("old-local", phase="synced")

    def rest(_cfg, _token, method, path, *args, **kwargs):
        assert method == "GET"
        assert path == "profile_secrets?id=eq.user-a&select=api_keys,updated_at"
        return [{"api_keys": {"mistral": "from-cloud", "deepseek": "keep"},
                 "updated_at": "rev-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "from-cloud"
    assert env.reveal() == "from-cloud"
    assert _record(env)["phase"] == "synced"
    assert _record(env)["owner_user_id"] == "user-a"


def test_profile_mistral_pending_edit_merges_and_clears_marker(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("new-local", phase="pending")
    calls = []

    def rest(_cfg, _token, method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "GET":
            return [{"api_keys": {"mistral": "old", "deepseek": "keep"},
                     "updated_at": "rev-1"}]
        return [{"id": "user-a"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "new-local"
    assert calls[1][0] == "PATCH"
    assert calls[1][2]["api_keys"] == {
        "mistral": "new-local", "deepseek": "keep",
    }
    assert env.reveal() == "new-local"
    assert _record(env)["phase"] == "synced"


def test_profile_mistral_offline_retains_pending_protected_edit(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("offline-edit", phase="pending")

    def unavailable(*args, **kwargs):
        raise server.sauth.AuthError("offline", status=503)

    monkeypatch.setattr(server.sauth, "rest", unavailable)
    assert server._sync_profile_mistral_key() is None
    assert env.reveal() == "offline-edit"
    assert _record(env)["phase"] == "pending"


def test_profile_sync_does_not_clear_newer_local_cas(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("first-edit", phase="pending")

    def rest(_cfg, _token, method, _path, body=None, **_kwargs):
        if method == "GET":
            return [{"api_keys": {}, "updated_at": "rev-1"}]
        assert body["api_keys"]["mistral"] == "first-edit"
        fresh = env.status()
        server._commit_mistral_mutation(
            ReplaceSecretCommand(
                secret_id=env.secret_id,
                expected_revision=fresh.revision,
                credential="second-edit",
                operation_id=env.operation("concurrent-replace"),
            ),
            action="replace",
            target_phase="pending",
            target_owner_user_id="user-a",
        )
        return [{"id": "user-a"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "first-edit"
    assert env.reveal() == "second-edit"
    record = _record(env)
    assert record["phase"] == "pending"
    assert record["revision"] == env.status().revision


def test_account_switch_during_successful_upload_settles_old_owner_then_pulls_new(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("account-a-pending", owner="user-a", phase="pending")

    def upload_a(_cfg, _token, method, _path, body=None, **_kwargs):
        if method == "GET":
            return [{"api_keys": {}, "updated_at": "a-r1"}]
        assert body["api_keys"]["mistral"] == "account-a-pending"
        env.active["user_id"] = "user-b"
        return [{"id": "user-a"}]

    monkeypatch.setattr(server.sauth, "rest", upload_a)
    assert server._sync_profile_mistral_key() == "account-a-pending"
    settled = _record(env)
    assert settled["phase"] == "synced"
    assert settled["owner_user_id"] == "user-a"

    monkeypatch.setattr(server.sauth, "rest", lambda *_args, **_kwargs: [{
        "api_keys": {"mistral": "account-b-cloud"},
        "updated_at": "b-r1",
    }])
    assert server._sync_profile_mistral_key() == "account-b-cloud"
    assert env.reveal() == "account-b-cloud"
    switched = _record(env)
    assert switched["phase"] == "synced"
    assert switched["owner_user_id"] == "user-b"


def test_pending_account_a_blocks_account_b_upload_lease_and_overwrite(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("account-a-secret", owner="user-a", phase="pending")
    env.active["user_id"] = "user-b"
    monkeypatch.setattr(
        server.sauth,
        "rest",
        lambda *_args, **_kwargs: pytest.fail("account B must not reach cloud"),
    )

    assert server._sync_profile_mistral_key() is None
    with pytest.raises(RuntimeError, match="not configured"):
        with server._lease_secret("mistralKey"):
            pass
    before = env.status()
    with pytest.raises(server.EngineConflictError) as exc:
        server._mutate_mistral_from_request(ReplaceSecretCommand(
            secret_id=env.secret_id,
            expected_revision=before.revision,
            credential="account-b-secret",
            operation_id=env.operation("account-b-replace"),
        ), action="replace")
    assert exc.value.code == "mistral_pending_for_another_account"
    assert env.reveal() == "account-a-secret"
    assert server._public_secret_status(env.status()).configured is False


def test_synced_account_switch_pulls_b_without_reading_or_uploading_a(
        monkeypatch, mistral_env):
    env = mistral_env
    env.seed("account-a-secret", owner="user-a", phase="synced")
    env.active["user_id"] = "user-b"
    calls = []

    def rest(_cfg, token, method, path, *args, **kwargs):
        calls.append((token, method, path))
        assert method == "GET"
        return [{"api_keys": {"mistral": "account-b-secret"},
                 "updated_at": "b-rev"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "account-b-secret"
    assert calls == [(
        "token-user-b",
        "GET",
        "profile_secrets?id=eq.user-b&select=api_keys,updated_at",
    )]
    assert env.reveal() == "account-b-secret"
    record = _record(env)
    assert record["phase"] == "synced"
    assert record["owner_user_id"] == "user-b"


def test_logout_makes_owned_mistral_lease_unavailable(mistral_env):
    env = mistral_env
    env.seed("account-a-secret", owner="user-a", phase="synced")
    env.active["user_id"] = None

    assert server._secret_is_configured("mistralKey") is False
    with pytest.raises(RuntimeError, match="not configured"):
        with server._lease_secret("mistralKey"):
            pass


def test_signed_out_mistral_stays_usable_as_unowned_local_only_key(
        monkeypatch, mistral_env):
    env = mistral_env
    env.active["user_id"] = None
    before = env.status()
    result = server._mutate_mistral_from_request(ReplaceSecretCommand(
        secret_id=env.secret_id,
        expected_revision=before.revision,
        credential="local-only",
        operation_id=env.operation("local-only-replace"),
    ), action="replace")

    assert result.receipt.after.configured is True
    assert _record(env)["phase"] == "unowned"
    assert server._secret_is_configured("mistralKey") is True
    with server._lease_secret("mistralKey") as credential:
        assert credential == "local-only"

    env.active["user_id"] = "user-b"
    monkeypatch.setattr(
        server.sauth,
        "rest",
        lambda *_args, **_kwargs: pytest.fail("unowned key must not upload"),
    )
    assert server._secret_is_configured("mistralKey") is False
    assert server._public_secret_status(env.status()).configured is False
    with pytest.raises(RuntimeError, match="not configured"):
        with server._lease_secret("mistralKey"):
            pass
    assert server._sync_profile_mistral_key() is None


def test_signed_out_user_can_start_local_mode_after_synced_empty_account(
        mistral_env):
    env = mistral_env
    before = env.seed("", owner="user-a", phase="synced")
    env.active["user_id"] = None

    result = server._mutate_mistral_from_request(ReplaceSecretCommand(
        secret_id=env.secret_id,
        expected_revision=before.revision,
        credential="new-local-mode",
        operation_id=env.operation("new-local-mode"),
    ), action="replace")

    assert result.receipt.after.configured is True
    assert env.reveal() == "new-local-mode"
    assert _record(env)["phase"] == "unowned"


def test_ownerless_legacy_key_never_uploads_and_requires_explicit_reentry(
        monkeypatch, mistral_env):
    env = mistral_env
    before = env.status()
    env.service.replace(ReplaceSecretCommand(
        secret_id=env.secret_id,
        expected_revision=before.revision,
        credential="ownerless-legacy",
        operation_id=env.operation("legacy-seed"),
    ))
    monkeypatch.setattr(
        server.sauth,
        "rest",
        lambda *_args, **_kwargs: pytest.fail("ownerless key must not upload"),
    )

    assert server._sync_profile_mistral_key() is None
    assert _record(env)["phase"] == "unowned"
    fresh = env.status()
    result = server._mutate_mistral_from_request(ReplaceSecretCommand(
        secret_id=env.secret_id,
        expected_revision=fresh.revision,
        credential="explicit-account-a-key",
        operation_id=env.operation("explicit-reentry"),
    ), action="replace")
    assert result.receipt.after.configured is True
    assert env.reveal() == "explicit-account-a-key"
    record = _record(env)
    assert record["phase"] == "pending"
    assert record["owner_user_id"] == "user-a"


def test_signed_in_clear_cannot_erase_unowned_or_remote_mistral_key(
        client, monkeypatch, mistral_env):
    env = mistral_env
    before = env.seed("ownerless-local", phase="unowned")
    monkeypatch.setattr(
        server.sauth,
        "rest",
        lambda *_args, **_kwargs: pytest.fail(
            "an unowned clear must never reach profile sync"),
    )

    response = client.delete(
        "/api/v1/secrets/" + env.secret_id,
        headers={
            "If-Match": f'"{before.revision}"',
            "Idempotency-Key": env.operation("unowned-clear"),
        },
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "mistral_credential_unowned"
    assert env.reveal() == "ownerless-local"
    assert _record(env) == {
        "phase": "unowned",
        "revision": before.revision,
    }
    assert server._public_secret_status(env.status()).configured is False


def test_cross_account_exact_replay_cannot_transfer_credential_ownership(
        mistral_env):
    env = mistral_env
    initial = env.status()
    operation_id = env.operation("account-a-original")
    command = ReplaceSecretCommand(
        secret_id=env.secret_id,
        expected_revision=initial.revision,
        credential="account-a-secret",
        operation_id=operation_id,
    )
    first = server._mutate_mistral_from_request(command, action="replace")
    server._save_mistral_sync_record(server._mistral_stable_record(
        "synced", first.receipt.after.revision, owner_user_id="user-a"))

    env.active["user_id"] = "user-b"
    replay = server._mutate_mistral_from_request(command, action="replace")

    assert replay.replayed is True
    assert env.reveal() == "account-a-secret"
    record = _record(env)
    assert record["phase"] == "synced"
    assert record["owner_user_id"] == "user-a"
    assert record["revision"] == first.receipt.after.revision


def test_write_ahead_crash_before_vault_commit_restores_prior_state(
        monkeypatch, mistral_env):
    env = mistral_env
    before = env.seed("stable", owner="user-a", phase="synced")
    real_service = env.service

    class CrashBeforeCommit:
        get_status = real_service.get_status

        @staticmethod
        def replace(_command):
            raise KeyboardInterrupt("simulated process death")

    monkeypatch.setattr(server, "_secret_service", lambda: CrashBeforeCommit())
    with pytest.raises(KeyboardInterrupt):
        server._commit_mistral_mutation(
            ReplaceSecretCommand(
                secret_id=env.secret_id,
                expected_revision=before.revision,
                credential="not-committed",
                operation_id=env.operation("crash-before"),
            ),
            action="replace",
            target_phase="pending",
            target_owner_user_id="user-a",
        )

    monkeypatch.setattr(server, "_secret_service", lambda: real_service)
    record = server._recover_mistral_sync_record(env.status())
    assert record["phase"] == "synced"
    assert record["revision"] == before.revision
    assert env.reveal() == "stable"


def test_write_ahead_crash_after_vault_commit_recovers_pending_intent(
        monkeypatch, mistral_env):
    env = mistral_env
    before = env.status()
    real_save = server._save_mistral_sync_record
    saves = {"count": 0}

    def crash_on_final_save(record):
        saves["count"] += 1
        if saves["count"] == 2:
            raise KeyboardInterrupt("simulated process death")
        real_save(record)

    monkeypatch.setattr(server, "_save_mistral_sync_record", crash_on_final_save)
    with pytest.raises(KeyboardInterrupt):
        server._commit_mistral_mutation(
            ReplaceSecretCommand(
                secret_id=env.secret_id,
                expected_revision=before.revision,
                credential="committed-before-crash",
                operation_id=env.operation("crash-after"),
            ),
            action="replace",
            target_phase="pending",
            target_owner_user_id="user-a",
        )

    monkeypatch.setattr(server, "_save_mistral_sync_record", real_save)
    record = server._recover_mistral_sync_record(env.status())
    assert record["phase"] == "pending"
    assert record["owner_user_id"] == "user-a"
    assert record["revision"] == env.status().revision
    assert env.reveal() == "committed-before-crash"


def test_mistral_http_replace_attempts_immediate_sync(
        client, monkeypatch, mistral_env):
    env = mistral_env
    calls = []

    def rest(_cfg, _token, method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "GET":
            return []
        return [{"id": "user-a"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    before = env.status()
    response = client.put(
        "/api/v1/secrets/" + env.secret_id,
        json={"credential": "immediate-secret"},
        headers={
            "If-Match": f'"{before.revision}"',
            "Idempotency-Key": env.operation("http-immediate"),
        },
    )

    assert response.status_code == 200
    assert "immediate-secret" not in response.get_data(as_text=True)
    assert calls[1][0] == "POST"
    assert calls[1][2][0]["api_keys"]["mistral"] == "immediate-secret"
    assert _record(env)["phase"] == "synced"


def test_legacy_renderer_http_import_is_unowned_and_never_syncs(
        client, monkeypatch, mistral_env):
    env = mistral_env
    monkeypatch.setattr(
        server.sauth,
        "rest",
        lambda *_args, **_kwargs: pytest.fail("legacy import must not sync"),
    )
    before = env.status()
    response = client.put(
        "/api/v1/secrets/" + env.secret_id,
        json={"credential": "legacy-renderer-value"},
        headers={
            "If-Match": f'"{before.revision}"',
            "Idempotency-Key": env.operation("legacy-renderer-import"),
            server._LEGACY_RENDERER_SECRET_HEADER:
                server._LEGACY_RENDERER_SECRET_SOURCE,
        },
    )

    assert response.status_code == 200
    assert "legacy-renderer-value" not in response.get_data(as_text=True)
    record = _record(env)
    assert record["phase"] == "unowned"
    assert "owner_user_id" not in record
    # An active account cannot see or lease this ownerless upgrade value.
    assert server._public_secret_status(env.status()).configured is False
    with pytest.raises(RuntimeError, match="not configured"):
        with server._lease_secret("mistralKey"):
            pass


def test_mistral_http_remote_failure_keeps_committed_pending_retry(
        client, monkeypatch, mistral_env):
    env = mistral_env
    monkeypatch.setattr(
        server.sauth,
        "rest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            server.sauth.AuthError("offline", status=503)),
    )
    before = env.status()
    response = client.put(
        "/api/v1/secrets/" + env.secret_id,
        json={"credential": "offline-but-safe"},
        headers={
            "If-Match": f'"{before.revision}"',
            "Idempotency-Key": env.operation("http-offline"),
        },
    )

    assert response.status_code == 200
    assert env.reveal() == "offline-but-safe"
    record = _record(env)
    assert record["phase"] == "pending"
    assert record["owner_user_id"] == "user-a"


def test_mistral_http_clear_attempts_immediate_remote_clear(
        client, monkeypatch, mistral_env):
    env = mistral_env
    before = env.seed("delete-me", owner="user-a", phase="synced")
    calls = []

    def rest(_cfg, _token, method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "GET":
            return [{"api_keys": {"mistral": "delete-me", "deepseek": "keep"},
                     "updated_at": "remote-r1"}]
        return [{"id": "user-a"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    response = client.delete(
        "/api/v1/secrets/" + env.secret_id,
        headers={
            "If-Match": f'"{before.revision}"',
            "Idempotency-Key": env.operation("http-clear"),
        },
    )

    assert response.status_code == 200
    assert calls[1][0] == "PATCH"
    assert calls[1][2]["api_keys"] == {"mistral": "", "deepseek": "keep"}
    assert env.status().configured is False
    assert _record(env)["phase"] == "synced"
