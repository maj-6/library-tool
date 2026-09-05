# Library Tool Capture 0.5.2-alpha.22

Android-only prerelease. `versionCode` 53.

## Find a collection directly

Inspect now uses a searchable collection picker instead of the horizontal box
carousel. Search by any part of a collection's hierarchy path or name, its
printed tag ID, or its recorded origin. Results show the path, tag, origin, and
known book count so similarly named boxes can be distinguished before opening
one.

Matching uses the collection's stable internal ID rather than reverse-mapping
visible text. Suggestion results are ranked and capped at 24 using the
already-loaded snapshot, so typing does not reread the inventory or start cloud
requests. If a typed query is dismissed without choosing a result, the field
restores the box that is actually open.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
