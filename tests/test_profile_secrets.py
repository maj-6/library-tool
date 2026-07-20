"""Account-owned Mistral sync operates directly on protected status/leases."""

from contextlib import contextmanager

from librarytool.engine.secret_store import SecretStatus


class _ProtectedMistral:
    def __init__(self, server, value="", revision="mistral-r1"):
        self.server = server
        self.value = value
        self.revision = revision
        self.pending = False
        self.service = self

    def get_status(self, secret_id):
        return SecretStatus(secret_id, bool(self.value), self.revision)

    def replace(self, command):
        assert command.secret_id == self.server._SECRET_IDS["mistralKey"]
        assert command.expected_revision == self.revision
        self.value = command.credential
        self.revision += "-next"

    def clear(self, command):
        assert command.expected_revision == self.revision
        self.value = ""
        self.revision += "-next"

    @contextmanager
    def lease(self, key):
        assert key == "mistralKey"
        assert self.value
        yield self.value


def _bind(monkeypatch, server, protected):
    monkeypatch.setattr(server, "_auth_cfg", lambda: {"url": "https://x", "key": "k"})
    monkeypatch.setattr(server, "_auth_session", lambda: {
        "user_id": "user-1", "access_token": "token",
    })
    monkeypatch.setattr(server, "_secret_service", lambda: protected.service)
    monkeypatch.setattr(server, "_lease_secret", protected.lease)
    monkeypatch.setattr(server, "_secret_sync_state", lambda: {
        "mistral_pending": True} if protected.pending else {})
    monkeypatch.setattr(server, "_set_mistral_pending",
                        lambda value: setattr(protected, "pending", bool(value)))


def test_profile_mistral_pull_replaces_protected_value(monkeypatch):
    import server

    protected = _ProtectedMistral(server, "old-local")
    _bind(monkeypatch, server, protected)

    def rest(_cfg, _token, method, path, *args, **kwargs):
        assert method == "GET"
        assert path == "profile_secrets?id=eq.user-1&select=api_keys,updated_at"
        return [{"api_keys": {"mistral": "from-cloud", "deepseek": "keep"},
                 "updated_at": "rev-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "from-cloud"
    assert protected.value == "from-cloud"
    assert protected.pending is False


def test_profile_mistral_pending_edit_merges_and_clears_marker(monkeypatch):
    import server

    protected = _ProtectedMistral(server, "new-local")
    protected.pending = True
    _bind(monkeypatch, server, protected)
    calls = []

    def rest(_cfg, _token, method, path, body=None, **kwargs):
        calls.append((method, path, body, kwargs))
        if method == "GET":
            return [{"api_keys": {"mistral": "old", "deepseek": "keep"},
                     "updated_at": "rev-1"}]
        return [{"id": "user-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "new-local"
    assert calls[1][0] == "PATCH"
    assert calls[1][2]["api_keys"] == {
        "mistral": "new-local", "deepseek": "keep",
    }
    assert protected.value == "new-local"
    assert protected.pending is False


def test_profile_mistral_offline_retains_pending_protected_edit(monkeypatch):
    import server

    protected = _ProtectedMistral(server, "offline-edit")
    protected.pending = True
    _bind(monkeypatch, server, protected)

    def unavailable(*args, **kwargs):
        raise server.sauth.AuthError("offline", status=503)

    monkeypatch.setattr(server.sauth, "rest", unavailable)
    assert server._sync_profile_mistral_key() is None
    assert protected.value == "offline-edit"
    assert protected.pending is True


def test_profile_sync_does_not_clear_newer_local_cas(monkeypatch):
    import server

    protected = _ProtectedMistral(server, "first-edit")
    protected.pending = True
    _bind(monkeypatch, server, protected)

    def rest(_cfg, _token, method, _path, body=None, **_kwargs):
        if method == "GET":
            return [{"api_keys": {}, "updated_at": "rev-1"}]
        assert body["api_keys"]["mistral"] == "first-edit"
        protected.value = "second-edit"
        protected.revision = "mistral-r2"
        protected.pending = True
        return [{"id": "user-1"}]

    monkeypatch.setattr(server.sauth, "rest", rest)
    assert server._sync_profile_mistral_key() == "first-edit"
    assert protected.value == "second-edit"
    assert protected.pending is True
