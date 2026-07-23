"""Authenticated Android-to-desktop LAN pairing and capture receipts."""

import contextlib
import io
import inspect
import json

import libcommon as lib
import pytest


CAPTURE_ID = "2ec86526-1133-4e74-a2c7-497886201d76"


def test_lan_listener_log_never_reads_or_prints_the_pairing_token():
    import server

    source = inspect.getsource(server._apply_lan_state)
    assert "_lan_token()" not in source
    assert "token=" not in source


def test_lan_pair_requires_token_and_echoes_fresh_nonce(monkeypatch, data_root):
    import server

    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    client = server.lan_app.test_client()
    nonce = "fresh-client-nonce-1234"

    assert client.post("/lan/pair", json={"nonce": nonce}).status_code == 401
    assert client.post(
        "/lan/pair",
        headers={"X-WHL-Token": "wrong"},
        json={"nonce": nonce},
    ).status_code == 401
    assert client.post(
        "/lan/pair",
        headers={"X-WHL-Token": "paired-secret"},
        json={"nonce": "short"},
    ).status_code == 400

    response = client.post(
        "/lan/pair",
        headers={"X-WHL-Token": "paired-secret"},
        json={"nonce": nonce},
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "app": "whl-capture",
        "authorized": True,
        "nonce": nonce,
    }


def test_lan_capture_returns_branded_matching_receipt(monkeypatch, data_root):
    import server

    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    monkeypatch.setattr(server, "_client_settings", lambda: {})
    monkeypatch.setattr(server, "_lease_secret", lambda key:
                        contextlib.nullcontext("leased-mistral")
                        if key == "mistralKey" else None)
    monkeypatch.setattr(
        server,
        "ingest_capture",
        lambda cap, photos, _key, names, **_kwargs: ("manual-generated-id", []),
    )
    association = {
        "capture_id": "entry-1",
        "book_id": "b-" + "1" * 32,
        "archive_sha256": "a" * 64,
        "archive_bytes": 123,
        "format_version": "3.0",
        "state": "current",
        "generated_at": "2026-07-23T00:00:00+00:00",
        "source_revision": "sha256:" + "b" * 64,
        "source_fingerprint": "c" * 64,
    }

    class Association:
        @staticmethod
        def as_dict():
            return dict(association)

    monkeypatch.setattr(
        server,
        "_capture_archive_association",
        lambda _capture_id: Association(),
    )
    client = server.lan_app.test_client()
    response = client.post(
        "/lan/capture",
        headers={"X-WHL-Token": "paired-secret"},
        data={
            "meta": '{"id":"entry-1"}',
            "photo": (io.BytesIO(b"jpeg"), "photo_1.jpg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "app": "whl-capture",
        "status": "imported",
        "id": "entry-1",
        "entry_id": "manual-generated-id",
        "lib_association": association,
    }


def test_lan_metadata_roundtrip_applies_and_acknowledges_phone_review(
        monkeypatch, data_root):
    import server

    root = data_root / "lan-metadata-roundtrip"
    root.mkdir(exist_ok=True)
    manual_path = root / "manual.json"
    reviews_path = root / "reviews.json"
    state_path = root / "capture-sync.json"
    identity_path = root / "lan-id.txt"
    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    monkeypatch.setattr(lib, "MANUAL_ENTRIES_PATH", manual_path)
    monkeypatch.setattr(server, "REVIEWS_PATH", reviews_path)
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    monkeypatch.setattr(server, "_LAN_ID_PATH", identity_path)
    lib.save_json(manual_path, {"manual-1": {
        "id": "manual-1", "title": "Herbal", "capture_id": CAPTURE_ID,
        "attention": "",
    }})
    payload = {
        "capture_ids": [CAPTURE_ID],
        "reviews": [{
            "schema": "org.whl.bookcapture.capture-review",
            "version": 1,
            "capture_id": CAPTURE_ID,
            "revision": 0,
            "updated_at": "",
            "needs_attention": True,
            "attention_reason": "Check edition",
            "needs_review": True,
            "review_id": "",
            "status": "",
        }],
    }
    client = server.lan_app.test_client()
    first = client.post(
        "/lan/metadata",
        headers={"X-WHL-Token": "paired-secret"},
        json=payload,
    )
    second = client.post(
        "/lan/metadata",
        headers={"X-WHL-Token": "paired-secret"},
        json={"capture_ids": [CAPTURE_ID], "reviews": []},
    )

    assert first.status_code == 200
    body = first.get_json()
    assert body["errors"] == []
    assert body["books"][0]["book_id"] == ""
    assert body["books"][0]["data"]["registered"] is False
    assert body["reviews"][0]["needs_review"] is True
    assert body["reviews"][0]["attention_reason"] == "Check edition"
    assert second.get_json()["books"][0]["revision"] == \
        body["books"][0]["revision"]
    assert second.get_json()["reviews"][0]["revision"] == \
        body["reviews"][0]["revision"]
    assert lib.load_json(manual_path, {})["manual-1"]["attention"] == \
        "Check edition"
    assert len(lib.load_json(reviews_path, {})) == 1


def test_lan_owner_identity_survives_pairing_token_rotation(monkeypatch, data_root):
    import server

    root = data_root / "lan-owner-identity"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(server, "_LAN_ID_PATH", root / "lan-id.txt")
    monkeypatch.setattr(server, "_lan_token", lambda: "first-token")
    first = server._lan_owner_id()
    monkeypatch.setattr(server, "_lan_token", lambda: "rotated-token")

    assert server._lan_owner_id() == first


def test_lan_identity_is_atomic_and_corruption_fails_closed(monkeypatch, data_root):
    import server

    root = data_root / "lan-owner-failure"
    root.mkdir(exist_ok=True)
    identity = root / "lan-id.txt"
    monkeypatch.setattr(server, "_LAN_ID_PATH", identity)
    strict_atomic_replace = lib._strict_atomic_replace
    monkeypatch.setattr(
        lib,
        "_strict_atomic_replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(RuntimeError, match="could not be stored"):
        server._lan_owner_id()

    monkeypatch.setattr(lib, "_strict_atomic_replace", strict_atomic_replace)
    identity.write_text("not-a-uuid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="identity is invalid"):
        server._lan_owner_id()


def test_lan_snapshot_loss_rotates_stream_and_retains_only_fingerprints(
        monkeypatch, data_root):
    import server

    root = data_root / "lan-ledger-reset"
    root.mkdir(exist_ok=True)
    state_path = root / "capture-sync.json"
    identity_path = root / "lan-id.txt"
    monkeypatch.setattr(server, "CAPTURE_PHONE_SYNC_STATE_PATH", state_path)
    monkeypatch.setattr(server, "_LAN_ID_PATH", identity_path)
    monkeypatch.setattr(server, "_current_capture_targets", lambda: {})

    first_books, _first_reviews, first_errors = \
        server._lan_versioned_capture_rows([CAPTURE_ID])
    first_owner = first_books[0]["owner_id"]
    state = lib.load_json(state_path, {})
    snapshot = state["lan_snapshots"][CAPTURE_ID]["book"]

    assert first_errors == []
    assert set(snapshot) == {
        "fingerprint", "revision", "updated_at", "last_seen_at",
    }
    assert "data" not in json.dumps(snapshot)
    state_path.unlink()

    second_books, _second_reviews, second_errors = \
        server._lan_versioned_capture_rows([CAPTURE_ID])
    assert second_errors == []
    assert second_books[0]["owner_id"] != first_owner
    assert second_books[0]["revision"] == 1


def test_lan_request_and_photo_limits_are_enforced(monkeypatch):
    import server

    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    client = server.lan_app.test_client()
    oversized = b" " * (server.LAN_METADATA_MAX_REQUEST_BYTES + 1)
    response = client.post(
        "/lan/metadata",
        headers={"X-WHL-Token": "paired-secret"},
        data=oversized,
        content_type="application/json",
    )
    assert response.status_code == 413
    assert server._read_lan_photo(io.BytesIO(b"abcd"), maximum=4) == b"abcd"
    with pytest.raises(ValueError, match="size limit"):
        server._read_lan_photo(io.BytesIO(b"abcde"), maximum=4)


def test_lan_metadata_rejects_boolean_review_version(monkeypatch):
    import server

    monkeypatch.setattr(server, "_lan_token", lambda: "paired-secret")
    client = server.lan_app.test_client()
    response = client.post(
        "/lan/metadata",
        headers={"X-WHL-Token": "paired-secret"},
        json={
            "capture_ids": [CAPTURE_ID],
            "reviews": [{
                "schema": "org.whl.bookcapture.capture-review",
                "version": True,
                "capture_id": CAPTURE_ID,
                "needs_attention": True,
                "attention_reason": "Check",
                "needs_review": False,
            }],
        },
    )

    assert response.status_code == 200
    assert response.get_json()["errors"][0]["capture_id"] == CAPTURE_ID
