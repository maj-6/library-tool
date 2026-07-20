"""Desktop sidecar transport authentication and remote-content isolation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import desktop_transport
import server


CAPABILITY = "A" * 43
PORT = 45678
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
CAPABILITY_HEADER = "X-WHL-Desktop-Capability"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def desktop_client(monkeypatch):
    monkeypatch.setattr(server, "_DESKTOP_MODE", "packaged")
    monkeypatch.setattr(
        server,
        "_DESKTOP_CAPABILITY_DIGEST",
        hashlib.sha256(CAPABILITY.encode("ascii")).digest(),
    )
    monkeypatch.setattr(server, "_DESKTOP_PORT", PORT)
    monkeypatch.setattr(server, "_DESKTOP_EXPECTED_HOST", HOST)
    monkeypatch.setattr(server, "_DESKTOP_EXPECTED_ORIGIN", ORIGIN)
    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        yield client


def _headers(*, capability: str | None = CAPABILITY,
             host: str = HOST, origin: str | None = None) -> dict[str, str]:
    headers = {"Host": host}
    if capability is not None:
        headers[CAPABILITY_HEADER] = capability
    if origin is not None:
        headers["Origin"] = origin
    return headers


def test_desktop_api_requires_exact_capability(desktop_client):
    assert desktop_client.get(
        "/api/client_state", headers=_headers(capability=None)).status_code == 401
    assert desktop_client.get(
        "/api/client_state", headers=_headers(capability="B" * 43)).status_code == 401
    authenticated = desktop_client.get(
        "/api/client_state", headers=_headers())
    assert authenticated.status_code == 200
    assert authenticated.headers["Cache-Control"] == "no-store"
    assert authenticated.headers["Pragma"] == "no-cache"


def test_desktop_host_and_supplied_origin_are_exact(desktop_client):
    assert desktop_client.get(
        "/api/client_state",
        headers=_headers(host=f"localhost:{PORT}"),
    ).status_code == 403
    assert desktop_client.get(
        "/api/client_state",
        headers=_headers(origin="https://attacker.example"),
    ).status_code == 403
    assert desktop_client.get(
        "/api/client_state",
        headers=_headers(origin=ORIGIN + "/path"),
    ).status_code == 403
    assert desktop_client.get(
        "/api/client_state",
        headers=_headers(origin=ORIGIN),
    ).status_code == 200


def test_health_is_tokenless_but_still_host_guarded(desktop_client):
    assert desktop_client.get(
        "/healthz", headers={"Host": HOST}).get_json() == {"ok": True}
    assert desktop_client.get(
        "/healthz", headers={"Host": f"localhost:{PORT}"}).status_code == 403


def test_remote_html_proxy_is_gone(desktop_client, monkeypatch):
    def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("retired webview endpoint attempted a network fetch")

    monkeypatch.setattr(server.urllib.request, "urlopen", unexpected_fetch)
    response = desktop_client.get(
        "/api/webview?url=https://example.com/", headers=_headers())
    assert response.status_code == 410
    assert response.get_json() == {
        "ok": False,
        "error": "embedded_remote_content_disabled",
    }


def test_capability_comparison_always_uses_constant_time_digest(monkeypatch):
    expected = hashlib.sha256(CAPABILITY.encode("ascii")).digest()
    calls: list[tuple[bytes, bytes]] = []

    def checked_compare(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return left == right

    monkeypatch.setattr(desktop_transport.hmac, "compare_digest", checked_compare)
    assert desktop_transport.capability_matches(CAPABILITY, expected)
    assert not desktop_transport.capability_matches("wrong", expected)
    assert len(calls) == 2
    assert all(len(left) == len(right) == 32 for left, right in calls)


def test_startup_environment_is_consumed_and_packaged_mode_fails_closed():
    environ = {
        "WHL_DESKTOP_MODE": "packaged",
        "WHL_DESKTOP_CAPABILITY": CAPABILITY,
        "WHL_PORT": str(PORT),
        "UNCHANGED": "yes",
    }
    config = desktop_transport.load_desktop_transport_config(environ, packaged=True)
    assert (config.mode, config.port) == ("packaged", PORT)
    assert config.capability_digest == hashlib.sha256(CAPABILITY.encode("ascii")).digest()
    assert "WHL_DESKTOP_CAPABILITY" not in environ
    assert "WHL_DESKTOP_MODE" not in environ
    assert environ["UNCHANGED"] == "yes"

    with pytest.raises(RuntimeError, match="requires desktop transport"):
        desktop_transport.load_desktop_transport_config(
            {"WHL_PORT": str(PORT)}, packaged=True)
    with pytest.raises(RuntimeError, match="missing or malformed"):
        desktop_transport.load_desktop_transport_config({
            "WHL_DESKTOP_MODE": "packaged",
            "WHL_DESKTOP_CAPABILITY": "too-short",
            "WHL_PORT": str(PORT),
        }, packaged=True)


def test_source_mode_remains_available_without_a_capability():
    environ = {"WHL_DATA_ROOT": "example"}
    config = desktop_transport.load_desktop_transport_config(environ, packaged=False)
    assert (config.mode, config.capability_digest, config.port) == ("", None, None)
    assert environ == {"WHL_DATA_ROOT": "example"}


def test_fresh_server_import_consumes_plaintext_before_application_imports(tmp_path):
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": os.pathsep.join((
            str(ROOT / "src"),
            str(ROOT / "tools"),
            str(ROOT / "tools" / "whl_explorer"),
        )),
        "WHL_DATA_ROOT": str(tmp_path),
        "WHL_DESKTOP_MODE": "packaged",
        "WHL_DESKTOP_CAPABILITY": CAPABILITY,
        "WHL_PORT": str(PORT),
    })
    script = (
        "import json, os, server; "
        "print('TRANSPORT=' + json.dumps({"
        "'capability_present': 'WHL_DESKTOP_CAPABILITY' in os.environ, "
        "'mode_present': 'WHL_DESKTOP_MODE' in os.environ, "
        "'mode': server._DESKTOP_MODE, 'host': server._DESKTOP_EXPECTED_HOST}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    marker = next(
        line for line in completed.stdout.splitlines() if line.startswith("TRANSPORT="))
    result = json.loads(marker.removeprefix("TRANSPORT="))
    assert result == {
        "capability_present": False,
        "mode_present": False,
        "mode": "packaged",
        "host": HOST,
    }
    assert CAPABILITY not in completed.stdout
    assert CAPABILITY not in completed.stderr


def test_fresh_packaged_equivalent_import_fails_closed_without_launch_contract():
    env = os.environ.copy()
    env.pop("WHL_DESKTOP_MODE", None)
    env.pop("WHL_DESKTOP_CAPABILITY", None)
    env["PYTHONPATH"] = str(ROOT / "tools" / "whl_explorer")
    completed = subprocess.run(
        [sys.executable, "-c", "import sys; sys.frozen=True; import desktop_transport"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "requires desktop transport authentication" in completed.stderr


def test_root_response_has_a_restrictive_compatible_csp(desktop_client):
    response = desktop_client.get("/", headers={"Host": HOST})
    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "frame-src 'self' blob:" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "camera=()" in response.headers["Permissions-Policy"]
