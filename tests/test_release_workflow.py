import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(
    encoding="utf-8"
)
ANDROID_CERT_SHA256 = (
    ROOT / "android" / "BookCapture" / "release-signing-cert.sha256"
).read_text(encoding="utf-8").strip()


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


def test_release_requires_persistent_android_signing_identity():
    android = _job("android", "desktop")
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    # A tagged build with no keystore must FAIL the android job rather than
    # silently fall back to the runner's debug key (issue #119).
    release_event = (
        "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/')"
    )
    assert f"IS_RELEASE: ${{{{ {release_event} }}}}" in android
    assert 'elif [ "$IS_RELEASE" = "true" ]; then' in android
    assert "Tagged Android release requires the persistent signing keystore" in android

    # A workflow_dispatch dry run may still debug-sign, but the artifact is
    # clearly labelled as non-publishable.
    assert "WHL_DEBUG_SIGNED=true" in android
    assert "BookCapture-$V-debug-DONOTPUBLISH.apk" in android

    # The signer is verified before upload. Only an explicitly labelled dispatch
    # dry run may use the debug key; every other APK must match the pinned public
    # certificate fingerprint, including signed workflow_dispatch artifacts.
    assert "apksigner" in android
    assert "shell: bash" in android
    assert 'if ! "$APKSIGNER" verify --print-certs "$APK" > signer.txt; then' in android
    assert 'if [ -z "$DN" ] || [ -z "$SHA256" ]; then' in android
    assert "Android Debug" in android
    assert 'if [ "${WHL_DEBUG_SIGNED:-}" = "true" ]; then' in android
    assert 'EXPECTED_FILE="release-signing-cert.sha256"' in android
    assert "tr -d '[:space:]' < \"$EXPECTED_FILE\"" in android
    assert 'if [ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]; then' in android
    assert "Android signing certificate mismatch" in android
    # matches ANY signer block — newer build-tools drop the "Signer #1" prefix
    assert "certificate SHA-256 digest: " in android

    # The android job exposes the verified signer and the publish job reports it.
    assert "signer: ${{ steps.signer.outputs.signer }}" in android
    assert "signer_sha256: ${{ steps.signer.outputs.sha256 }}" in android
    assert "needs.android.outputs.signer" in publish

    # Manual dispatches are dry runs even when dispatched against a tag ref.
    assert release_event in publish
    preflight = _job("preflight", "android")
    assert release_event in preflight


def test_android_release_certificate_fingerprint_is_pinned():
    assert ANDROID_CERT_SHA256 == (
        "a28f22745810390f46faaee576c8c3272cb4ca72782ea38879392ae3b27a4fbf"
    )
    assert re.fullmatch(r"[0-9a-f]{64}", ANDROID_CERT_SHA256)


def test_partial_release_metadata_comes_from_collected_artifacts():
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    # Preserve independent app releases, but title and qualify a one-sided
    # GitHub Release from the files that were actually downloaded.
    assert "needs.desktop.result == 'success' || needs.android.result == 'success'" in publish
    assert "desktop_asset=$(find dist" in publish
    assert "android_asset=$(find dist" in publish
    assert "(desktop only)" in publish
    assert "Android only" in publish
    assert "**Partial release:**" in publish
    assert 'notes_args=(--notes-file "$effective_notes")' in publish
    # A retry must replace a stale partial/full title and notes, not merely
    # clobber the binary assets on an existing GitHub Release.
    assert 'gh release edit "$GITHUB_REF_NAME"' in publish


def test_desktop_job_requires_the_complete_updater_artifact_set():
    desktop = _job("desktop", "publish")

    assert "Verify the desktop release set" in desktop
    assert '"release/LibraryTool-Setup-$V.exe"' in desktop
    assert '"release/LibraryTool-Setup-$V.exe.blockmap"' in desktop
    assert '"release/latest.yml"' in desktop
    assert 'if [ ! -s "$FILE" ]; then' in desktop
    assert 'grep -Fq "version: $V" release/latest.yml' in desktop
    assert 'grep -Fq "LibraryTool-Setup-$V.exe" release/latest.yml' in desktop


def test_release_docs_use_raw_windows_base64_not_certutil_pem():
    docs = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    assert "[Convert]::ToBase64String" in docs
    assert "Do not use `certutil -encode`" in docs
    assert "(or `certutil -encode` on Windows)" not in docs


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


def test_tagged_release_uses_its_committed_release_notes_when_present():
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]
    assert 'notes_file="docs/releases/$GITHUB_REF_NAME.md"' in publish
    assert 'cat "$notes_file" >> "$effective_notes"' in publish
    assert 'notes_args=(--notes-file "$effective_notes")' in publish
    assert '"${notes_args[@]}"' in publish
