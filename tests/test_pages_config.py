import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "write_config.py"
SPEC = importlib.util.spec_from_file_location("pages_write_config", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PAGES_CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PAGES_CONFIG)
WORKFLOW = (ROOT / ".github" / "workflows" / "pages.yml").read_text(
    encoding="utf-8"
)


def _b64(value: dict) -> str:
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _jwt(role: str = "anon", *, expiration: float | None = None) -> str:
    if expiration is None:
        expiration = time.time() + 3600
    return ".".join(
        (
            _b64({"alg": "HS256", "typ": "JWT"}),
            _b64({"iss": "supabase", "role": role, "exp": expiration}),
            "dGVzdC1zaWduYXR1cmU",
        )
    )


def _run(
    tmp_path: Path,
    *,
    mode: str,
    url: str | None = None,
    key: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    output = tmp_path / "config.js"
    summary = tmp_path / "summary.md"
    env = os.environ.copy()
    env.pop("SUPABASE_URL", None)
    env.pop("SUPABASE_ANON_KEY", None)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    if url is not None:
        env["SUPABASE_URL"] = url
    if key is not None:
        env["SUPABASE_ANON_KEY"] = key
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            mode,
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, output, summary


@pytest.mark.parametrize(
    ("url", "key", "missing"),
    (
        (None, _jwt(), "SUPABASE_URL"),
        ("https://project.supabase.co", None, "SUPABASE_ANON_KEY"),
    ),
)
def test_production_rejects_partial_cloud_configuration(
    tmp_path: Path,
    url: str | None,
    key: str | None,
    missing: str,
):
    result, output, summary = _run(tmp_path, mode="production", url=url, key=key)

    assert result.returncode == 1
    assert missing in result.stdout
    assert not output.exists()
    assert "**Selected mode:** `production`" in summary.read_text(encoding="utf-8")
    assert f"**error:** {missing} is empty" in summary.read_text(encoding="utf-8")


def test_production_rejects_absent_cloud_configuration(tmp_path: Path):
    result, output, summary = _run(tmp_path, mode="production")

    assert result.returncode == 1
    assert "production Pages requires non-empty SUPABASE_URL" in result.stdout
    assert not output.exists()
    report = summary.read_text(encoding="utf-8")
    assert "**Selected mode:** `production`" in report
    assert "**error:** production Pages requires" in report


def test_production_writes_complete_unexpired_anon_configuration(tmp_path: Path):
    key = _jwt()
    result, output, summary = _run(
        tmp_path,
        mode="production",
        url="https://project.supabase.co",
        key=key,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    config = output.read_text(encoding="utf-8")
    assert 'deploymentMode: "production"' in config
    assert 'supabaseUrl: "https://project.supabase.co"' in config
    assert f"supabaseAnonKey: {json.dumps(key)}" in config
    report = summary.read_text(encoding="utf-8")
    assert "**Selected mode:** `production`" in report
    assert "validated legacy anon JWT" in report
    assert key not in result.stdout
    assert key not in report


def test_production_accepts_modern_publishable_key(tmp_path: Path):
    result, output, summary = _run(
        tmp_path,
        mode="production",
        url="https://project.supabase.co/",
        key="sb_publishable_public_browser_key",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "sb_publishable_public_browser_key" in output.read_text(encoding="utf-8")
    assert "validated publishable" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("url", "error"),
    (
        ("http://project.supabase.co", "https:// origin"),
        ("https://project.supabase.co/rest/v1", "bare origin"),
        ("https://user:password@project.supabase.co", "user credentials"),
        ("https://project.supabase.co:invalid", "invalid port"),
    ),
)
def test_production_rejects_invalid_cloud_origin(
    tmp_path: Path,
    url: str,
    error: str,
):
    result, output, _ = _run(tmp_path, mode="production", url=url, key=_jwt())

    assert result.returncode == 1
    assert error in result.stdout
    assert not output.exists()


@pytest.mark.parametrize(
    ("key", "error"),
    (
        (_jwt("service_role"), "role is 'service_role'"),
        (_jwt(expiration=1), "has expired"),
        ("not-a-jwt", "expected three non-empty parts"),
        ("sb_secret_backend_owner_key", "Supabase secret key"),
        ("sb_publishable_", "not a valid Supabase publishable key"),
    ),
)
def test_production_rejects_privileged_expired_or_invalid_keys(
    tmp_path: Path,
    key: str,
    error: str,
):
    result, output, summary = _run(
        tmp_path,
        mode="production",
        url="https://project.supabase.co",
        key=key,
    )

    assert result.returncode == 1
    assert error in result.stdout
    assert not output.exists()
    assert "**error:**" in summary.read_text(encoding="utf-8")


def test_fixture_preview_is_credential_free_and_unmistakable(tmp_path: Path):
    secret = _jwt("service_role")
    result, output, summary = _run(
        tmp_path,
        mode="fixture-preview",
        url="https://production.supabase.co",
        key=secret,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    config = output.read_text(encoding="utf-8")
    assert config.startswith("// FIXTURE PREVIEW")
    assert 'deploymentMode: "fixture-preview"' in config
    assert "fixturePreview: true" in config
    assert "supabaseUrl" not in config
    assert secret not in config
    report = summary.read_text(encoding="utf-8")
    assert "**Selected mode:** `fixture-preview`" in report
    assert "never a Pages deployment" in report
    assert "must not be deployed" in report


def test_local_development_without_credentials_removes_stale_generated_config(
    tmp_path: Path,
):
    output = tmp_path / "config.js"
    output.write_text("stale live config", encoding="utf-8")

    result, actual_output, summary = _run(tmp_path, mode="development")

    assert result.returncode == 0, result.stdout + result.stderr
    assert actual_output == output
    assert not output.exists()
    assert "**Selected mode:** `development`" in summary.read_text(encoding="utf-8")


class _ProbeResponse:
    def __init__(
        self,
        url: str,
        *,
        body: bytes = b"[]",
        status: int = 200,
        final_url: str | None = None,
    ):
        self.url = url
        self.body = body
        self.status = status
        self.final_url = final_url or url
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def geturl(self) -> str:
        return self.final_url

    def read(self, amount: int) -> bytes:
        return self.body[:amount]

    def close(self) -> None:
        self.closed = True


def test_live_probe_checks_every_public_website_resource(monkeypatch):
    calls = []

    def open_probe(request, timeout):
        calls.append((request, timeout))
        return _ProbeResponse(request.full_url)

    monkeypatch.setattr(PAGES_CONFIG, "_open_probe", open_probe)
    key = "sb_publishable_public_browser_key"

    PAGES_CONFIG.probe_live_configuration(
        "https://project.supabase.co",
        key,
    )

    assert [
        urlsplit(request.full_url).path.removeprefix("/rest/v1/")
        for request, _ in calls
    ] == [table for table, _ in PAGES_CONFIG.PUBLIC_READ_PROBES]
    assert all(0 < timeout <= 5 for _, timeout in calls)
    for request, _ in calls:
        headers = {name.casefold(): value for name, value in request.header_items()}
        assert headers["apikey"] == key
        assert headers["authorization"] == f"Bearer {key}"


def test_live_probe_rejects_redirected_endpoint(monkeypatch):
    def redirect_probe(request, timeout):
        return _ProbeResponse(
            request.full_url,
            final_url="https://attacker.invalid/rest/v1/volumes",
        )

    monkeypatch.setattr(PAGES_CONFIG, "_open_probe", redirect_probe)

    with pytest.raises(PAGES_CONFIG.LiveProbeError, match="redirected away"):
        PAGES_CONFIG.probe_live_configuration(
            "https://project.supabase.co",
            "sb_publishable_public_browser_key",
        )


@pytest.mark.parametrize(
    ("body", "error"),
    (
        (b"<html>not json</html>", "valid JSON"),
        (b'{"unexpected": true}', "JSON array"),
        (b"x" * (64 * 1024 + 1), "oversized"),
    ),
    ids=("invalid-json", "wrong-shape", "oversized"),
)
def test_live_probe_rejects_unusable_public_response(
    monkeypatch,
    body: bytes,
    error: str,
):
    monkeypatch.setattr(
        PAGES_CONFIG,
        "_open_probe",
        lambda request, timeout: _ProbeResponse(request.full_url, body=body),
    )

    with pytest.raises(PAGES_CONFIG.LiveProbeError, match=error):
        PAGES_CONFIG.probe_live_configuration(
            "https://project.supabase.co",
            "sb_publishable_public_browser_key",
        )


def _job(name: str, next_name: str | None = None) -> str:
    start = WORKFLOW.index(f"  {name}:\n")
    if next_name is None:
        return WORKFLOW[start:]
    return WORKFLOW[start : WORKFLOW.index(f"  {next_name}:\n", start)]


def test_pages_workflow_gates_production_before_every_artifact_action():
    deploy = _job("deploy", "fixture-preview")

    validation = deploy.index("write_config.py")
    configure = deploy.index("actions/configure-pages@")
    upload = deploy.index("actions/upload-pages-artifact@")
    publish = deploy.index("actions/deploy-pages@")
    assert validation < configure < upload < publish
    assert "--mode production" in deploy
    assert "--probe-live" in deploy
    assert "SUPABASE_URL: ${{ vars.SUPABASE_URL }}" in deploy
    assert "SUPABASE_ANON_KEY: ${{ vars.SUPABASE_ANON_KEY }}" in deploy
    assert "name: github-pages" in deploy
    assert "pages: write" in deploy


def test_pages_workflow_fixture_preview_cannot_deploy_to_pages():
    preview = _job("fixture-preview")

    assert "workflow_dispatch:" in WORKFLOW
    assert "- fixture-preview" in WORKFLOW
    assert "write_config.py --mode fixture-preview" in preview
    assert "actions/upload-artifact@v4" in preview
    assert "fixture-preview-NOT-FOR-PRODUCTION-" in preview
    assert "uses: actions/upload-pages-artifact" not in preview
    assert "uses: actions/deploy-pages" not in preview
    assert "environment:" not in preview
    assert "pages: write" not in preview
