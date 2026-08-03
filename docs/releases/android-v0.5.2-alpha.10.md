# Library Tool Capture 0.5.2-alpha.10

Android-only prerelease. `versionCode` 41.

This release fixes sync attempts that started and then ended before the first
photo reached the cloud. Captures remain safely queued through ownership and
session recovery instead of being poisoned by a local preflight failure.

## Claim local captures explicitly

Cloud and Auto sync now detect valid captures made while no account was active.
Before uploading them, the app asks whether to associate those captures with
the currently signed-in account. Canceling leaves every capture unchanged, and
captures already owned by another account are never adopted.

The confirmation works for the entire selected backlog, reopens captures that
an older attempt had already marked blocked, and prevents duplicate claim flows
while a large queue is being inspected.

## Sync survives session and processing interruptions

- An interrupted token refresh retries without marking the capture blocked.
- A server-rejected access token is expired and refreshed; a real sign-out
  pauses the batch for a visible retry after sign-in.
- Stale reprocessing markers stop delaying photo delivery after their bounded
  grace period.
- If delivery moves a capture from the queue to sent history while forced
  reprocessing is waiting, reprocessing follows the moved capture and completes
  against its current directory.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
