# Library Tool Capture 0.5.2-alpha.8

Android-only prerelease. `versionCode` 39.

This release repairs capture sync batches whose saved state no longer matched
the queue on the phone. Updating preserves the capture queue and its completed
item accounting; it does not require clearing app data.

## The batch count follows the current queue

Starting **Sync captures** compares any active saved batch with the captures
that are actually pending. A stale batch such as **1/23** is expanded in place
to include the full current queue while retaining its completed accounting.
Pressing Sync again does not cancel its healthy WorkManager chain.

The final completion write is conditional on that exact batch snapshot. If a
second press adds captures during the finish window, the worker continues with
another round instead of overwriting the expanded batch as complete. The count
remains per sealed capture rather than per individual page photo.

## Every selected capture is accounted for

A sync round no longer declares success merely because it cannot immediately
select another ready capture. It waits while selected captures are still being
processed and reports a partial result when a target is explicitly blocked or
skipped. Full completion is reserved for a batch whose selected captures are
all delivered.

Transient upload failures are recorded and retried a bounded number of times.
If one capture still cannot be delivered, it is marked with an actionable
error and the cursor advances, allowing later captures in the same batch to
continue instead of leaving the whole queue pinned behind one entry.

## Stranded processing work recovers

Pending processing markers now identify the work that owns them. On startup,
the app rebuilds work for any marker that has no unfinished owner, including
markers left by earlier builds whose work did not carry an owner tag.
Successful terminal processing also clears its marker on every completion
path, so a ready capture cannot remain deferred indefinitely behind a stale
hold.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
