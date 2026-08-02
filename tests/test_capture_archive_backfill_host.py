"""Bounded desktop-host invocation for legacy capture archive repair."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import uuid
from pathlib import Path

import libcommon as lib
import pytest
import server
from PIL import Image
from librarytool.adapters.filesystem.capture_archive_repository import (
    FilesystemCaptureArchiveRepository,
)


def _entry(capture_id: str) -> dict:
    return {
        "id": f"manual-{capture_id}",
        "capture_id": capture_id,
        "title": f"Legacy {capture_id}",
        "created_at": "2026-07-30T12:00:00+00:00",
    }


def _assets(root: Path, capture_id: str) -> None:
    directory = root / capture_id
    directory.mkdir(parents=True)
    original = io.BytesIO()
    Image.new("RGB", (7, 11), (24, 80, 46)).save(original, format="JPEG")
    display = io.BytesIO()
    Image.new("RGB", (5, 9), (80, 46, 24)).save(display, format="JPEG")
    (directory / "orig_1.jpg").write_bytes(original.getvalue())
    (directory / "photo_1.jpg").write_bytes(display.getvalue())


def _workspace(monkeypatch, tmp_path: Path, capture_ids: tuple[str, ...]):
    captures = tmp_path / "captures"
    manual = tmp_path / "manual_entries.json"
    output = tmp_path / "output"
    for capture_id in capture_ids:
        _assets(captures, capture_id)
    lib.save_json(
        manual,
        {
            f"manual-{index}": _entry(capture_id)
            for index, capture_id in enumerate(capture_ids)
        },
    )
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual)
    monkeypatch.setattr(lib, "OUTPUT_DIR", output)
    monkeypatch.setattr(server, "CAPTURES_DIR", captures)
    return captures, manual, output


@contextlib.contextmanager
def _clearing_lease(value: dict, exits: list[str], label: str):
    try:
        yield value
    finally:
        value.clear()
        exits.append(label)


def _cloud_row(capture_id: str) -> dict:
    return {
        "id": capture_id,
        "status": "pending",
        "lib_association": None,
        "lib_association_revision": 0,
        "lib_association_updated_at": None,
    }


def test_apply_backfills_all_selected_but_publishes_only_rls_visible_rows(
    client,
    monkeypatch,
    tmp_path,
):
    cloud_id = str(uuid.uuid4())
    local_id = "lan-local-capture"
    _captures, _manual, output = _workspace(
        monkeypatch,
        tmp_path,
        (cloud_id, local_id),
    )
    owner_cfg = {"url": "https://project.invalid", "key": "owner-secret"}
    capture_cfg = {
        "url": "https://project.invalid",
        "key": "anon-secret",
        "access_token": "user-secret",
    }
    exits: list[str] = []
    publishers = []

    monkeypatch.setattr(
        server,
        "_lease_cloud_cfg",
        lambda: _clearing_lease(owner_cfg, exits, "owner"),
    )
    monkeypatch.setattr(
        server,
        "_lease_capture_cfg",
        lambda: _clearing_lease(capture_cfg, exits, "capture"),
    )
    discovered = []

    def list_states(cfg, capture_ids):
        discovered.append((cfg, tuple(capture_ids)))
        return [_cloud_row(cloud_id)]

    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        list_states,
    )

    class Publisher:
        def __init__(self, owner, capture) -> None:
            self.owner = owner
            self.capture = capture
            self.calls = []
            publishers.append(self)

        def publish(self, association) -> None:
            # Publication is observably after the local archive transaction.
            assert FilesystemCaptureArchiveRepository.inspect_association(
                output,
                association.capture_id,
            ) == association
            self.calls.append(association.as_dict())

    monkeypatch.setattr(
        server.sbase,
        "ScopedCaptureLibAssociationPublisher",
        Publisher,
    )

    response = client.post(
        "/api/v1/capture-archive-backfills",
        json={"capture_ids": [cloud_id, local_id], "apply": True},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["schema"] == "org.whl.capture-lib-backfill-host-result"
    assert body["scope"]["requested"] == 2
    assert body["cloud"] == {
        "status": "complete",
        "code": "capture_cloud_publication_complete",
        "candidate_count": 1,
        "local_only_count": 1,
        "authorized_count": 1,
        "published": 1,
        "failed": 0,
        "unpublished_candidate_count": 0,
    }
    assert body["report"]["summary"]["created"] == 2
    assert body["report"]["summary"]["cloud_succeeded"] == 1
    assert [call["capture_id"] for call in publishers[0].calls] == [cloud_id]
    assert discovered[0][1] == (cloud_id,)
    assert FilesystemCaptureArchiveRepository.inspect_association(
        output,
        local_id,
    ) is not None
    assert exits == ["owner", "capture"]
    assert owner_cfg == {}
    assert capture_cfg == {}
    assert publishers[0].owner == {}
    assert publishers[0].capture == {}
    encoded = json.dumps(body)
    assert "owner-secret" not in encoded
    assert "user-secret" not in encoded


def test_apply_local_only_scope_never_leases_or_publishes(
    client,
    monkeypatch,
    tmp_path,
):
    capture_id = "paired-lan-only"
    _captures, _manual, output = _workspace(
        monkeypatch,
        tmp_path,
        (capture_id,),
    )

    def unexpected_lease():
        raise AssertionError("local-only scope attempted to lease credentials")

    monkeypatch.setattr(server, "_lease_cloud_cfg", unexpected_lease)
    monkeypatch.setattr(server, "_lease_capture_cfg", unexpected_lease)
    monkeypatch.setattr(
        server.sbase,
        "ScopedCaptureLibAssociationPublisher",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("local-only scope constructed a publisher")
        ),
    )

    response = client.post(
        "/api/v1/capture-archive-backfills",
        json={"capture_ids": [capture_id], "apply": True},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["cloud"]["status"] == "complete"
    assert body["cloud"]["code"] == "capture_cloud_scope_not_applicable"
    assert body["cloud"]["local_only_count"] == 1
    assert body["report"]["summary"]["created"] == 1
    assert FilesystemCaptureArchiveRepository.inspect_association(
        output,
        capture_id,
    ) is not None


def test_uuid_missing_from_user_scope_is_local_success_not_cloud_failure(
    client,
    monkeypatch,
    tmp_path,
):
    capture_id = str(uuid.uuid4())
    _captures, _manual, output = _workspace(
        monkeypatch,
        tmp_path,
        (capture_id,),
    )
    owner_cfg = {"url": "https://project.invalid", "key": "owner-secret"}
    capture_cfg = {
        "url": "https://project.invalid",
        "access_token": "user-secret",
    }
    exits: list[str] = []
    monkeypatch.setattr(
        server,
        "_lease_cloud_cfg",
        lambda: _clearing_lease(owner_cfg, exits, "owner"),
    )
    monkeypatch.setattr(
        server,
        "_lease_capture_cfg",
        lambda: _clearing_lease(capture_cfg, exits, "capture"),
    )
    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        lambda _cfg, _ids: [],
    )
    monkeypatch.setattr(
        server.sbase,
        "ScopedCaptureLibAssociationPublisher",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("an RLS-hidden row constructed a publisher")
        ),
    )

    response = client.post(
        "/api/v1/capture-archive-backfills",
        json={"capture_ids": [capture_id], "apply": True},
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["cloud"]["status"] == "complete"
    assert body["cloud"]["authorized_count"] == 0
    assert body["cloud"]["unpublished_candidate_count"] == 1
    assert body["report"]["summary"]["created"] == 1
    assert FilesystemCaptureArchiveRepository.inspect_association(
        output,
        capture_id,
    ) is not None
    assert exits == ["owner", "capture"]
    assert owner_cfg == {}
    assert capture_cfg == {}


def test_cloud_scope_failure_is_generic_and_keeps_local_report(
    client,
    monkeypatch,
    tmp_path,
):
    capture_id = str(uuid.uuid4())
    _workspace(monkeypatch, tmp_path, (capture_id,))
    owner_cfg = {"url": "https://private.invalid", "key": "owner-secret"}
    capture_cfg = {
        "url": "https://private.invalid",
        "access_token": "user-secret",
    }
    exits: list[str] = []
    monkeypatch.setattr(
        server,
        "_lease_cloud_cfg",
        lambda: _clearing_lease(owner_cfg, exits, "owner"),
    )
    monkeypatch.setattr(
        server,
        "_lease_capture_cfg",
        lambda: _clearing_lease(capture_cfg, exits, "capture"),
    )

    def unavailable(*_args):
        raise server.sbase.SyncError(
            "HTTP 503 at https://private.invalid?token=owner-secret"
        )

    monkeypatch.setattr(
        server.sbase,
        "list_capture_association_states",
        unavailable,
    )

    response = client.post(
        "/api/v1/capture-archive-backfills",
        json={"capture_ids": [capture_id], "apply": True},
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["report"]["ok"] is True
    assert body["report"]["summary"]["created"] == 1
    assert body["cloud"] == {
        "status": "failed",
        "code": "capture_cloud_scope_discovery_failed",
        "message": "authorized capture scope could not be read",
        "candidate_count": 1,
        "local_only_count": 0,
        "authorized_count": 0,
        "published": 0,
        "failed": 0,
        "unpublished_candidate_count": 1,
    }
    encoded = json.dumps(body)
    assert "private.invalid" not in encoded
    assert "owner-secret" not in encoded
    assert exits == ["owner", "capture"]


def test_credential_lease_failure_still_runs_local_phase_and_releases_prior_lease(
    client,
    monkeypatch,
    tmp_path,
):
    capture_id = str(uuid.uuid4())
    _captures, _manual, output = _workspace(
        monkeypatch,
        tmp_path,
        (capture_id,),
    )
    capture_cfg = {
        "url": "https://private.invalid",
        "access_token": "user-secret",
    }
    exits: list[str] = []
    monkeypatch.setattr(
        server,
        "_lease_capture_cfg",
        lambda: _clearing_lease(capture_cfg, exits, "capture"),
    )

    @contextlib.contextmanager
    def unavailable_owner():
        raise RuntimeError("secret lease unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(server, "_lease_cloud_cfg", unavailable_owner)

    response = client.post(
        "/api/v1/capture-archive-backfills",
        json={"capture_ids": [capture_id], "apply": True},
    )

    body = response.get_json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["cloud"]["code"] == "capture_cloud_scope_discovery_failed"
    assert body["report"]["ok"] is True
    assert body["report"]["summary"]["created"] == 1
    assert FilesystemCaptureArchiveRepository.inspect_association(
        output,
        capture_id,
    ) is not None
    assert capture_cfg == {}
    assert exits == ["capture"]


def test_local_setup_failure_is_generic_at_authenticated_boundary(
):
    report = server._capture_archive_backfill_failure_report(
        apply=True,
        error=RuntimeError("owner-secret at https://private.invalid"),
    )

    assert report["ok"] is False
    assert report["diagnostics"][0]["message"] == (
        "capture backfill setup failed"
    )
    encoded = json.dumps(report)
    assert "owner-secret" not in encoded
    assert "private.invalid" not in encoded


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "invalid_capture_archive_backfill_scope"),
        (
            {"capture_ids": ["capture-a", "capture-a"]},
            "duplicate_capture_archive_backfill_scope",
        ),
        (
            {"capture_ids": ["CAPTURE-UPPER"]},
            "invalid_capture_archive_backfill_scope",
        ),
        (
            {"capture_ids": ["capture-a"], "apply": "true"},
            "invalid_capture_archive_backfill_mode",
        ),
        (
            {"capture_ids": ["capture-a"], "unexpected": True},
            "invalid_capture_archive_backfill_request",
        ),
        (
            {
                "capture_ids": [
                    f"capture-{index}"
                    for index in range(
                        server._CAPTURE_ARCHIVE_BACKFILL_MAX_IDS + 1
                    )
                ]
            },
            "capture_archive_backfill_scope_too_large",
        ),
    ],
)
def test_request_scope_fails_closed(client, monkeypatch, payload, code):
    monkeypatch.setattr(
        server,
        "_run_capture_archive_backfill_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid request reached the backfill host")
        ),
    )

    response = client.post(
        "/api/v1/capture-archive-backfills",
        json=payload,
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == code


def test_mutation_is_explicit_and_form_posts_are_rejected(client, monkeypatch):
    calls = []

    def run(capture_ids, *, apply):
        calls.append((capture_ids, apply))
        return {"ok": True, "mode": "dry-run"}

    monkeypatch.setattr(server, "_run_capture_archive_backfill_host", run)
    dry_run = client.post(
        "/api/v1/capture-archive-backfills",
        json={"capture_ids": ["capture-a"]},
    )
    form = client.post(
        "/api/v1/capture-archive-backfills",
        data={"capture_ids": "capture-a", "apply": "true"},
    )

    assert dry_run.status_code == 200
    assert calls == [(('capture-a',), False)]
    assert form.status_code == 400
    assert form.get_json()["code"] == (
        "invalid_capture_archive_backfill_request"
    )


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (
            b'{"capture_ids":["capture-a"],"apply":false,"apply":true}',
            "invalid_capture_archive_backfill_request",
        ),
        (
            b'{"capture_ids":["capture-a"],"apply":NaN}',
            "invalid_capture_archive_backfill_request",
        ),
        (b'{"capture_ids":[true]}', "invalid_capture_archive_backfill_scope"),
        (
            b'{"capture_ids":["capture-a",1]}',
            "invalid_capture_archive_backfill_scope",
        ),
    ],
)
def test_noncanonical_json_never_reaches_backfill_host(
    client,
    monkeypatch,
    document,
    code,
):
    monkeypatch.setattr(
        server,
        "_run_capture_archive_backfill_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid request reached the backfill host")
        ),
    )

    response = client.post(
        "/api/v1/capture-archive-backfills",
        data=document,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == code


def test_excessively_nested_json_fails_closed(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_run_capture_archive_backfill_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid request reached the backfill host")
        ),
    )
    document = (
        '{"capture_ids":'
        + "[" * 1200
        + '"capture-a"'
        + "]" * 1200
        + "}"
    ).encode("utf-8")

    response = client.post(
        "/api/v1/capture-archive-backfills",
        data=document,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == (
        "invalid_capture_archive_backfill_request"
    )


def test_json_depth_limit_is_independent_of_the_platform_decoder():
    value = "capture-a"
    for _ in range(server._CAPTURE_ARCHIVE_BACKFILL_MAX_JSON_DEPTH + 1):
        value = [value]

    assert server._capture_archive_backfill_json_too_deep(
        {"capture_ids": value}
    ) is True
    assert server._capture_archive_backfill_json_too_deep(
        {"capture_ids": ["capture-a"], "apply": True}
    ) is False


def test_packaged_transport_rejects_missing_capability_and_foreign_origin(
    client,
    monkeypatch,
):
    capability = "A" * 43
    host = "127.0.0.1:45678"
    origin = f"http://{host}"
    monkeypatch.setattr(
        server,
        "_DESKTOP_CAPABILITY_DIGEST",
        hashlib.sha256(capability.encode("ascii")).digest(),
    )
    monkeypatch.setattr(server, "_DESKTOP_EXPECTED_HOST", host)
    monkeypatch.setattr(server, "_DESKTOP_EXPECTED_ORIGIN", origin)
    monkeypatch.setattr(
        server,
        "_run_capture_archive_backfill_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unauthenticated request reached the host")
        ),
    )
    payload = {"capture_ids": ["capture-a"], "apply": True}

    missing = client.post(
        "/api/v1/capture-archive-backfills",
        json=payload,
        headers={"Host": host, "Origin": origin},
    )
    foreign = client.post(
        "/api/v1/capture-archive-backfills",
        json=payload,
        headers={
            "Host": host,
            "Origin": "https://attacker.invalid",
            "X-WHL-Desktop-Capability": capability,
        },
    )

    assert missing.status_code == 401
    assert foreign.status_code == 403


def test_concurrent_backfill_is_rejected_before_work_starts(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "_run_capture_archive_backfill_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("busy request reached the backfill host")
        ),
    )
    assert server._capture_archive_backfill_lock.acquire(blocking=False)
    try:
        response = client.post(
            "/api/v1/capture-archive-backfills",
            json={"capture_ids": ["capture-a"]},
        )
    finally:
        server._capture_archive_backfill_lock.release()

    assert response.status_code == 409
    assert response.get_json()["code"] == "capture_archive_backfill_busy"
