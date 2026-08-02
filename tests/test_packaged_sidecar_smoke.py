import importlib.util
import json
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "smoke_packaged_sidecar.py"
SPEC = importlib.util.spec_from_file_location("smoke_packaged_sidecar", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def _secret_status(configured, revision):
    return {
        "id": smoke.SMOKE_SECRET_ID,
        "configured": configured,
        "masked_hint": "masked" if configured else "",
        "revision": revision,
    }


def _secret_list(configured, revision, *, health=None):
    return json.dumps({
        "ok": True,
        "schema": "librarytool.secret-status-list/1",
        "health": health or {
            "available": True,
            "state": "ready",
            "writable": True,
        },
        "secrets": [_secret_status(configured, revision)],
    }).encode()


def _mutation():
    return b'{"ok":true,"schema":"librarytool.secret-mutation-receipt/1","replayed":false}'


def test_capability_has_the_desktop_transport_shape():
    capability = smoke._new_capability()

    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", capability)


def test_transport_probe_requires_auth_and_validates_discovery():
    calls = []

    def request(port, path, capability=None):
        calls.append((port, path, capability))
        if capability is None:
            return 401, b""
        return 200, b'{"ok":true,"schema":"librarytool.capabilities/1"}'

    smoke._verify_transport(43123, "secret-capability", request)

    assert calls == [
        (43123, "/api/v1/capabilities", None),
        (43123, "/api/v1/capabilities", "secret-capability"),
    ]


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        ([(200, b"{}")], "not 401"),
        ([(401, b""), (403, b"{}")] , "returned HTTP 403"),
        (
            [(401, b""), (200, b'{"ok":true,"schema":"wrong"}')],
            "invalid contract",
        ),
    ],
)
def test_transport_probe_fails_closed(responses, message):
    answers = iter(responses)

    with pytest.raises(smoke.SmokeFailure, match=message):
        smoke._verify_transport(43123, "secret-capability", lambda *_args: next(answers))


def test_failure_log_tail_redacts_capabilities_and_credential(tmp_path):
    log = tmp_path / "sidecar.log"
    log.write_text(
        "safe\nfirst-capability\nsecond-capability\ndummy-credential\n",
        encoding="utf-8",
    )

    tail = smoke._log_tail(log, (
        "first-capability", "second-capability", "dummy-credential",
    ))

    assert "capability" not in tail
    assert "dummy-credential" not in tail
    assert tail.count("[REDACTED]") == 3


def test_secret_probe_writes_persists_and_clears_masked_credential():
    credential = "dummy-credential"
    calls = []
    responses = iter([
        (200, _secret_list(False, "secret-r1")),
        (200, _mutation()),
        (200, _secret_list(True, "secret-r2")),
        (200, _mutation()),
        (200, _secret_list(False, "secret-r3")),
    ])

    def request(port, path, capability=None, **options):
        calls.append((port, path, capability, options))
        return next(responses)

    smoke._credential_probe(
        43123, "first-capability", credential, False, request)
    smoke._credential_probe(
        43124, "second-capability", credential, True, request)

    assert [call[1] for call in calls] == [
        "/api/v1/secrets", smoke.SMOKE_SECRET_PATH,
        "/api/v1/secrets", smoke.SMOKE_SECRET_PATH, "/api/v1/secrets",
    ]
    assert json.loads(calls[1][3]["body"]) == {"credential": credential}
    assert calls[1][3]["headers"]["If-Match"] == '"secret-r1"'
    assert calls[3][3] == {
        "method": "DELETE",
        "headers": {
            "Idempotency-Key": "packaged-smoke-clear",
            "If-Match": '"secret-r2"',
        },
    }
    assert [call[2] for call in calls] == [
        "first-capability", "first-capability",
        "second-capability", "second-capability", "second-capability",
    ]


def test_secret_probe_fails_when_protected_store_is_unavailable():
    body = _secret_list(False, "secret-r1", health={
        "available": False,
        "state": "unsupported",
        "writable": False,
    })

    with pytest.raises(smoke.SmokeFailure, match="not ready and writable"):
        smoke._credential_probe(
            43123,
            "capability",
            "dummy-credential",
            False,
            lambda *_args, **_kwargs: (200, body),
        )


def test_secret_probe_rejects_a_response_that_exposes_the_credential():
    credential = "dummy-credential"
    document = json.loads(_secret_list(False, "secret-r1"))
    document["credential"] = credential

    with pytest.raises(smoke.SmokeFailure, match="exposed credential") as raised:
        smoke._credential_probe(
            43123,
            "capability",
            credential,
            False,
            lambda *_args, **_kwargs: (200, json.dumps(document).encode()),
        )

    assert credential not in str(raised.value)


def test_smoke_restarts_same_sidecar_and_data_root_with_new_capability(
        tmp_path, monkeypatch):
    sidecar = tmp_path / "sidecar.exe"
    sidecar.write_bytes(b"frozen-sidecar")
    capabilities = iter(("first-capability", "second-capability"))
    ports = iter((43123, 43124))
    launches = []
    probes = []

    monkeypatch.setattr(smoke, "_new_capability", lambda: next(capabilities))
    monkeypatch.setattr(smoke, "_new_smoke_credential", lambda: "dummy-credential")
    monkeypatch.setattr(smoke, "_free_loopback_port", lambda: next(ports))
    monkeypatch.setattr(
        smoke, "_credential_probe",
        lambda port, capability, credential, persisted: probes.append(
            ("persisted" if persisted else "write", port, capability, credential)))

    def run_sidecar(
            launched_sidecar, data_root, _log_path, capability,
            _timeout_seconds, verify):
        port = next(ports)
        launches.append((launched_sidecar, data_root, capability, port))
        verify(port)

    monkeypatch.setattr(smoke, "_run_sidecar", run_sidecar)

    smoke.smoke(sidecar)

    assert [launch[0] for launch in launches] == [sidecar, sidecar]
    assert launches[0][1] == launches[1][1]
    assert [launch[2] for launch in launches] == [
        "first-capability", "second-capability",
    ]
    assert probes == [
        ("write", 43123, "first-capability", "dummy-credential"),
        ("persisted", 43124, "second-capability", "dummy-credential"),
    ]
