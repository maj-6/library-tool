# Library Tool Capture 0.5.2-alpha.12

Android-only prerelease. `versionCode` 43.

This release makes **Inspect** faster to scan and adds safe multi-book
organization directly on the phone.

## Select, move, and delete books

Long-press a book in Inspect to begin a selection. Tap more books to add or
remove them, then use the contextual actions to move the selection to another
collection or delete it. Selection survives a screen rotation, and the active
capture is protected from deletion.

Moves and deletes are saved locally first and synchronize to the book owner's
cloud account. Interrupted uploads, account changes, and failed local cleanup
remain retryable instead of losing the organizational change.

## Denser, more consistent Inspect views

- **Icons** gives every book the same footprint, regardless of title length or
  cover proportions.
- **Tiles** uses smaller covers, tighter line spacing, and less vertical
  padding so more books fit on screen.
- **Content** is now a compact text row: a small cover-color swatch followed by
  title, author, and year, with no cover image or action icon.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
