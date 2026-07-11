"""The shipped cloud identity (tools/cloud_defaults.py) and its fallbacks.

A fresh install must reach the cloud with zero configuration, Settings must
still override everything, and — the one that really matters — the baked-in
key must be the PUBLIC anon key. If a service key ever lands in
cloud_defaults, the role check here goes red.
"""
from __future__ import annotations

import base64
import json

import cloud_defaults
import libcommon as lib
import pytest
import server


def _jwt_payload(key: str) -> dict:
    body = key.split(".")[1]
    body += "=" * (-len(body) % 4)
    return json.loads(base64.urlsafe_b64decode(body))


def test_shipped_key_is_the_anon_role():
    payload = _jwt_payload(cloud_defaults.SUPABASE_ANON_KEY)
    assert payload["role"] == "anon"
    assert payload["ref"] in cloud_defaults.SUPABASE_URL


def test_shipped_url_shape():
    assert cloud_defaults.SUPABASE_URL.startswith("https://")
    assert not cloud_defaults.SUPABASE_URL.endswith("/")


@pytest.fixture()
def settings():
    """Write settings for the test, restore the previous state after."""
    state = lib.load_json(lib.CLIENT_STATE_PATH, {})
    before = json.dumps(state)

    def put(**kw):
        doc = lib.load_json(lib.CLIENT_STATE_PATH, {})
        doc["settings"] = dict(doc.get("settings") or {}, **kw)
        lib.save_json(lib.CLIENT_STATE_PATH, doc)
    yield put
    lib.save_json(lib.CLIENT_STATE_PATH, json.loads(before))


def test_auth_cfg_works_with_nothing_configured(settings):
    settings(supabaseUrl="", supabaseAnonKey="", supabaseKey="")
    cfg = server._auth_cfg()
    assert cfg == {"url": cloud_defaults.SUPABASE_URL,
                   "key": cloud_defaults.SUPABASE_ANON_KEY}


def test_auth_cfg_settings_override_the_defaults(settings):
    settings(supabaseUrl="https://own.supabase.co", supabaseAnonKey="own-anon")
    assert server._auth_cfg() == {"url": "https://own.supabase.co",
                                  "key": "own-anon"}


def test_auth_cfg_never_pairs_the_default_key_with_a_custom_url(settings):
    settings(supabaseUrl="https://own.supabase.co", supabaseAnonKey="",
             supabaseKey="")
    assert server._auth_cfg() is None


def test_auth_cfg_service_key_alone_rides_the_default_url(settings):
    # the owner's historical setup: only the service key pasted
    settings(supabaseUrl="", supabaseAnonKey="", supabaseKey="service-secret")
    assert server._auth_cfg() == {"url": cloud_defaults.SUPABASE_URL,
                                  "key": "service-secret"}


def test_cloud_cfg_still_requires_the_service_key(settings):
    settings(supabaseUrl="", supabaseAnonKey="", supabaseKey="")
    assert server._cloud_cfg() is None          # anon must never drive sync
    settings(supabaseKey="service-secret")
    assert server._cloud_cfg() == {"url": cloud_defaults.SUPABASE_URL,
                                   "key": "service-secret"}


def test_auth_status_reports_cloud_without_any_settings(settings, client):
    settings(supabaseUrl="", supabaseAnonKey="", supabaseKey="")
    r = client.get("/api/auth/status").get_json()
    assert r["cloud"] is True
    assert r["signed_in"] is False


# --- signup confirmation redirect (the ERR_CONNECTION_REFUSED fix) --------------

def test_confirm_redirect_defaults_to_the_website(settings):
    settings(cloudSiteUrl="")
    assert server._email_confirm_redirect() == \
        cloud_defaults.WEBSITE_URL + "/confirmed.html"


def test_confirm_redirect_honours_a_custom_site(settings):
    settings(cloudSiteUrl="https://example.org/lib/")   # trailing slash trimmed
    assert server._email_confirm_redirect() == "https://example.org/lib/confirmed.html"


def test_sign_up_encodes_redirect_to_on_the_signup_path(monkeypatch):
    import supabase_auth as sauth
    seen = {}

    def fake_post(cfg, path, payload, bearer=""):
        seen["path"] = path
        return {}                       # confirm-required: no access_token
    monkeypatch.setattr(sauth, "_post", fake_post)
    out = sauth.sign_up({"url": "https://x.co", "key": "k"}, "a@b.co", "pw",
                        "Nom", redirect_to="https://site/confirmed.html")
    assert out is None
    assert seen["path"] == "signup?redirect_to=https%3A%2F%2Fsite%2Fconfirmed.html"


def test_sign_up_without_redirect_is_a_plain_signup(monkeypatch):
    import supabase_auth as sauth
    seen = {}
    monkeypatch.setattr(sauth, "_post",
                        lambda cfg, path, payload, bearer="": seen.update(path=path) or {})
    sauth.sign_up({"url": "https://x.co", "key": "k"}, "a@b.co", "pw", "Nom")
    assert seen["path"] == "signup"


def test_signup_endpoint_passes_the_confirmation_redirect(settings, client, monkeypatch):
    settings(supabaseUrl="", supabaseAnonKey="", supabaseKey="", cloudSiteUrl="")
    seen = {}

    def fake_sign_up(cfg, email, password, name, redirect_to=""):
        seen["redirect_to"] = redirect_to
        return None                     # confirmation required
    monkeypatch.setattr(server.sauth, "sign_up", fake_sign_up)
    r = client.post("/api/auth/signup",
                    json={"email": "a@b.co", "password": "secret"}).get_json()
    assert r == {"ok": True, "confirm": True}
    assert seen["redirect_to"] == cloud_defaults.WEBSITE_URL + "/confirmed.html"
