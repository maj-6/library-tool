"""Launch and verify the frozen desktop sidecar before packaging Electron."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


CAPABILITY_HEADER = "X-WHL-Desktop-Capability"
MAX_RESPONSE_BYTES = 1 << 20


class SmokeFailure(RuntimeError):
    """The packaged sidecar did not satisfy its desktop transport contract."""


def _new_capability() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_request(port: int, path: str, capability: str | None = None) -> tuple[int, bytes]:
    headers = {CAPABILITY_HEADER: capability} if capability is not None else {}
    request = Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        response = urlopen(request, timeout=2)  # noqa: S310 - fixed loopback origin
    except HTTPError as error:
        response = error
    with response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise SmokeFailure(f"{path} exceeded the smoke response limit")
        return int(response.status), body


def _decode_json(body: bytes, path: str) -> dict:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SmokeFailure(f"{path} did not return UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise SmokeFailure(f"{path} did not return a JSON object")
    return value


def _wait_until_ready(
    process: subprocess.Popen,
    port: int,
    timeout_seconds: float,
    request: Callable[[int, str, str | None], tuple[int, bytes]] = _http_request,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    delay = 0.05
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise SmokeFailure(f"sidecar exited before readiness (code {code})")
        try:
            status, body = request(port, "/healthz", None)
            if status == 200 and _decode_json(body, "/healthz") == {"ok": True}:
                return
            last_error = SmokeFailure(f"/healthz returned HTTP {status}")
        except (OSError, URLError, SmokeFailure) as error:
            last_error = error
        time.sleep(delay)
        delay = min(delay * 1.5, 0.5)
    detail = f": {last_error}" if last_error else ""
    raise SmokeFailure(f"sidecar did not become ready within {timeout_seconds:g}s{detail}")


def _verify_transport(
    port: int,
    capability: str,
    request: Callable[[int, str, str | None], tuple[int, bytes]] = _http_request,
) -> None:
    status, _body = request(port, "/api/v1/capabilities", None)
    if status != 401:
        raise SmokeFailure(f"unauthenticated engine request returned HTTP {status}, not 401")

    status, body = request(port, "/api/v1/capabilities", capability)
    if status != 200:
        raise SmokeFailure(f"authenticated engine request returned HTTP {status}")
    document = _decode_json(body, "/api/v1/capabilities")
    if document.get("ok") is not True or document.get("schema") != "librarytool.capabilities/1":
        raise SmokeFailure("authenticated capability discovery returned an invalid contract")


def _stop_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as error:
        raise SmokeFailure("sidecar process tree could not be stopped") from error


def _log_tail(path: Path, capability: str, lines: int = 80) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(sidecar log unavailable)"
    return "\n".join(text.splitlines()[-lines:]).replace(capability, "[REDACTED]")


def smoke(sidecar: Path, timeout_seconds: float = 45) -> None:
    sidecar = sidecar.resolve(strict=True)
    if not sidecar.is_file():
        raise SmokeFailure(f"sidecar is not a file: {sidecar}")
    capability = _new_capability()
    port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="whl-sidecar-smoke-") as temporary:
        root = Path(temporary)
        data_root = root / "data"
        data_root.mkdir()
        log_path = root / "sidecar.log"
        environment = os.environ.copy()
        environment.update({
            "WHL_PORT": str(port),
            "WHL_DATA_ROOT": str(data_root),
            "WHL_APP_VERSION": "packaged-smoke",
            "WHL_DESKTOP_MODE": "packaged",
            "WHL_DESKTOP_CAPABILITY": capability,
        })
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            process = subprocess.Popen(
                [str(sidecar)],
                cwd=sidecar.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=flags,
            )
            failure: Exception | None = None
            try:
                _wait_until_ready(process, port, timeout_seconds)
                _verify_transport(port, capability)
            except Exception as error:  # preserve diagnostics after cleanup
                failure = error
            finally:
                _stop_process_tree(process)
        if failure is not None:
            tail = _log_tail(log_path, capability)
            raise SmokeFailure(f"{failure}\n--- sidecar log tail ---\n{tail}") from failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=45)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.timeout <= 120:
        raise SystemExit("--timeout must be between 1 and 120 seconds")
    try:
        smoke(args.sidecar, args.timeout)
    except (OSError, SmokeFailure) as error:
        print(f"packaged sidecar smoke failed: {error}", file=sys.stderr)
        return 1
    print("Packaged sidecar transport smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
