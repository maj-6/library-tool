# Corrections release-gate matrix

This matrix maps the final Corrections acceptance scenarios to executable
repository coverage. The release workflow runs the full Python and Node suites
before either desktop or Android packaging, then runs Android unit, debug and
release lint, and debug assembly gates.

| Scenario | Executable evidence |
| --- | --- |
| Backfill dry-run, idempotence, and resume | `tests/test_capture_archive_backfill.py` verifies a no-write dry run, preserved legacy identity, deterministic one-time identity derivation, a no-op second apply, partial-run continuation, and retry without resealing an already-published archive. |
| Backfill failure isolation and publication ordering | `tests/test_capture_archive_backfill.py` continues across missing/corrupt assets with bounded diagnostic codes, quarantines unsafe details, and proves cloud publication is attempted only after a valid archive exists. |
| Representative capture-to-correction flow | `tests/test_corrections_server_bridge.py::test_representative_flow_crosses_ui_client_flask_engine_and_worker` launches the real Flask composition and runs `tests/corrections_live_bridge_e2e.test.js` through `EngineClient`. |
| Cross-runtime archive and marker contract | `tests/test_corrections_cross_platform_fixture.py` seals the shared source twice through the production `.lib/3` materializer, binds the resulting byte length/hash to the association, reads the archive through `libformat`, and validates the exact cloud and LAN wire shapes. `CorrectionsReleaseFixtureTest` consumes those same wire objects through Android's production parsers and marker presenter. |
| Stale-source conflict | `tests/test_engine_correction_transforms.py::test_stale_source_conflicts_before_atomic_publication` and `::test_initial_stale_source_pin_fails_before_transform_or_commit`. |
| Duplicate Space and ambiguous retry | `tests/corrections_release_gate_behavior.test.js` and `tests/corrections_image_editor_state_behavior.test.js` retain one exact command and reject duplicate submission after acceptance. |
| Cancellation and process restart | `tests/test_corrections_release_gate.py::test_cancel_and_process_restart_require_a_new_operation_and_keep_original` plus the restart-reconciliation cases in `tests/test_engine_correction_transforms.py`. |
| OCR child failure | `tests/test_corrections_release_gate.py::test_window_reopen_and_review_transitions_do_not_own_running_transform` and the matching Node release gate prove image success remains usable and OCR failure remains observable. |
| Original recovery | The cancellation/restart gate verifies the original digest before and after recovery; the filesystem transform-store suite verifies immutable object and replay integrity. |
| Two-window review | The Python release gate reopens a durable review repository while work is live; `tests/test_corrections_server_bridge.py::test_production_review_bridge_owns_actor_and_reconciles_cas` covers stale second-window reconciliation. |
| Large-book performance | `tests/corrections_release_gate_behavior.test.js` executes the documented 5,000-capture Books-controller/render budget with lazy thumbnails, 10,000-artifact bounded-window budget, and 100-pointer-update budget. |
| Keyboard and accessible pane state | `tests/corrections_keyboard_accessibility_release_gate.test.js` composes the real Books, Artifacts, classification, and Properties modules against the production shell's pane/name/live-region contract. With focus on the actual tree target, it reaches image and Mistral-region objects through mounted keyboard events, applies `T` and `M`, submits a caption through the semantic form contract, and asserts native buttons, names, active/selected tree state, live target announcements, and machine/human Properties sections. The shared-fixture Node test independently navigates both MAR and ILL objects through the mounted Artifacts listener. |
| Replica compatibility | `tests/test_replica_integrity.py`, `tests/test_replica_format_integrity.py`, `tests/test_replica_group_detection.py`, `tests/test_replica_detection_jobs.py`, and `tests/replica_workbench_behavior.test.js`. |
| `.lib/1`, `.lib/2`, and `.lib/3` | `tests/test_libformat.py`, the legacy round trips in `tests/test_layout_regions.py`, and `tests/test_libformat_v3.py`. |
| Android v1 and association compatibility | `tests/test_capture_lib_source.py` consumes the version-1 Android photo-assets contract; Android's `CaptureBookPreviewTest`, `CaptureLibAssociationTest`, `CaptureSyncTest`, `CorrectionsReleaseFixtureTest`, `HomeListPresentationTest`, and `HomeListResourceTest` cover legacy input, association persistence, delivery, the shared desktop wire, and the accessible marker resource/presentation. |
| Updater and packaged resource-window smoke | `tests/test_release_workflow.py`, `tests/downloads_release_policy.test.js`, `tests/settings_behavior.test.js`, `tests/desktop_transport_security_behavior.test.js`, `tests/test_packaged_sidecar_smoke.py`, and the packaged-sidecar step in `.github/workflows/release.yml`. |

Run the Corrections-focused gates with:

```text
python -m pytest -q tests/test_capture_archive_backfill.py tests/test_corrections_cross_platform_fixture.py tests/test_corrections_release_gate.py tests/test_corrections_server_bridge.py tests/test_engine_correction_transforms.py
node --test tests/corrections_cross_platform_fixture_behavior.test.js tests/corrections_release_gate_behavior.test.js tests/corrections_keyboard_accessibility_release_gate.test.js
cd android/BookCapture && ./gradlew --no-daemon testDebugUnitTest --tests org.whl.bookcapture.CorrectionsReleaseFixtureTest
```

Release publication is additionally blocked by the repository-wide commands in
`.github/workflows/release.yml`:

```text
python -m ruff check .
python -m pytest -q
node --test
./gradlew --no-daemon testDebugUnitTest lintDebug lintRelease assembleDebug
```

The composed keyboard test intentionally exercises production modules without
inventing browser defaults: Books and Properties expose native button/form
semantics, while explicit keyboard handling is dispatched through the real
Artifacts tree and classification keymap. It is not a live Chromium, screen
reader, external OCR provider, physical Android device, or signed-installer
test; those remain manual or packaging validations.
