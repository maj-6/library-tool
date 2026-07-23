# Library Tool Capture 0.5.1-alpha.10

Android version code: `29`.

This Android prerelease adds a physical-box inspection workflow while retaining
the explicit capture sync, desktop-status, and phone-review behavior introduced
in 0.5.1-alpha.9.

## Highlights

- Every collection now has a short, editable tag ID for printed labels and QR
  codes. New tags are derived from the collection name, remain stable across
  renames, synchronize with the cloud, and stay reserved after deletion or
  merge so an old label cannot silently identify another box.
- Home has a new Inspect tab. Its on-device QR scanner opens a matching
  collection without changing the collection selected for the next capture.
- A selected box can be browsed in distinct Tiles, Content, or Icons views, and
  the chosen layout remains a device-local preference.
- A lightweight, photo-free collection inventory retains the bibliographic summary
  needed by Inspect before delivered scan folders are pruned. Cleared photos and
  their local detail views are not retained.
- Cloud migration 018 adds deterministic tag backfill, canonical-format and
  global-uniqueness constraints, a safe allocator for older clients,
  column-scoped authenticated grants, and permanent historical reservations.
- Migration 019 adds a reservation-owner index and an explicit deny policy on
  the private ledger.

## Compatibility and safeguards

- The Scans tab keeps only one collection expanded and displays its history in
  fixed 24-scan pages, preventing a large capture backlog from building an
  unbounded Android view hierarchy.
- Version-three Android collection stores upgrade deterministically to the
  tag-aware version-four format; duplicate legacy names are numbered by durable
  UUID order rather than local file order.
- QR lookup can follow an authoritative collection-merge alias, while
  duplicates, malformed tags, missing targets, and alias cycles fail closed.
- A collection editor opened before sync preserves a newer cloud tag when the
  tag field itself was not changed.
- Corrupt or unknown inventory files remain untouched for recovery, and sent
  media is pruned only after the inventory summary has been committed.

Migrations `018_collection_tag_ids` and
`019_collection_tag_reservation_hardening` were applied database-first on
2026-07-22. Post-deploy checks found 18 valid, unique collection tags, matching
permanent reservations, successful authenticated legacy allocation, and no
feature-local security or missing-index advisor finding.

This is a testing build. Please report QR recognition, collection-sync,
inspection-layout, or retained-summary regressions with the device model and
Android version when possible.
