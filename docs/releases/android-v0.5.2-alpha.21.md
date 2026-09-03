# Library Tool Capture 0.5.2-alpha.21

Android-only prerelease. `versionCode` 52.

## See scan assessments everywhere

Curator-assigned High, Medium, Low, and no-scan assessments now appear on Home,
all Inspect layouts, Archive, book details, and the last-captured preview.
Assessment remains independent of scan-candidate membership. Explicit no-scan
uses `N/S`; an unassessed book has no assessment badge. Older numeric candidate
ranks remain compatible.

## Refresh archived metadata safely

Home refreshes both recent and archived book metadata from the applicable LAN
or cloud source. Archived captures stay read-only and excluded from review,
correction, import-state, and asset-lifecycle synchronization. Archive work uses
bounded rotating windows, checks its cadence before loading records, and records
empty checks so repeated Home visits do not traverse the full archive.

Projection merges preserve explicit unassessed values and treat malformed
assessment values as unknown, preventing stale or accidentally erased badges.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
