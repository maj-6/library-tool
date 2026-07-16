from __future__ import annotations

import json


def _signed_in(monkeypatch, server):
    monkeypatch.setattr(server, "_auth_cfg", lambda: {
        "url": "https://example.supabase.co", "key": "anon",
    })
    monkeypatch.setattr(server, "_auth_session", lambda: {
        "access_token": "token", "user_id": "user-1",
    })


def test_activity_push_preserves_expandable_detail(monkeypatch):
    import server

    _signed_in(monkeypatch, server)
    auth_doc = {"push_cursor": 0}
    monkeypatch.setattr(server, "_auth_doc", lambda: auth_doc)
    monkeypatch.setattr(server.lib, "save_json", lambda _path, _doc: None)
    monkeypatch.setattr(server, "_activity_lines", lambda: [json.dumps({
        "ts": "2026-07-15T12:00:00+00:00",
        "actor": "Ada",
        "verb": "added",
        "subject": "Checked Books",
        "n": 2,
        "detail": "Materia Medica; Medical Botany",
    })])
    calls = []

    def fake_rest(cfg, token, method, path, payload=None, prefer="", timeout=0):
        calls.append((cfg, token, method, path, payload, prefer))

    monkeypatch.setattr(server.sauth, "rest", fake_rest)

    server._push_events_once()

    assert calls[0][2:4] == ("POST", "events")
    assert calls[0][4][0]["detail"] == "Materia Medica; Medical Botany"
    assert auth_doc["push_cursor"] == 1


def test_cloud_activity_feed_requests_and_returns_detail(monkeypatch):
    import server

    _signed_in(monkeypatch, server)
    server._cloud_feed_cache.update(at=0.0, rows=[], fail_at=0.0)
    paths = []

    def fake_rest(_cfg, _token, method, path, **_kwargs):
        paths.append((method, path))
        return [{
            "at": "2026-07-15T12:00:00+00:00",
            "actor": "Ada",
            "verb": "added",
            "subject": "Checked Books",
            "n": 2,
            "detail": "Materia Medica; Medical Botany",
        }]

    monkeypatch.setattr(server.sauth, "rest", fake_rest)

    rows = server._cloud_events(20)

    assert paths[0][0] == "GET"
    assert "select=at,actor,verb,subject,n,detail" in paths[0][1]
    assert rows[0]["detail"] == "Materia Medica; Medical Botany"
