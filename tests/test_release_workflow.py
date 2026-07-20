import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
CI_WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
ANDROID_CERT_SHA256 = (
    (ROOT / "android" / "BookCapture" / "release-signing-cert.sha256")
    .read_text(encoding="utf-8")
    .strip()
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

    assert "Verify tag matches its product version" in preflight
    assert 'require("./desktop/package.json").version' in preflight
    assert "android/BookCapture/app/build.gradle.kts" in preflight
    assert 'expected="android-v$version"' in preflight
    assert 'if [ "$expected" != "$GITHUB_REF_NAME" ]; then' in preflight
    assert "needs: preflight" in android
    assert "needs: preflight" in desktop
    assert "needs.preflight.result == 'success'" in publish
    assert "needs: [preflight, android, desktop]" in publish


def test_android_is_released_only_after_its_version_identity_changes():
    preflight = _job("preflight", "android")
    android = _job("android", "desktop")

    assert "fetch-depth: 0" in preflight
    assert "android_release: ${{ steps.scope.outputs.android_release }}" in preflight
    assert "android_version: ${{ steps.scope.outputs.android_version }}" in preflight
    assert "desktop_version: ${{ steps.version.outputs.desktop_version }}" in preflight
    assert "gh api --paginate --slurp" in preflight
    assert '"repos/$GITHUB_REPOSITORY/releases?per_page=100"' in preflight
    assert "if ! gh api" in preflight
    assert "refusing to guess the Android release scope" in preflight
    assert ".github/scripts/release_preflight.py android-scope" in preflight
    assert '--exclude-tag "$GITHUB_REF_NAME"' in preflight
    assert "git describe" not in preflight
    assert "if: needs.preflight.outputs.android_release == 'true'" in android


def test_public_tags_reject_unknown_prerelease_channels():
    preflight = _job("preflight", "android")
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    assert "release_channel: ${{ steps.version.outputs.release_channel }}" in preflight
    assert ".github/scripts/release_preflight.py classify-version" in preflight
    assert "*-alpha.*|*-beta.*|*-rc.*" not in preflight
    assert "RELEASE_CHANNEL: ${{ needs.preflight.outputs.release_channel }}" in publish
    assert 'case "$GITHUB_REF_NAME"' not in publish
    assert '--channel "$RELEASE_CHANNEL"' in publish
    assert "--prerelease=false --latest" in publish
    assert "--prerelease --latest=false" in publish
    assert '"${release_metadata_args[@]}"' in publish


def test_publish_runs_after_failures_or_skips_but_never_after_cancellation():
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    condition = publish[: publish.index("    needs:")]
    assert "!cancelled()" in condition
    assert "always()" not in condition
    assert "needs.preflight.result == 'success'" in condition
    assert (
        "needs.desktop.result == 'success' || needs.android.result == 'success'"
        in condition
    )


def test_release_token_is_write_scoped_only_to_publish_job():
    pre_publish = WORKFLOW[: WORKFLOW.index("  publish:\n")]
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    assert "permissions:\n  contents: read" in pre_publish
    assert "contents: write" not in pre_publish
    assert "permissions:\n      contents: write" in publish


def test_android_only_tag_skips_desktop_and_is_not_reported_as_a_failed_partial():
    desktop = _job("desktop", "publish")
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    assert 'tags: ["v*", "android-v*"]' in WORKFLOW
    assert "if: ${{ !startsWith(github.ref_name, 'android-v') }}" in desktop
    assert 'if [[ "$GITHUB_REF_NAME" != android-v* ]]; then' in publish
    assert 'notes_file="docs/releases/$GITHUB_REF_NAME.md"' in publish


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


def test_tagged_android_release_requires_public_cloud_config():
    android = _job("android", "desktop")

    assert "WHL_SUPABASE_URL: ${{ vars.SUPABASE_URL }}" in android
    assert "WHL_SUPABASE_ANON_KEY: ${{ vars.SUPABASE_ANON_KEY }}" in android
    assert (
        "Tagged Android release requires non-empty SUPABASE_URL and "
        "SUPABASE_ANON_KEY"
    ) in android
    assert "SUPABASE_URL must be an https URL" in android
    assert "./gradlew --no-daemon testDebugUnitTest lintRelease assembleRelease" in android


def test_release_token_is_write_scoped_only_to_publish_job():
    pre_publish = WORKFLOW[: WORKFLOW.index("  publish:\n")]
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    assert "permissions:\n  contents: read" in pre_publish
    assert "contents: write" not in pre_publish
    assert "permissions:\n      contents: write" in publish


def test_partial_release_metadata_comes_from_collected_artifacts():
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    # Preserve independent app releases, but title and qualify a one-sided
    # GitHub Release from the files that were actually downloaded.
    assert (
        "needs.desktop.result == 'success' || needs.android.result == 'success'"
        in publish
    )
    assert "mapfile -t desktop_assets" in publish
    assert "mapfile -t android_assets" in publish
    assert "(desktop only)" in publish
    assert "Android only" in publish
    assert "**Partial release:**" in publish
    assert "was deliberately not rebuilt" in publish
    assert 'notes_args=(--notes-file "$effective_notes")' in publish
    # A retry must replace a stale partial/full title and notes, not merely
    # clobber the binary assets on an existing GitHub Release.
    assert 'gh release edit "$GITHUB_REF_NAME"' in publish

    # Reruns include allowed assets already attached to this tag when deriving
    # full/partial metadata, but only freshly collected allowlisted files upload.
    assert ".github/scripts/release_preflight.py inspect-release" in publish
    assert "draft) release_exists=true; release_is_draft=true" in publish
    assert "existing_android_asset=" in publish
    assert "existing_desktop_asset=" in publish
    assert 'gh release download "$GITHUB_REF_NAME"' in publish
    assert '--pattern "$existing_android_asset" --dir dist' in publish
    assert '--pattern "$existing_desktop_asset" --dir dist' in publish
    assert 'if [ "$has_desktop" = true ] && [ "$has_android" = true ]; then' in publish


def test_publish_downloads_and_uploads_only_named_release_artifacts():
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]

    assert "name: desktop" in publish
    assert "name: android" in publish
    assert "merge-multiple: true" not in publish
    assert 'upload_assets+=("$asset")' in publish
    assert 'upload_assets+=("$android_asset")' in publish
    assert '"dist/LibraryTool-Setup-$DESKTOP_VERSION.exe"' in publish
    assert '"dist/BookCapture-$ANDROID_VERSION.apk"' in publish
    assert '"${upload_assets[@]}"' in publish
    assert "asset outside the public allowlist" in publish
    assert "dist/*" not in publish


def test_publication_is_atomic_and_registration_follows_publication():
    publish = WORKFLOW[WORKFLOW.index("  publish:\n") :]
    github_step = publish[publish.index("      - name: GitHub Release") :]
    github_step = github_step[
        : github_step.index("      - name: Register on the Downloads page")
    ]

    # Existing releases receive complete assets before metadata is edited. A
    # recovered draft is explicitly published only after that upload succeeds.
    assert github_step.index("gh release upload") < github_step.index("gh release edit")
    assert "--draft=false" in github_step
    # New releases explicitly follow create-draft -> allowlisted upload ->
    # publish. Channel/latest metadata is absent from draft creation and applied
    # only by the final edit.
    new_release = github_step[github_step.index("# Keep new release metadata") :]
    create_at = new_release.index("gh release create")
    upload_at = new_release.index("gh release upload")
    publish_at = new_release.index("gh release edit")
    assert create_at < upload_at < publish_at
    create_command = new_release[create_at:upload_at]
    assert "--draft --title" in create_command
    assert '"${release_metadata_args[@]}"' not in create_command
    assert '"${upload_assets[@]}"' in new_release[upload_at:publish_at]
    assert "--draft=false" in new_release[publish_at:]
    assert '"${release_metadata_args[@]}"' in new_release[publish_at:]

    assert publish.index("      - name: GitHub Release") < publish.index(
        "      - name: Register on the Downloads page"
    )


def test_desktop_job_requires_the_complete_updater_artifact_set():
    desktop = _job("desktop", "publish")

    assert "Verify the desktop release set" in desktop
    assert '"release/LibraryTool-Setup-$V.exe"' in desktop
    assert '"release/LibraryTool-Setup-$V.exe.blockmap"' in desktop
    assert '"release/latest.yml"' in desktop
    assert 'if [ ! -s "$FILE" ]; then' in desktop
    assert 'grep -Fq "version: $V" release/latest.yml' in desktop
    assert 'grep -Fq "LibraryTool-Setup-$V.exe" release/latest.yml' in desktop


def test_desktop_job_smokes_the_frozen_sidecar_before_packaging():
    desktop = _job("desktop", "publish")

    freeze = desktop.index("Freeze the sidecar")
    smoke = desktop.index("Smoke-test the frozen sidecar transport")
    package = desktop.index("Build the installer")
    assert freeze < smoke < package
    assert "../.github/scripts/smoke_packaged_sidecar.py" in desktop
    assert "dist-sidecar/whl-explorer-sidecar/whl-explorer-sidecar.exe" in desktop


def test_release_docs_use_raw_windows_base64_not_certutil_pem():
    docs = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    assert "[Convert]::ToBase64String" in docs
    assert "Do not use `certutil -encode`" in docs
    assert "(or `certutil -encode` on Windows)" not in docs


def test_release_source_versions_are_internally_consistent():
    package = json.loads(
        (ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
    )
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


def test_ci_uses_cross_platform_node_test_discovery():
    # Shell glob expansion differs between bash and PowerShell, while Node 22
    # discovers the repository's *.test.js files itself when no path is given.
    assert "node --test\n" in CI_WORKFLOW
    assert "node --test tests" not in CI_WORKFLOW


def test_all_javascript_workflow_steps_use_electron_43_node_baseline():
    preflight = _job("preflight", "android")
    desktop = _job("desktop", "publish")

    assert 'node-version: "22.12"' in CI_WORKFLOW
    assert 'node-version: "22.12"' in preflight
    assert 'node-version: "22.12"' in desktop
    assert 'node-version: "20"' not in WORKFLOW
