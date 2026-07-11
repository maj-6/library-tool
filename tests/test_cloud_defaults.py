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
