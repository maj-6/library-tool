import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(
    encoding="utf-8"
)


def _job(name: str, next_name: str) -> str:
    start = WORKFLOW.index(f"  {name}:\n")
    end = WORKFLOW.index(f"  {next_name}:\n", start)
    return WORKFLOW[start:end]


def test_release_tag_version_preflight_gates_every_publish_path():
    preflight = _job("preflight", "android")
    android = _job("android", "desktop")
    desktop = _job("desktop", "publish")
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    assert "Verify tag matches the desktop version" in preflight
    assert 'require("./desktop/package.json").version' in preflight
    assert '"v$version" != "$GITHUB_REF_NAME"' in preflight
    assert "needs: preflight" in android
    assert "needs: preflight" in desktop
    assert "needs.preflight.result == 'success'" in publish
    assert "needs: [preflight, android, desktop]" in publish


def test_release_requires_persistent_android_signing_for_tags():
    android = _job("android", "desktop")
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    # A tagged build with no keystore must FAIL the android job rather than
    # silently fall back to the runner's debug key (issue #119).
    assert "IS_TAG: ${{ startsWith(github.ref, 'refs/tags/') }}" in android
    assert 'elif [ "$IS_TAG" = "true" ]; then' in android
    assert "Tagged Android release requires the persistent signing keystore" in android

    # A workflow_dispatch dry run may still debug-sign, but the artifact is
    # clearly labelled as non-publishable.
    assert "WHL_DEBUG_SIGNED=true" in android
    assert "BookCapture-$V-debug-DONOTPUBLISH.apk" in android

    # The signer is verified before upload; a debug-signed tagged APK is refused.
    assert "apksigner" in android
    assert "verify --print-certs" in android
    assert "Android Debug" in android

    # The android job exposes the verified signer and the publish job reports it.
    assert "signer: ${{ steps.signer.outputs.signer }}" in android
    assert "needs.android.outputs.signer" in publish


def test_release_source_versions_are_internally_consistent():
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads(
        (ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8")
    )
    assert package["version"] == lock["version"] == lock["packages"][""]["version"]

    gradle = (ROOT / "android" / "BookCapture" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    assert re.search(r"\bversionCode\s*=\s*[1-9]\d*", gradle)
    assert re.search(r'\bversionName\s*=\s*"\d+\.\d+\.\d+[^\"]*"', gradle)
