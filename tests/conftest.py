"""Test bootstrap: isolate the suite from the developer's live data.

libcommon resolves DATA_ROOT from the WHL_DATA_ROOT environment variable
ONCE at import time, so this file must set it before any test imports a
tools module (pytest imports conftest.py before collecting tests). Every
test therefore reads and writes a throwaway directory and can never touch
the repo's live output/ state — the client-state wipe recounted in
tools/README.md is the incident this guards against.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if "libcommon" in sys.modules:  # pragma: no cover — misconfiguration guard
    raise RuntimeError("libcommon was imported before conftest set WHL_DATA_ROOT")

_TMP = Path(tempfile.mkdtemp(prefix="whl-tests-")).resolve()
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["WHL_DATA_ROOT"] = str(_TMP)

import pytest  # noqa: E402

# The session an approved contributor's machine would hold. display_name stays
# blank so _actor() still exercises the X-WHL-Actor fallback paths that
# several tests assert on. expires_at is far enough out that nothing ever
# attempts a token refresh (which would be a live network call).
TEST_SESSION = {
    "access_token": "test-token", "refresh_token": "test-refresh",
    "expires_at": 4102444800, "user_id": "test-user",
    "email": "tester@example.com", "display_name": "",
    "role": "contributor", "status": "approved",
}


def seed_session(role: str = "contributor", status: str = "approved",
                 **extra) -> None:
    """(Re)write auth_session.json — the account gate reads it per request."""
    auth = _TMP / "output" / "auth_session.json"
    auth.parent.mkdir(parents=True, exist_ok=True)
    ses = dict(TEST_SESSION, role=role, status=status, **extra)
    auth.write_text(json.dumps({"session": ses, "account_id": ses["user_id"]}),
                    encoding="utf-8")


@pytest.fixture(scope="session")
def data_root() -> Path:
    """The throwaway DATA_ROOT every tools module resolved at import."""
    return _TMP


@pytest.fixture()
def client():
    """Flask test client for the explorer, bound to the isolated DATA_ROOT.

    server.py builds the app at import time; the migrations/warmup that run
    under __main__ are intentionally skipped here. The workbench is account-
    gated (server._require_member), so each test starts from the session an
    approved contributor would have; gate tests reseed or delete it.
    """
    import server

    seed_session()
    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c
