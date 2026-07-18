"""The account-first workbench: sign-in gate, roles, approval, profile.

The app is a shared tool — every action belongs to a member. These tests pin
the API-boundary enforcement (server._require_member and friends); the cloud
side of the same rules lives in docs/cloud/migrations/005_member_roles_approval.sql
and is exercised against the real project, not here.
"""
from __future__ import annotations

import json

import pytest

from conftest import TEST_SESSION, seed_session


def _auth_path(data_root):
    return data_root / "output" / "auth_session.json"


# --- the front door ---------------------------------------------------------------


def test_signed_out_workbench_is_shut(client, data_root):
    _auth_path(data_root).unlink()
    r = client.get("/api/reviews")
    assert r.status_code == 401
    assert r.get_json() == {"ok": False, "error": "signin_required",
                            "gate": "signin"}
    # the sign-in surface, the console feed and the page shell stay open
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/log").status_code == 200
    assert client.get("/").status_code == 200


def test_pending_account_waits_at_the_door(client):
    seed_session(role="guest", status="pending")
    r = client.get("/api/reviews")
    assert r.status_code == 403
    assert r.get_json()["error"] == "approval_pending"
    assert r.get_json()["gate"] == "pending"
    assert client.get("/api/auth/status").get_json()["gate"] == "pending"


def test_rejected_account_stays_out(client):
    seed_session(role="guest", status="rejected")
    r = client.get("/api/reviews")
    assert r.status_code == 403
    assert r.get_json()["gate"] == "rejected"


def test_pending_account_can_still_sign_out(client, monkeypatch):
    import server
    monkeypatch.setattr(server.sauth, "sign_out", lambda cfg, tok: None)
    seed_session(role="guest", status="pending")
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/status").get_json()["gate"] == "signin"


def test_guest_is_read_only(client):
    seed_session(role="guest")
    assert client.get("/api/reviews").status_code == 200
    r = client.post("/api/reviews", json={"kind": "row", "ref": "x"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "read_only"
    # device-local UI preferences are not library data
    assert client.put("/api/client_state",
                      json={"settings": {}}).status_code == 200


def test_contributor_works_normally(client):
    r = client.post("/api/reviews",
                    json={"kind": "row", "ref": "x", "label": "a review"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_legacy_session_counts_as_approved_contributor(client, data_root):
    # a session stored before roles existed carries neither key
    path = _auth_path(data_root)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["session"].pop("role")
    doc["session"].pop("status")
    path.write_text(json.dumps(doc), encoding="utf-8")
    s = client.get("/api/auth/status").get_json()
    assert (s["gate"], s["role"], s["status"]) == ("ok", "contributor", "approved")


def test_no_cloud_configuration_runs_ungated(client, data_root, monkeypatch):
    import server
    _auth_path(data_root).unlink()
    monkeypatch.setattr(server, "_auth_cfg", lambda: None)
    server._gate_memo.update(key=None, value=None)   # patched fn: drop the memo
    assert client.get("/api/reviews").status_code == 200
    s = client.get("/api/auth/status").get_json()
    assert s["cloud"] is False and s["gate"] == "ok"


# --- membership management --------------------------------------------------------


def test_members_requires_the_maintainer_role(client):
    assert client.get("/api/members").status_code == 403


def _fake_rest(calls, rows=None):
    def rest(cfg, token, method, path, payload=None, prefer="", timeout=0):
        calls.append((method, path, payload))
        return rows if rows is not None else None
    return rest


def test_members_endpoints_pass_through_to_the_rpcs(client, monkeypatch):
    import server
    seed_session(role="maintainer")
    monkeypatch.setattr(server, "_auth_session",
                        lambda: dict(TEST_SESSION, role="maintainer"))
    calls = []
    monkeypatch.setattr(server.sauth, "rest", _fake_rest(calls, rows=[
        {"id": "u2", "email": "x@y.z", "display_name": "X",
         "role": "guest", "status": "pending"}]))

    r = client.get("/api/members").get_json()
    assert r["ok"] is True and r["me"] == "test-user"
    assert r["members"][0]["email"] == "x@y.z"
    assert calls[0][:2] == ("POST", "rpc/member_directory")

    r = client.post("/api/members/u2/status",
                    json={"status": "approved", "label": "X"})
    assert r.get_json()["ok"] is True
    assert calls[-1] == ("POST", "rpc/set_member_status",
                         {"target": "u2", "new_status": "approved"})

    r = client.post("/api/members/u2/role", json={"role": "contributor"})
    assert r.get_json()["ok"] is True
    assert calls[-1] == ("POST", "rpc/set_member_role",
                         {"target": "u2", "new_role": "contributor"})


def test_member_updates_validate_role_and_status(client, monkeypatch):
    import server
    seed_session(role="maintainer")
    monkeypatch.setattr(server, "_auth_session",
                        lambda: dict(TEST_SESSION, role="maintainer"))
    monkeypatch.setattr(server.sauth, "rest",
                        lambda *a, **k: pytest.fail("must validate first"))
    assert client.post("/api/members/u2/role",
                       json={"role": "emperor"}).status_code == 400
    assert client.post("/api/members/u2/status",
                       json={"status": "vanished"}).status_code == 400


def test_members_offline_reads_as_unreachable_not_forbidden(client, monkeypatch):
    import server
    seed_session(role="maintainer")
    monkeypatch.setattr(server, "_auth_session",
                        lambda: dict(TEST_SESSION, role="maintainer"))

    def rest(*a, **k):
        raise server.sauth.AuthError("timed out")   # transport: status None
    monkeypatch.setattr(server.sauth, "rest", rest)
    r = client.get("/api/members")
    assert r.status_code == 503
    assert "unreachable" in r.get_json()["error"]


# --- own profile ------------------------------------------------------------------


def test_profile_me_serves_the_stored_identity_offline(client):
    seed_session(display_name="Ada", member_since="2026-01-02T00:00:00+00:00")
    p = client.get("/api/profile/me").get_json()
    assert p["email"] == "tester@example.com"
    assert p["display_name"] == "Ada"
    assert p["role"] == "contributor" and p["status"] == "approved"
    assert p["member_since"].startswith("2026-01-02")


def test_profile_rename_patches_the_cloud_and_the_stored_session(
        client, data_root, monkeypatch):
    import server
    monkeypatch.setattr(server, "_auth_session", lambda: dict(TEST_SESSION))
    calls = []
    monkeypatch.setattr(server.sauth, "rest", _fake_rest(calls))
    r = client.put("/api/profile/me", json={"display_name": "  Ada Lovelace  "})
    assert r.get_json() == {"ok": True, "display_name": "Ada Lovelace"}
    method, path, payload = calls[0]
    assert method == "PATCH"
    assert "profiles?id=eq.test-user" in path
    assert payload == {"display_name": "Ada Lovelace"}
    doc = json.loads(_auth_path(data_root).read_text(encoding="utf-8"))
    assert doc["session"]["display_name"] == "Ada Lovelace"


def test_profile_rename_offline_fails_honestly(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "_auth_session", lambda: dict(TEST_SESSION))

    def rest(*a, **k):
        raise server.sauth.AuthError("timed out")
    monkeypatch.setattr(server.sauth, "rest", rest)
    r = client.put("/api/profile/me", json={"display_name": "Ada"})
    assert r.status_code == 503
    assert "online" in r.get_json()["error"]


def test_password_change_verifies_the_current_password_first(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "_auth_session", lambda: dict(TEST_SESSION))

    def bad_sign_in(cfg, email, password):
        raise server.sauth.AuthError("Invalid login credentials", status=400)
    monkeypatch.setattr(server.sauth, "sign_in", bad_sign_in)
    monkeypatch.setattr(server.sauth, "update_user",
                        lambda *a, **k: pytest.fail("must not change password"))
    r = client.post("/api/profile/password",
                    json={"current_password": "wrong", "new_password": "hunter22"})
    assert r.status_code == 403
    assert "incorrect" in r.get_json()["error"]


def test_password_change_updates_and_discards_the_check_session(client, monkeypatch):
    import server
    monkeypatch.setattr(server, "_auth_session", lambda: dict(TEST_SESSION))
    monkeypatch.setattr(server.sauth, "sign_in",
                        lambda cfg, email, pw: {"access_token": "throwaway"})
    updates, outs = [], []
    monkeypatch.setattr(server.sauth, "update_user",
                        lambda cfg, tok, changes: updates.append((tok, changes)))
    monkeypatch.setattr(server.sauth, "sign_out",
                        lambda cfg, tok: outs.append(tok))
    r = client.post("/api/profile/password",
                    json={"current_password": "old", "new_password": "hunter22"})
    assert r.get_json() == {"ok": True}
    assert updates == [("test-token", {"password": "hunter22"})]
    assert outs == ["throwaway"]


# --- adoption + the activity mirror -----------------------------------------------


def test_adopt_profile_survives_a_pre_membership_cloud(monkeypatch):
    import server
    cfg = {"url": "https://x.supabase.co", "key": "anon"}

    def rest(_cfg, token, method, path, payload=None, prefer="", timeout=0):
        if "role" in path:
            raise server.sauth.AuthError(
                'column profiles.role does not exist', status=400)
        return [{"display_name": "Ada", "created_at": "2026-01-01T00:00:00+00:00"}]
    monkeypatch.setattr(server.sauth, "rest", rest)
    ses = server._adopt_profile(cfg, {"access_token": "t", "user_id": "u",
                                      "email": "ada@x.co", "display_name": ""})
    assert ses["display_name"] == "Ada"
    assert ses["role"] == "contributor" and ses["status"] == "approved"


def test_adopt_profile_reads_membership_when_present(monkeypatch):
    import server
    cfg = {"url": "https://x.supabase.co", "key": "anon"}
    monkeypatch.setattr(server.sauth, "rest", _fake_rest([], rows=[
        {"display_name": "New Person", "role": "guest", "status": "pending",
         "created_at": "2026-07-01T00:00:00+00:00"}]))
    ses = server._adopt_profile(cfg, {"access_token": "t", "user_id": "u",
                                      "email": "n@x.co", "display_name": ""})
    assert ses["role"] == "guest" and ses["status"] == "pending"


def test_activity_push_waits_for_approval(monkeypatch):
    import server
    monkeypatch.setattr(server, "_auth_cfg",
                        lambda: {"url": "https://x.supabase.co", "key": "anon"})
    monkeypatch.setattr(server.sauth, "rest",
                        lambda *a, **k: pytest.fail("must not push yet"))
    for role, status in (("contributor", "pending"), ("guest", "approved")):
        monkeypatch.setattr(server, "_auth_session", lambda r=role, s=status: {
            "access_token": "t", "user_id": "u", "role": r, "status": s})
        server._push_events_once()
