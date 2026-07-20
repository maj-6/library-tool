import importlib.util
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "smoke_packaged_sidecar.py"
SPEC = importlib.util.spec_from_file_location("smoke_packaged_sidecar", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


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


def test_failure_log_tail_redacts_the_capability(tmp_path):
    log = tmp_path / "sidecar.log"
    log.write_text("safe\nsecret-capability\n", encoding="utf-8")

    tail = smoke._log_tail(log, "secret-capability")

    assert "secret-capability" not in tail
    assert "[REDACTED]" in tail
