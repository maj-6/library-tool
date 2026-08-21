# Library Tool Capture 0.5.2-alpha.16

Android-only prerelease. `versionCode` 47.

This release turns **Inspect** into a book locator and makes the digitization
queue visible wherever a book appears in the Android app.

## Find which collection holds a book

Type a title in Inspect to search across collections, or photograph a cover and
let Mistral OCR 4.1 read its title and author. The cover image is resized into
the app's temporary cache, sent to Mistral using the API key configured in
Settings, then deleted from the phone after recognition. The app ranks matches
from the owner's local and cloud inventory and opens the selected collection
without creating or changing a capture.

Exact titles rank first, author and year resolve close matches, and ambiguous
books remain separate results instead of silently choosing a collection. Cloud
lookup failures are disclosed alongside any local matches.

## See scan priority everywhere

Every Android book representation now shares the same scanner overlay and
subtle candidate highlight: Home, Tiles/Content/Icons in Inspect, Archive, the
last-captured card, and full book details. A synchronized `scan_priority` from
1 (highest) to 5 (lowest) appears on the badge. A candidate that has not yet
been assigned a priority shows `?` rather than receiving an invented rank.

Candidate and priority metadata survive Inspect cache refreshes, collection
moves and merges, delivered-media pruning, app restarts, and older cache schema
upgrades. Accessibility labels announce the candidate and its priority without
confusing the highlight with Inspect's real selection state.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
