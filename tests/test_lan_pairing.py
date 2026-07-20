"""Authenticated Android-to-desktop LAN pairing and capture receipts."""

import contextlib
import io
import inspect


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
        lambda cap, photos, _key, names: ("manual-generated-id", []),
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
    }
