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
