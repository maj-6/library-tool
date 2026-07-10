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


@pytest.fixture(scope="session")
def data_root() -> Path:
    """The throwaway DATA_ROOT every tools module resolved at import."""
    return _TMP


@pytest.fixture()
def client():
    """Flask test client for the explorer, bound to the isolated DATA_ROOT.

    server.py builds the app at import time; the migrations/warmup that run
    under __main__ are intentionally skipped here.
    """
    import server

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c
