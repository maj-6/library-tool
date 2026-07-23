from __future__ import annotations

import base64
import json

import pytest
import supabase_sync


CAPTURE_ID = "11111111-2222-4333-8444-555555555555"


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


def user_token(role: str = "authenticated") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"role": role, "sub": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}).encode(),
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
        lambda value: value.update(capture_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
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


def test_atomic_publish_uses_service_cas_and_returns_verified_row(monkeypatch):
    calls = []
    service_cfg, scope_cfg = publish_configs()

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}]
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
    assert calls[0][0] is scope_cfg
    assert calls[0][1] == "GET"
    assert "id=in.(11111111-2222-4333-8444-555555555555)" in calls[0][2]
    cfg, method, path, payload, prefer = calls[1]
    assert cfg is service_cfg
    assert method == "PATCH"
    assert path.startswith(
        "captures?id=eq.11111111-2222-4333-8444-555555555555"
        "&lib_association_revision=eq.0&status=eq.pending&select=",
    )
    assert payload == {
        "status": "imported",
        "lib_association": association(),
    }
    assert prefer == "return=representation"


def test_atomic_publish_accepts_exact_replay_after_cas_race(monkeypatch):
    service_cfg, scope_cfg = publish_configs()
    service_responses = iter([[], [accepted_row()]])
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}]
        return next(service_responses)

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    result = supabase_sync.publish_capture_lib_association(
        service_cfg,
        scope_cfg,
        CAPTURE_ID,
        association(),
    )
    assert result["lib_association_revision"] == 1
    assert [call[1] for call in calls] == ["GET", "PATCH", "GET"]
    assert "id=eq.11111111-2222-4333-8444-555555555555" in calls[2][2]
    assert "limit=2" in calls[2][2]


def test_association_only_update_keeps_status_out_of_payload(monkeypatch):
    row = accepted_row(
        lib_association=association(state="stale"),
        lib_association_revision=2,
    )
    calls = []
    service_cfg, scope_cfg = publish_configs()

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((method, path, payload, prefer))
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}]
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
    assert calls[1][2] == {"lib_association": association(state="stale")}
    assert "status=eq.pending" not in calls[1][1]


@pytest.mark.parametrize("mark_imported", [False, True])
def test_atomic_publish_accepts_direct_same_document_noop(monkeypatch, mark_imported):
    service_cfg, scope_cfg = publish_configs()
    row = accepted_row(lib_association_revision=3)
    if not mark_imported:
        row["status"] = "pending"

    def rest(cfg, *_args, **_kwargs):
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}]
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


def test_zero_row_cas_only_accepts_exact_previous_attempt_replay(monkeypatch):
    service_cfg, scope_cfg = publish_configs()
    service_responses = iter([
        [],
        [accepted_row(lib_association_revision=3)],
    ])

    def rest(cfg, *_args, **_kwargs):
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}]
        return next(service_responses)

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="compare-and-set"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
            expected_revision=3,
        )


def test_publish_requires_exact_user_rls_scope_before_service_write(monkeypatch):
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer))
        return []

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    with pytest.raises(supabase_sync.SyncError, match="outside the signed-in user scope"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )
    assert len(calls) == 1
    assert calls[0][0] is scope_cfg
    assert calls[0][1] == "GET"


@pytest.mark.parametrize(
    ("scope_case", "visible", "service_patch_expected"),
    [
        ("owner", True, True),
        ("assigned_ingester", True, True),
        ("unrelated_user", False, False),
    ],
)
def test_publish_honors_rls_visibility_before_service_write(
    monkeypatch,
    scope_case,
    visible,
    service_patch_expected,
):
    service_cfg, scope_cfg = publish_configs()
    calls = []

    def rest(cfg, method, path, payload=None, prefer=""):
        calls.append((cfg, method, path, payload, prefer, scope_case))
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}] if visible else []
        return [accepted_row()]

    monkeypatch.setattr(supabase_sync, "_rest", rest)
    if visible:
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )
    else:
        with pytest.raises(supabase_sync.SyncError, match="outside"):
            supabase_sync.publish_capture_lib_association(
                service_cfg,
                scope_cfg,
                CAPTURE_ID,
                association(),
            )
    assert any(call[0] is service_cfg for call in calls) is service_patch_expected


def test_atomic_publish_rejects_conflict_or_untrusted_credentials(monkeypatch):
    service_cfg, scope_cfg = publish_configs()

    def conflict_rest(cfg, *_args, **_kwargs):
        if cfg is scope_cfg:
            return [{"id": CAPTURE_ID}]
        return [accepted_row(lib_association=association(state="stale"))]

    monkeypatch.setattr(supabase_sync, "_rest", conflict_rest)
    with pytest.raises(supabase_sync.SyncError, match="compare-and-set"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            CAPTURE_ID,
            association(),
        )

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

    with pytest.raises(supabase_sync.SyncError, match="publication id is invalid"):
        supabase_sync.publish_capture_lib_association(
            service_cfg,
            scope_cfg,
            "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",
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
