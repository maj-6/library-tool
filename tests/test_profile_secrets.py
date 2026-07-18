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
        assert path == "profile_secrets?id=eq.user-1&select=api_keys,updated_at"
        return [{"api_keys": {"mistral": "from-cloud", "deepseek": "keep-me"},
                 "updated_at": "2026-07-17T12:00:00+00:00"}]

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
            return [{"api_keys": {"mistral": "old-cloud", "deepseek": "keep-me"},
                     "updated_at": "2026-07-17T12:00:00+00:00"}]
        return [{"id": "user-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "new-local"
    assert calls[1][0:3] == (
        "PATCH",
        "profile_secrets?id=eq.user-1&updated_at=eq."
        "2026-07-17T12%3A00%3A00%2B00%3A00",
        {"api_keys": {
            "mistral": "new-local", "deepseek": "keep-me",
        }, "updated_at": calls[1][2]["updated_at"]},
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


def test_profile_mistral_conflict_rereads_and_preserves_newer_android_key(
        monkeypatch, data_root):
    import server

    _session(monkeypatch, server)
    server._save_secrets({
        "mistralKey": "desktop-mistral", server._MISTRAL_PENDING: True,
    })
    gets = iter([
        {"api_keys": {"mistral": "old", "deepseek": "old-deepseek"},
         "updated_at": "rev-1"},
        {"api_keys": {"mistral": "android-mistral", "deepseek": "android-deepseek"},
         "updated_at": "rev-2"},
    ])
    patches = []

    def rest(_cfg, _token, method, path, body=None, **kwargs):
        if method == "GET":
            return [next(gets)]
        patches.append((path, body, kwargs))
        return [] if len(patches) == 1 else [{"id": "user-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "desktop-mistral"
    assert len(patches) == 2
    assert patches[1][1]["api_keys"] == {
        "mistral": "desktop-mistral",
        "deepseek": "android-deepseek",
    }
    assert server._load_secrets() == {"mistralKey": "desktop-mistral"}


def test_profile_mistral_sync_does_not_clear_newer_local_edit(
        monkeypatch, data_root):
    import server

    _session(monkeypatch, server)
    server._save_secrets({
        "mistralKey": "first-edit", server._MISTRAL_PENDING: True,
    })

    def rest(_cfg, _token, method, _path, body=None, **_kwargs):
        if method == "GET":
            return [{"api_keys": {"deepseek": "keep-me"},
                     "updated_at": "rev-1"}]
        assert body["api_keys"]["mistral"] == "first-edit"
        # Simulate a Settings save while the older PATCH is in flight.
        server._save_secrets({
            "mistralKey": "second-edit", server._MISTRAL_PENDING: True,
        })
        return [{"id": "user-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "first-edit"
    assert server._load_secrets() == {
        "mistralKey": "second-edit", server._MISTRAL_PENDING: True,
    }
