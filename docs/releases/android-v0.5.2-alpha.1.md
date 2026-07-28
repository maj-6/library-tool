# Library Tool Capture 0.5.2-alpha.1

Android version code: `32`.

This Android-only prerelease hardens the cloud-to-phone round trip for retained
captures and cloud-only box listings.

## What changed

- Home resume and **Sync captures** now apply the cloud import status even after
  the bounded post-upload polling chain has ended. Desktop archive confirmation
  and status use one ordered, idempotent local commit.
- A late `pending` or stale archive response cannot regress a newer terminal
  import result or confirmation already stored on the phone.
- Inspect requests box summaries with an explicit signed-in-owner predicate,
  validates every returned owner and collection, and follows stable UUID cursor
  pages until the cloud returns an empty page.
- Resume and explicit Sync re-arm box listings. Generation-guarded cache commits
  keep older in-flight responses from overwriting a newer refresh or another
  account's state.
- Cache cleanup removes only collection IDs explicitly recorded as deleted or
  merged, within the same serialized commit as the refreshed listing.

## Suggested device checks

1. Upload a capture, import it on the desktop after the phone's initial polling
   has stopped, then return to Home or tap **Sync captures**. Confirm the phone
   advances from pending to the terminal cloud result.
2. Add or import a capture into an existing box from another device, open that
   box in Inspect, tap **Sync captures**, and confirm the cloud-only summary
   appears without restarting the app.
3. Switch accounts or rapidly leave and revisit Inspect while a box refresh is
   running. Confirm one account's books never appear in the other account's
   cache.

Cloud round-trip listings remain photo-free summaries. Imported camera files
cannot be restored because the existing cloud workflow intentionally deletes
the remote objects after verified desktop import.

## Validation

- 440 Android unit tests passed.
- Android debug and release lint passed, and the debug APK assembled.
- 172 focused cloud-contract tests passed.
- A read-only production audit found 475 captures, no missing owners, and a
  largest owner/box listing of 68 rows.

This is a testing build. Please report the phone model, Android version, source
and destination accounts, collection tag, and observed sync state with any
round-trip issue.
