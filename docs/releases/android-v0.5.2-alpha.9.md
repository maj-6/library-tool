# Library Tool Capture 0.5.2-alpha.9

Android-only prerelease. `versionCode` 40.

This release adds capture management actions to the Home and camera screens
and keeps the Sync button fully visible when its progress label wraps. It also
includes the sync recovery from 0.5.2-alpha.8: stale batches such as **1/23**
are reconciled with the full pending queue, and interrupted processing work is
recovered instead of silently ending the batch.

## Manage a capture with a long press

Long-press a capture on Home or the last captured book in the camera to choose
an action:

- **Add remark** or **Edit remark** updates its needs-attention note.
- **Reprocess** submits a sealed capture with retained photos for fresh OCR and
  catalog extraction without creating duplicate work.
- **Delete from device** removes the local capture only after confirmation. If
  the capture was already delivered, its copy in Library Tool is preserved.

The same actions are exposed to accessibility services. Actions that are not
safe for the capture's current state explain why they cannot run.

## Sync stays aligned

**New scan** and **Sync captures** now share the same height even when a larger
progress count wraps onto another line. The Sync button no longer sinks below
the action row or gets cut off at narrow widths and larger font sizes.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
