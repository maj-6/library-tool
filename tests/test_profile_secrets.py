"""Desktop synchronization of the account-owned Mistral API key."""


def _session(monkeypatch, server):
    monkeypatch.setattr(server, "_auth_cfg", lambda: {"url": "https://x", "key": "k"})
    monkeypatch.setattr(server, "_auth_session", lambda: {
        "user_id": "user-1", "access_token": "token",
    })


def test_profile_mistral_pull_preserves_local_secrets(monkeypatch, data_root):
    import server

    _session(monkeypatch, server)
    server._save_secrets({"mistralKey": "old-local", "aiKey": "local-only"})

    def rest(_cfg, _token, method, path, *args, **kwargs):
        assert method == "GET"
        assert path == "profile_secrets?id=eq.user-1&select=api_keys"
        return [{"api_keys": {"mistral": "from-cloud", "deepseek": "keep-me"}}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "from-cloud"
    assert server._load_secrets() == {
        "mistralKey": "from-cloud", "aiKey": "local-only",
    }


def test_profile_mistral_pending_edit_merges_and_upserts(monkeypatch, data_root):
    import server

    _session(monkeypatch, server)
    server._save_secrets({
        "mistralKey": "new-local", server._MISTRAL_PENDING: True,
    })
    calls = []

    def rest(_cfg, _token, method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "GET":
            return [{"api_keys": {"mistral": "old-cloud", "deepseek": "keep-me"}}]
        return None

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "new-local"
    assert calls[1][0:3] == (
        "POST",
        "profile_secrets?on_conflict=id",
        [{"id": "user-1", "api_keys": {
            "mistral": "new-local", "deepseek": "keep-me",
        }}],
    )
    assert server._load_secrets() == {"mistralKey": "new-local"}


def test_profile_mistral_offline_keeps_pending_edit(monkeypatch, data_root):
    import server

    _session(monkeypatch, server)
    server._save_secrets({
        "mistralKey": "offline-edit", server._MISTRAL_PENDING: True,
    })

    def unavailable(*args, **kwargs):
        raise server.sauth.AuthError("offline", status=503)

    monkeypatch.setattr(server.sauth, "rest", unavailable)
    assert server._sync_profile_mistral_key() is None
    assert server._load_secrets() == {
        "mistralKey": "offline-edit", server._MISTRAL_PENDING: True,
    }
