"""Versioned HTTP transport for recoverable item lifecycle commands."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from librarytool.engine.item_lifecycle import (
    DeleteItemCommand,
    ItemLifecycleReceipt,
    ItemLifecycleResult,
    ItemLifecycleState,
    ItemTombstoneSnapshot,
    LifecycleItemSnapshot,
    ManagedTreeSnapshot,
    RestoreItemCommand,
)


ITEM_REVISION = "item/revision=1,blue"
TREE_REVISION = "managed-tree-v1-deadbeef"
DELETED_TOMBSTONE_REVISION = "ltr-delete-1"
RESTORED_ITEM_REVISION = "item-revision-2"
RESTORED_TOMBSTONE_REVISION = "ltr-restored-2"
TOMBSTONE_ID = "ilt-delete-1"


def _tombstone(*, restored: bool = False) -> ItemTombstoneSnapshot:
    return ItemTombstoneSnapshot(
        tombstone_id=TOMBSTONE_ID,
        revision=(
            RESTORED_TOMBSTONE_REVISION
            if restored
            else DELETED_TOMBSTONE_REVISION
        ),
        state="restored" if restored else "deleted",
        item_id="book-one",
        deleted_item_revision=ITEM_REVISION,
        managed_tree_revision=TREE_REVISION,
        restored_item_revision=(RESTORED_ITEM_REVISION if restored else ""),
    )


class LifecycleStub:
    def __init__(self) -> None:
        self.inspect_calls: list[str] = []
        self.delete_calls: list[DeleteItemCommand] = []
        self.restore_calls: list[RestoreItemCommand] = []
        self.tombstone_reads: list[str] = []
        self.list_calls = 0

    def inspect(self, item_id: str) -> ItemLifecycleState:
        self.inspect_calls.append(item_id)
        return ItemLifecycleState(
            item=LifecycleItemSnapshot(item_id=item_id, revision=ITEM_REVISION),
            managed_tree=ManagedTreeSnapshot(
                item_id=item_id,
                revision=TREE_REVISION,
            ),
        )

    def delete(self, command: DeleteItemCommand) -> ItemLifecycleResult:
        self.delete_calls.append(command)
        tombstone = ItemTombstoneSnapshot(
            tombstone_id=TOMBSTONE_ID,
            revision=DELETED_TOMBSTONE_REVISION,
            state="deleted",
            item_id=command.item_id,
            deleted_item_revision=command.expected_item_revision,
            managed_tree_revision=command.expected_managed_tree_revision,
        )
        receipt = ItemLifecycleReceipt(
            action="delete",
            operation_id=command.operation_id,
            command_sha256="d" * 64,
            item_id=command.item_id,
            deleted_item_revision=command.expected_item_revision,
            restored_item_revision="",
            managed_tree_revision=command.expected_managed_tree_revision,
            tombstone_before_revision="",
            tombstone=tombstone,
        )
        return ItemLifecycleResult(
            receipt,
            replayed=command.operation_id == "delete-op-replay",
        )

    def get_tombstone(self, tombstone_id: str) -> ItemTombstoneSnapshot:
        self.tombstone_reads.append(tombstone_id)
        return _tombstone()

    def list_tombstones(self) -> tuple[ItemTombstoneSnapshot, ...]:
        self.list_calls += 1
        return (_tombstone(), _tombstone(restored=True))

    def restore(self, command: RestoreItemCommand) -> ItemLifecycleResult:
        self.restore_calls.append(command)
        receipt = ItemLifecycleReceipt(
            action="restore",
            operation_id=command.operation_id,
            command_sha256="e" * 64,
            item_id="book-one",
            deleted_item_revision=ITEM_REVISION,
            restored_item_revision=RESTORED_ITEM_REVISION,
            managed_tree_revision=TREE_REVISION,
            tombstone_before_revision=command.expected_tombstone_revision,
            tombstone=_tombstone(restored=True),
        )
        return ItemLifecycleResult(
            receipt,
            replayed=command.operation_id == "restore-op-replay",
        )


@dataclass
class EngineStub:
    service: LifecycleStub | None

    def get_service(self, key):
        return self.service


@pytest.fixture()
def lifecycle_http(client, monkeypatch):
    import server

    service = LifecycleStub()
    engine = EngineStub(service)
    monkeypatch.setattr(server, "_library_engine", lambda: engine)
    return server, client, service, engine


def _delete_headers(**changes: str) -> dict[str, str]:
    headers = {
        "Idempotency-Key": "delete-op-1",
        "If-Record-Match": f'"{ITEM_REVISION}"',
        "If-Managed-Tree-Match": f'"{TREE_REVISION}"',
    }
    headers.update(changes)
    return headers


def _restore_headers(**changes: str) -> dict[str, str]:
    headers = {
        "Idempotency-Key": "restore-op-1",
        "If-Tombstone-Match": f'"{DELETED_TOMBSTONE_REVISION}"',
    }
    headers.update(changes)
    return headers


def test_lifecycle_preflight_is_flattened_revisioned_and_revalidatable(
    lifecycle_http,
):
    _server, client, service, _engine = lifecycle_http

    response = client.get("/api/v1/items/book-one/lifecycle")

    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "ok": True,
        "schema": "librarytool.item-lifecycle-state/1",
        "state": "live",
        "item_id": "book-one",
        "item_revision": ITEM_REVISION,
        "managed_tree_revision": TREE_REVISION,
        "revision": body["revision"],
    }
    assert body["revision"].startswith("il-")
    assert response.headers["ETag"] == f'"{body["revision"]}"'
    assert response.headers["X-Record-Revision"] == ITEM_REVISION
    assert response.headers["X-Managed-Tree-Revision"] == TREE_REVISION
    assert response.headers["Cache-Control"] == "no-cache"

    unchanged = client.get(
        "/api/v1/items/book-one/lifecycle",
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert unchanged.status_code == 304
    assert unchanged.get_data() == b""
    assert service.inspect_calls == ["book-one", "book-one"]


def test_delete_transports_dual_cas_and_only_the_public_receipt(
    lifecycle_http,
):
    _server, client, service, _engine = lifecycle_http

    response = client.delete(
        "/api/v1/items/book-one",
        headers=_delete_headers(),
    )

    assert response.status_code == 200
    assert service.delete_calls == [DeleteItemCommand(
        item_id="book-one",
        expected_item_revision=ITEM_REVISION,
        expected_managed_tree_revision=TREE_REVISION,
        operation_id="delete-op-1",
    )]
    body = response.get_json()
    assert body["schema"] == "librarytool.item-lifecycle-receipt/1"
    assert body["replayed"] is False
    assert body["receipt"]["action"] == "delete"
    assert body["receipt"]["tombstone"] == _tombstone().as_dict()
    assert "command_sha256" not in body["receipt"]
    assert "storage" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Record-Revision"] == ITEM_REVISION
    assert response.headers["X-Managed-Tree-Revision"] == TREE_REVISION
    assert response.headers["X-Tombstone-Revision"] == (
        DELETED_TOMBSTONE_REVISION
    )
    assert response.headers["Location"] == (
        f"/api/v1/item-tombstones/{TOMBSTONE_ID}"
    )


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 428, "idempotency_key_required"),
        (
            {"Idempotency-Key": "delete-op-1"},
            428,
            "item_revision_required",
        ),
        (
            {
                "Idempotency-Key": "delete-op-1",
                "If-Record-Match": f'"{ITEM_REVISION}"',
            },
            428,
            "managed_tree_revision_required",
        ),
        (
            _delete_headers(**{"If-Record-Match": f'W/"{ITEM_REVISION}"'}),
            400,
            "invalid_item_revision",
        ),
        (
            _delete_headers(**{"If-Record-Match": '"one", "two"'}),
            400,
            "invalid_item_revision",
        ),
        (
            _delete_headers(**{"If-Managed-Tree-Match": TREE_REVISION}),
            400,
            "invalid_managed_tree_revision",
        ),
    ],
)
def test_delete_requires_exact_header_preconditions(
    lifecycle_http,
    headers,
    status,
    code,
):
    _server, client, service, _engine = lifecycle_http

    response = client.delete("/api/v1/items/book-one", headers=headers)

    assert response.status_code == status
    assert response.get_json()["code"] == code
    assert service.delete_calls == []


def test_delete_rejects_a_body_before_dispatch(lifecycle_http):
    _server, client, service, _engine = lifecycle_http

    response = client.delete(
        "/api/v1/items/book-one",
        data=b"{}",
        content_type="application/json",
        headers=_delete_headers(),
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "item_lifecycle_body_not_allowed"
    assert service.delete_calls == []


def test_tombstone_reads_expose_only_public_snapshots(lifecycle_http):
    _server, client, service, _engine = lifecycle_http

    collection = client.get("/api/v1/item-tombstones")
    detail = client.get(f"/api/v1/item-tombstones/{TOMBSTONE_ID}")

    assert collection.status_code == detail.status_code == 200
    assert collection.get_json() == {
        "ok": True,
        "schema": "librarytool.item-tombstone-list/1",
        "tombstones": [
            _tombstone().as_dict(),
            _tombstone(restored=True).as_dict(),
        ],
    }
    assert collection.headers["Cache-Control"] == "no-cache"
    assert detail.get_json() == {
        "ok": True,
        "schema": "librarytool.item-tombstone/1",
        "tombstone": _tombstone().as_dict(),
    }
    assert detail.headers["ETag"] == f'"{DELETED_TOMBSTONE_REVISION}"'
    assert detail.headers["X-Tombstone-Revision"] == (
        DELETED_TOMBSTONE_REVISION
    )
    assert "command_sha256" not in detail.get_data(as_text=True)
    assert service.list_calls == 1
    assert service.tombstone_reads == [TOMBSTONE_ID]

    unchanged = client.get(
        f"/api/v1/item-tombstones/{TOMBSTONE_ID}",
        headers={"If-None-Match": detail.headers["ETag"]},
    )
    assert unchanged.status_code == 304


def test_tombstone_list_supports_one_exact_state_filter(lifecycle_http):
    _server, client, service, _engine = lifecycle_http

    deleted = client.get("/api/v1/item-tombstones?state=deleted")
    invalid = client.get("/api/v1/item-tombstones?state=active")
    ambiguous = client.get(
        "/api/v1/item-tombstones?state=deleted&state=restored"
    )

    assert deleted.status_code == 200
    assert deleted.get_json()["tombstones"] == [_tombstone().as_dict()]
    assert invalid.status_code == ambiguous.status_code == 400
    assert invalid.get_json()["code"] == "invalid_item_tombstone_filter"
    assert ambiguous.get_json()["code"] == "invalid_item_tombstone_filter"
    assert service.list_calls == 1


def test_restore_returns_created_then_replayed_public_receipts(lifecycle_http):
    _server, client, service, _engine = lifecycle_http
    url = f"/api/v1/item-tombstones/{TOMBSTONE_ID}/restore"

    created = client.post(url, headers=_restore_headers())
    replayed = client.post(
        url,
        headers=_restore_headers(**{"Idempotency-Key": "restore-op-replay"}),
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert created.get_json()["replayed"] is False
    assert replayed.get_json()["replayed"] is True
    assert created.get_json()["schema"] == (
        "librarytool.item-lifecycle-receipt/1"
    )
    assert "command_sha256" not in created.get_json()["receipt"]
    assert created.get_json()["receipt"]["tombstone_before_revision"] == (
        DELETED_TOMBSTONE_REVISION
    )
    assert created.headers["Location"] == "/api/v1/items/book-one"
    assert created.headers["Cache-Control"] == "no-store"
    assert created.headers["X-Record-Revision"] == RESTORED_ITEM_REVISION
    assert created.headers["X-Managed-Tree-Revision"] == TREE_REVISION
    assert created.headers["X-Tombstone-Revision"] == (
        RESTORED_TOMBSTONE_REVISION
    )
    assert service.restore_calls[0] == RestoreItemCommand(
        tombstone_id=TOMBSTONE_ID,
        expected_tombstone_revision=DELETED_TOMBSTONE_REVISION,
        operation_id="restore-op-1",
    )


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 428, "idempotency_key_required"),
        (
            {"Idempotency-Key": "restore-op-1"},
            428,
            "tombstone_revision_required",
        ),
        (
            _restore_headers(**{
                "If-Tombstone-Match": f'W/"{DELETED_TOMBSTONE_REVISION}"'
            }),
            400,
            "invalid_tombstone_revision",
        ),
        (
            _restore_headers(**{
                "If-Tombstone-Match": DELETED_TOMBSTONE_REVISION
            }),
            400,
            "invalid_tombstone_revision",
        ),
    ],
)
def test_restore_requires_exact_header_preconditions(
    lifecycle_http,
    headers,
    status,
    code,
):
    _server, client, service, _engine = lifecycle_http
    response = client.post(
        f"/api/v1/item-tombstones/{TOMBSTONE_ID}/restore",
        headers=headers,
    )

    assert response.status_code == status
    assert response.get_json()["code"] == code
    assert service.restore_calls == []


def test_lifecycle_routes_fail_closed_when_module_is_not_installed(
    lifecycle_http,
):
    _server, client, service, engine = lifecycle_http
    engine.service = None

    responses = [
        client.get("/api/v1/items/book-one/lifecycle"),
        client.get("/api/v1/item-tombstones"),
        client.delete(
            "/api/v1/items/book-one",
            headers=_delete_headers(),
        ),
        client.post(
            f"/api/v1/item-tombstones/{TOMBSTONE_ID}/restore",
            headers=_restore_headers(),
        ),
    ]

    assert {response.status_code for response in responses} == {503}
    assert {
        response.get_json()["code"] for response in responses
    } == {"item_lifecycle_unavailable"}
    assert all(response.get_json()["retryable"] for response in responses)
    assert service.delete_calls == []
    assert service.restore_calls == []
