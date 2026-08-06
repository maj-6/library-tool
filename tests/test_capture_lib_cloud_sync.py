from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
import supabase_sync

from librarytool.engine.errors import RepositoryError


CAPTURE_ID = "11111111-2222-4333-8444-555555555555"
OTHER_CAPTURE_ID = "66666666-7777-4888-8999-aaaaaaaaaaaa"
ACTOR_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CAPABILITY = "whlcap1_" + "f" * 64


def association(**changes) -> dict:
    value = {
        "schema": "org.whl.capture-lib-association",
        "version": 1,
        "capture_id": CAPTURE_ID,
        "book_id": "b-" + "a" * 32,
        "archive_sha256": "b" * 64,
        "archive_bytes": 12345,
        "format_version": "3.0",
        "state": "current",
        "generated_at": "2026-07-23T12:34:56+00:00",
        "source_revision": "sha256:" + "c" * 64,
        "source_fingerprint": "d" * 64,
    }
    value.update(changes)
    return value


def service_key() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"role": "service_role"}).encode(),
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def user_token(
    role: str = "authenticated",
    subject: str = ACTOR_ID,
) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"role": role, "sub": subject}).encode(),
    ).decode().rstrip("=")
    return f"{header}.{payload}.signature"


def publish_configs() -> tuple[dict, dict]:
    return (
        {"url": "https://example.supabase.co", "key": service_key()},
        {
            "url": "https://example.supabase.co",
            "key": "sb_publishable_example",
            "access_token": user_token(),
        },
    )


def prepared_row(
    *,
    expected_revision: int = 0,
    mark_imported: bool = True,
    **changes,
) -> dict:
    value = {
        "capture_id": CAPTURE_ID,
        "actor_id": ACTOR_ID,
        "association": association(),
        "association_digest": "e" * 64,
        "expected_revision": expected_revision,
        "mark_imported": mark_imported,
        "authorization_expires_at": "2026-07-23T12:39:00+00:00",
        "capability_state": "prepared",
    }
    value.update(changes)
    return value


def accepted_row(**changes) -> dict:
    value = {
        "id": CAPTURE_ID,
        "status": "imported",
        "lib_association": association(),
        "lib_association_revision": 1,
        "lib_association_updated_at": "2026-07-23T12:35:00+00:00",
    }
    value.update(changes)
    return value


def stable_capability(monkeypatch) -> None:
    def token_hex(byte_count: int) -> str:
        assert byte_count == 32
        return "f" * 64

    monkeypatch.setattr(supabase_sync.secrets, "token_hex", token_hex)


def test_capture_lib_validator_detaches_and_accepts_current_or_stale():
    original = association()
    parsed = supabase_sync._capture_lib_association_write(original, CAPTURE_ID)
    original["state"] = "stale"
    assert parsed["state"] == "current"
    assert supabase_sync._capture_lib_association_write(
        association(state="stale"),
        CAPTURE_ID,
    )["state"] == "stale"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("source_fingerprint"),
        lambda value: value.update(secret="not portable"),
        lambda value: value.update(state="available"),
        lambda value: value.update(
            capture_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        ),
        lambda value: value.update(book_id="manual-local-path"),
        lambda value: value.update(archive_sha256="A" * 64),
        lambda value: value.update(archive_bytes=True),
        lambda value: value.update(archive_bytes=250 * 1024 * 1024 + 1),
        lambda value: value.update(format_version=3.0),
        lambda value: value.update(generated_at="2026-07-23T12:34:56"),
        lambda value: value.update(generated_at="2026-07-23T12:34:56+0000"),
        lambda value: value.update(generated_at="2026-W30-4T12:34:56+00:00"),
        lambda value: value.update(generated_at="2026-07-23 12:34:56+00:00"),
        lambda value: value.update(generated_at="2026-07-23T12:34:60+00:00"),
        lambda value: value.update(generated_at="2026-07-23T12:34:56+14:01"),
        lambda value: value.update(source_revision="C:\\private\\capture"),
        lambda value: value.update(source_revision="/private/capture"),
        lambda value: value.update(source_fingerprint="x" * 64),
        lambda value: value.update(capture_id=f" {CAPTURE_ID}"),
        lambda value: value.update(capture_id=int("1" * 32)),
    ],
)
def test_capture_lib_validator_rejects_invalid_or_nonportable_documents(mutate):
    value = association()
    mutate(value)
    with pytest.raises(supabase_sync.SyncError):
        supabase_sync._capture_lib_association_write(value, CAPTURE_ID)


def test_publish_prepares_user_capability_then_consumes_only_token(monkeypatch):
    stable_capability(monkeypatch)
    calls = []
    service_cfg, scope_cfg = publish_configs()

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if cfg is scope_cfg:
            return [prepared_row()]
        return [accepted_row()]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    result = supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        association(),
    )

    assert result == accepted_row()
    assert len(calls) == 2
    assert calls[0] == (
        scope_cfg,
        "POST",
        "rpc/prepare_capture_lib_association",
        {
            "p_capability": CAPABILITY,
            "p_capture_id": CAPTURE_ID,
            "p_association": association(),
            "p_expected_revision": 0,
            "p_mark_imported": True,
        },
        "",
    )
    assert calls[1] == (
        service_cfg,
        "POST",
        "rpc/publish_capture_lib_association",
        {"p_capability": CAPABILITY},
        "",
    )
    assert "p_actor_id" not in calls[0][3]
    assert set(calls[1][3]) == {"p_capability"}


def test_association_only_update_binds_stale_document_and_false_mark(monkeypatch):
    stable_capability(monkeypatch)
    row = accepted_row(
        status="pending",
        lib_association=association(state="stale"),
        lib_association_revision=2,
    )
    calls = []
    service_cfg, scope_cfg = publish_configs()

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if cfg is scope_cfg:
            return [prepared_row(
                expected_revision=1,
                mark_imported=False,
                association=association(state="stale"),
            )]
        return [row]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    result = supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        association(state="stale"),
        expected_revision=1,
        mark_imported=False,
    )

    assert result["lib_association"]["state"] == "stale"
    assert calls[0][3]["p_association"]["state"] == "stale"
    assert calls[0][3]["p_mark_imported"] is False
    assert calls[1][3] == {"p_capability": CAPABILITY}


def test_first_acknowledgement_can_mark_a_stale_archive_imported(monkeypatch):
    stable_capability(monkeypatch)
    stale = association(state="stale")
    service_cfg, scope_cfg = publish_configs()

    def rest(cfg, _method, _path, _payload=None, _prefer=""):
        if cfg is scope_cfg:
            return [prepared_row(association=stale)]
        return [accepted_row(lib_association=stale)]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    result = supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        stale,
        mark_imported=True,
    )

    assert result["status"] == "imported"
    assert result["lib_association"]["state"] == "stale"


@pytest.mark.parametrize("mark_imported", [False, True])
def test_publish_accepts_consumed_prepare_and_exact_noop(
    monkeypatch,
    mark_imported,
):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    row = accepted_row(lib_association_revision=3)
    if not mark_imported:
        row["status"] = "pending"

    def rest(cfg, *_args, **_kwargs):
        if cfg is scope_cfg:
            return [
                prepared_row(
                    expected_revision=3,
                    mark_imported=mark_imported,
                    capability_state="consumed",
                )
            ]
        return [row]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    result = supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        association(),
        expected_revision=3,
        mark_imported=mark_imported,
    )
    assert result["lib_association_revision"] == 3


def test_lost_prepare_response_retries_same_capability(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if cfg is scope_cfg and len(calls) == 1:
            raise supabase_sync.SyncError("TimeoutError: response lost")
        if cfg is scope_cfg:
            return [prepared_row()]
        return [accepted_row()]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    assert supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        association(),
    ) == accepted_row()
    assert len(calls) == 3
    assert calls[0][3] == calls[1][3]
    assert calls[0][3]["p_capability"] == CAPABILITY


def test_lost_consume_response_retries_same_capability(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    service_calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        if cfg is scope_cfg:
            return [prepared_row()]
        service_calls.append((method, path, payload, prefer))
        if len(service_calls) == 1:
            raise supabase_sync.SyncError("ConnectionResetError: response lost")
        return [accepted_row()]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    assert supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        association(),
    ) == accepted_row()
    assert len(service_calls) == 2
    assert service_calls[0][2] == service_calls[1][2] == {
        "p_capability": CAPABILITY
    }


def test_rpc_conflict_is_not_retried_or_bypassed(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        raise supabase_sync.SyncError(
            "HTTP 409 on POST https://example.supabase.co/rest/v1/"
            "rpc/prepare_capture_lib_association: conflict"
        )

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="HTTP 409"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )
    assert len(calls) == 1
    assert calls[0][0] is scope_cfg


@pytest.mark.parametrize(
    "change",
    [
        {"capture_id": "22222222-2222-4222-8222-222222222222"},
        {"actor_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
        {"association": association(state="stale")},
        {"association_digest": "not-a-digest"},
        {"expected_revision": True},
        {"expected_revision": 2},
        {"mark_imported": False},
        {"authorization_expires_at": "not-a-timestamp"},
        {"capability_state": "available"},
        {"extra": "field"},
    ],
)
def test_prepare_must_return_exact_verified_scope(monkeypatch, change):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if cfg is scope_cfg:
            return [prepared_row(**change)]
        return pytest.fail("invalid prepared scope reached service consume")

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="preparation.*invalid row"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )
    assert len(calls) == 1


def test_malformed_prepare_response_retries_but_never_consumes(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        assert cfg is scope_cfg
        return []

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="preparation.*invalid row"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )
    assert len(calls) == 2
    assert calls[0][3] == calls[1][3]


def test_publish_must_return_one_verified_confirmation_row(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    service_calls = 0

    def rest(cfg, *_args, **_kwargs):
        nonlocal service_calls
        if cfg is scope_cfg:
            return [prepared_row()]
        service_calls += 1
        return []

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="publication.*invalid row"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )
    assert service_calls == 2


def test_publish_rejects_conflicting_confirmation(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()

    def rest(cfg, *_args, **_kwargs):
        if cfg is scope_cfg:
            return [prepared_row()]
        return [accepted_row(lib_association=association(state="stale"))]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="compare-and-set"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )


def test_publish_rejects_untrusted_credentials_and_scope(monkeypatch):
    service_cfg, scope_cfg = publish_configs()
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: pytest.fail("untrusted config reached REST"),
    )

    with pytest.raises(supabase_sync.SyncError, match="service credential"):
        supabase_sync.publish_capture_lib_association(
            {
                "url": "https://example.supabase.co",
                "key": "sb_publishable_example",
                "access_token": "user-jwt",
            },
            scope_cfg,
            CAPTURE_ID,
            association(),
        )

    bad_scope = dict(scope_cfg, key="sb_secret_not_a_scope")
    with pytest.raises(supabase_sync.SyncError, match="signed-in user scope"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            bad_scope,
            CAPTURE_ID,
            association(),
        )

    bad_subject = dict(scope_cfg, access_token=user_token(subject="not-a-uuid"))
    with pytest.raises(supabase_sync.SyncError, match="signed-in user scope"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            bad_subject,
            CAPTURE_ID,
            association(),
        )

    wrong_project = dict(scope_cfg, url="https://other.supabase.co")
    with pytest.raises(supabase_sync.SyncError, match="different projects"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            wrong_project,
            CAPTURE_ID,
            association(),
        )

    with pytest.raises(supabase_sync.SyncError, match="publication id is invalid"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
            association(),
        )


def test_invalid_generated_capability_never_reaches_rest(monkeypatch):
    service_cfg, scope_cfg = publish_configs()
    monkeypatch.setattr(
        supabase_sync,
        "_new_capture_lib_capability",
        lambda: "predictable",
    )
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: pytest.fail("invalid token reached REST"),
    )

    with pytest.raises(supabase_sync.SyncError, match="generation failed"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )


def test_capture_association_states_are_exact_scoped_and_chunked(monkeypatch):
    cfg = {"table": "captures"}
    calls = []

    def fake_rest(actual_cfg, method, path, payload=None, **_kwargs):
        calls.append((actual_cfg, method, path, payload))
        requested = OTHER_CAPTURE_ID if OTHER_CAPTURE_ID in path else CAPTURE_ID
        return [{
            "id": requested,
            "status": "imported",
            "lib_association": None,
            "lib_association_revision": 0,
            "lib_association_updated_at": None,
        }]

    monkeypatch.setattr(supabase_sync, "_rest", fake_rest)
    rows = supabase_sync.list_capture_association_states(
        cfg,
        [CAPTURE_ID, OTHER_CAPTURE_ID, CAPTURE_ID, "not-a-uuid"],
        chunk=1,
    )

    assert [row["id"] for row in rows] == [CAPTURE_ID, OTHER_CAPTURE_ID]
    assert len(calls) == 2
    assert all(call[0] is cfg and call[1] == "GET" for call in calls)
    assert all(
        "select=id,status,lib_association,lib_association_revision,"
        "lib_association_updated_at&order=id.asc" in call[2]
        for call in calls
    )


@pytest.mark.parametrize("chunk", [0, 101, True, 1.5])
def test_capture_association_states_reject_invalid_chunks(chunk):
    with pytest.raises(
        supabase_sync.SyncError,
        match="association state chunk is invalid",
    ):
        supabase_sync.list_capture_association_states(
            {},
            [CAPTURE_ID],
            chunk=chunk,
        )


@pytest.mark.parametrize(
    "response",
    [
        None,
        [{"id": CAPTURE_ID}],
        [{
            "id": OTHER_CAPTURE_ID,
            "status": "imported",
            "lib_association": None,
            "lib_association_revision": 0,
            "lib_association_updated_at": None,
        }],
    ],
)
def test_capture_association_states_fail_closed_on_malformed_rows(
    monkeypatch,
    response,
):
    monkeypatch.setattr(
        supabase_sync,
        "_rest",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(supabase_sync.SyncError, match="association state"):
        supabase_sync.list_capture_association_states({}, [CAPTURE_ID])


@pytest.mark.parametrize("status", ["pending", "error"])
def test_scoped_publisher_atomically_imports_recoverable_null_row(
    monkeypatch,
    status,
):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if method == "GET":
            return [{
                "id": CAPTURE_ID,
                "status": status,
                "lib_association": None,
                "lib_association_revision": 0,
                "lib_association_updated_at": None,
            }]
        if cfg is scope_cfg:
            return [prepared_row()]
        return [accepted_row()]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    publisher = supabase_sync.ScopedCaptureLibAssociationPublisher(
        service_cfg,
        scope_cfg,
    )
    publisher.publish(SimpleNamespace(as_dict=lambda: association()))

    assert [call[1] for call in calls] == ["GET", "POST", "POST"]
    assert calls[0][2].startswith(
        f"captures?id=in.({CAPTURE_ID})&select=id,status,lib_association,"
    )
    assert calls[1][2] == "rpc/prepare_capture_lib_association"
    assert calls[2][2] == "rpc/publish_capture_lib_association"
    assert calls[1][3]["p_expected_revision"] == 0
    assert calls[1][3]["p_mark_imported"] is True


def test_scoped_publisher_rejects_non_importable_null_row(monkeypatch):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(*_args, **_kwargs):
        calls.append(True)
        return [{
            "id": CAPTURE_ID,
            "status": "void",
            "lib_association": None,
            "lib_association_revision": 0,
            "lib_association_updated_at": None,
        }]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    publisher = supabase_sync.ScopedCaptureLibAssociationPublisher(
        service_cfg,
        scope_cfg,
    )

    with pytest.raises(RepositoryError) as failure:
        publisher.publish(SimpleNamespace(as_dict=lambda: association()))

    assert failure.value.code == "capture_cloud_target_not_importable"
    assert failure.value.retryable is False
    assert calls == [True]


def test_scoped_publisher_fills_legacy_imported_null_without_status_race(
    monkeypatch,
):
    stable_capability(monkeypatch)
    service_cfg, scope_cfg = publish_configs()
    prepared_payloads = []

    def rest(cfg, method, _path, payload=None, _prefer=""):
        if method == "GET":
            return [{
                "id": CAPTURE_ID,
                "status": "imported",
                "lib_association": None,
                "lib_association_revision": 0,
                "lib_association_updated_at": None,
            }]
        if cfg is scope_cfg:
            prepared_payloads.append(payload)
            return [prepared_row(mark_imported=False)]
        return [accepted_row()]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    supabase_sync.ScopedCaptureLibAssociationPublisher(
        service_cfg,
        scope_cfg,
    ).publish(SimpleNamespace(as_dict=lambda: association()))

    assert prepared_payloads[0]["p_expected_revision"] == 0
    assert prepared_payloads[0]["p_mark_imported"] is False


def test_scoped_publisher_replays_exact_state_and_rejects_drift_or_expired_lease(
    monkeypatch,
):
    service_cfg, scope_cfg = publish_configs()
    row = accepted_row()
    calls = []

    def rest(*_args, **_kwargs):
        calls.append(True)
        return [row]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    publisher = supabase_sync.ScopedCaptureLibAssociationPublisher(
        service_cfg,
        scope_cfg,
    )
    value = SimpleNamespace(as_dict=lambda: association())
    publisher.publish(value)
    assert len(calls) == 1

    row["lib_association"] = association(book_id="b-" + "e" * 32)
    with pytest.raises(RepositoryError) as conflict:
        publisher.publish(value)
    assert conflict.value.code == "capture_cloud_association_conflict"
    assert conflict.value.retryable is False
    assert str(conflict.value) == (
        "capture cloud association conflicts with remote state"
    )
    assert len(calls) == 2

    service_cfg.pop("key")
    with pytest.raises(RepositoryError) as expired:
        publisher.publish(value)
    assert expired.value.code == (
        "capture_cloud_publication_authority_unavailable"
    )
    assert expired.value.retryable is False
    assert str(expired.value) == (
        "capture cloud publication authority is unavailable"
    )
    assert len(calls) == 2


def test_scoped_publisher_normalizes_private_transport_failure_for_engine_port(
    monkeypatch,
):
    service_cfg, scope_cfg = publish_configs()
    private_detail = (
        "HTTP 503 on GET https://private-project.supabase.co/rest/v1/captures: "
        '{"service_role":"must-not-leak"}'
    )

    def rest(*_args, **_kwargs):
        raise supabase_sync.SyncError(private_detail)

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    publisher = supabase_sync.ScopedCaptureLibAssociationPublisher(
        service_cfg,
        scope_cfg,
    )

    with pytest.raises(RepositoryError) as failure:
        publisher.publish(SimpleNamespace(as_dict=lambda: association()))

    assert failure.value.code == "capture_cloud_state_unavailable"
    assert failure.value.retryable is True
    assert str(failure.value) == (
        "capture cloud association state is unavailable"
    )
    assert "private-project" not in str(failure.value)
    assert "service_role" not in str(failure.value)
