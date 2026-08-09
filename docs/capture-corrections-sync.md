# Capture corrections and asset lifecycle sync — desktop → cloud → Android

The Corrections manager cleans up photo data from phone captures that were
synced down from the cloud. This document pins the contract by which those
desktop corrections are published back to the cloud and reflected on Android.
All three legs (SQL migration, desktop publisher, Android consumer) implement
this contract; do not diverge from it without updating this file.

## Page deletion and restore

A desktop page delete is a reversible visibility change, not destruction of a
capture asset. The matching `photo_assets.json` asset stays in `assets[]` with
its stable `asset_id`, original `capture_order`, processing `lifecycle`,
original/display records, OCR geometry, and correction history. Desktop adds
this separate object:

```json
{
  "desktop_lifecycle": {
    "state": "deleted",
    "revision": 4,
    "updated_at": 1722000000123
  }
}
```

The object has exactly `state`, `revision`, and `updated_at`. `state` is
`active` or `deleted`; `revision` is a positive monotonic integer; and
`updated_at` is a positive Unix epoch millisecond. An older asset with no
`desktop_lifecycle` is active at revision zero. Missing data is never treated
as a restore operation. Deleting an interior page does not renumber later
`capture_order` values, remove its camera/display bytes, or erase its
correction and OCR records. Restoring writes `active` at the next revision and
therefore exposes the same asset at the same order.

### Cloud: table `capture_asset_lifecycle` (migrations 027–028)

One current row per `(capture_id, asset_id)`, CAS-updated on the table's
server-monotonic `revision`. Its owner/FK/RLS/trigger rules mirror
`capture_corrections`. Migration 027 creates the contract; migration 028
removes default table privileges and restores only authenticated reads plus
publisher select/insert/update access.

| column | type | notes |
|---|---|---|
| `capture_id` | uuid, FK `captures(id)` on delete cascade | PK part |
| `asset_id` | text, `^[A-Za-z0-9._-]{1,160}$`, excluding `.` / `..` | PK part |
| `owner_id` | uuid, FK `auth.users` | trigger-derived owner |
| `source_original_sha256` | text, `^[0-9a-f]{64}$` | immutable camera anchor |
| `result` | jsonb | `org.whl.capture-asset-lifecycle` v1 |
| `revision` | bigint | server-monotonic row CAS clock |
| `created_at` / `updated_at` | timestamptz | trigger-maintained |

The result document is exact (no additional v1 fields):

```json
{
  "schema": "org.whl.capture-asset-lifecycle",
  "version": 1,
  "capture_id": "<capture uuid>",
  "asset_id": "<stable asset id>",
  "source_original_sha256": "<64 hex>",
  "state": "deleted",
  "capture_order": 2,
  "lifecycle_revision": 4,
  "changed_at": 1722000000123
}
```

`lifecycle_revision` is the semantic clock copied from
`desktop_lifecycle.revision`; the table `revision` only orders CAS writes.
The publisher emits an explicit row for both delete and restore. A capture
with no row for an asset produces no Android mutation.

### Android lifecycle consumer

Android fetches lifecycle rows alongside corrections and applies lifecycle
rows first. It revalidates row and result identities, authenticated owner,
original sha256, and the unchanged
`capture_order`. Only a strictly greater `lifecycle_revision` changes the
persisted `desktop_lifecycle`; a lower revision is a no-op and a same-revision
collision fails closed. A missing incoming asset or missing cloud row is also
a no-op.

Deleted records remain serialized but are excluded from ordered assets,
detail/thumbnail descriptors, page counts, and later OCR/reprocessing. Their
files and all nested history remain on disk. A newer `active` record makes the
same stable asset visible again. Both cloud-processing results and desktop
correction installs treat a currently deleted asset as not applicable, so
pixel updates cannot override its visibility.

## Shape

A desktop correction transform commits a corrected display rendition of one
capture photo locally (engine `correction-transforms` publication). The
publisher uploads a JPEG transcode of that rendition to the private
`capture-derivatives` bucket and CAS-writes one row per `(capture_id,
asset_id)` into a new `capture_corrections` table. Android pulls those rows
alongside the existing `capture_book_metadata` / `capture_reviews` families,
validates them against the immutable camera original's sha256 (which both
sides hold), downloads the corrected display, and installs it as a new local
display revision — never touching `photo_N.jpg` or `original_*` files, exactly
like the existing cloud-processing install path.

Anchor identity: `(capture_id, asset_id, original sha256)`.
- Desktop side: `DATA_ROOT/captures/<capture_id>/photo_assets.json` →
  `desktop_import.assets[]` carries `asset_id` and `source_checksum`
  (sha256 of the transported camera original).
- Phone side: `photo_assets.json` asset record carries the same `asset_id`
  and `original.sha256`.
- Desktop display renditions do NOT byte-match phone display renditions, so
  display checksums are never used as the anchor.

After the first local display replacement, desktop removes only the local
`raw_ref` and records a versioned `original_backup` descriptor on the matching
`desktop_import.assets[]` row. The portable `asset.original` identity,
checksum, revision, dimensions, and orientation remain unchanged for Android;
the backup key is desktop-private and is stripped from `.lib` projections.

The verified camera bytes are stored below
`output/backups/originals/v1/sha256/`, split into a two-hex directory and a
62-hex filename, in the same recoverable transaction that publishes the
transform receipt, logical display head, active operation, capture manifest,
and item timestamp. Transforms begun from either the display or the separately
exposed original leave the base `photo_N.jpg` and capture geometry unchanged;
the validated logical display head supplies corrected pixels and mapped
geometry while the original remains immutable evidence. Restore verifies and
copies the cold object into the stable display slot, clears active geometry,
deletes the display head, and retains the shared backup object. Routine index,
detail, preview, and editor reads never stat or open the cold object; only View
original, Restore original, and an explicit archive build may resolve it.

## Cloud: table `capture_corrections` (migration 024)

One row per `(capture_id, asset_id)` — latest correction wins, superseded in
place via CAS on `revision`.

| column | type | notes |
|---|---|---|
| `capture_id` | uuid, FK `captures(id)` on delete cascade | PK part |
| `asset_id` | text, `^[A-Za-z0-9._-]{1,160}$` | PK part |
| `owner_id` | uuid, FK `auth.users` | trigger-derived from `captures.created_by`, never client-supplied |
| `correction_id` | text, `^[0-9a-f]{64}$` | the engine transform command fingerprint |
| `source_original_sha256` | text, `^[0-9a-f]{64}$` | camera-original anchor |
| `result` | jsonb, ≤ 64 KiB | `org.whl.capture-correction-result` v1 (below) |
| `revision` | bigint | server-monotonic (reuse `prepare_capture_phone_sync_row()` trigger from migration 017) |
| `created_at` / `updated_at` | timestamptz | trigger-maintained |

Grants/RLS mirror `photo_processing_jobs` (015): `service_role` full;
`authenticated` SELECT where owner or ingest-grant. Anon blocked. Writer is
the desktop under the owner service credential (same trust level as
`capture_book_metadata`).

Storage: an additional `capture-derivatives` SELECT policy (do not modify the
015 policy) exposing exactly the object names present in
`capture_corrections.result` artifact paths, owner-or-ingest-grant, mirroring
`capture_derivatives_select_authorized`.

## Result document: `org.whl.capture-correction-result` v1

```json
{
  "schema": "org.whl.capture-correction-result",
  "version": 1,
  "processor": "whl-desktop-corrections",
  "recipe": "whl-desktop-correction-v1",
  "correction_id": "<64 hex — transform command fingerprint>",
  "source": {
    "original_sha256": "<64 hex>",
    "desktop_display_sha256": "<64 hex, optional, informational>"
  },
  "geometry_strategy": "replace_and_reocr",
  "artifacts": {
    "display": {
      "bucket": "capture-derivatives",
      "path": "<owner_id>/<capture_id>/<asset_id>/desktop-<correction_id[:20]>/display-<sha256[:20]>.jpg",
      "sha256": "<64 hex of the JPEG bytes>",
      "bytes": 0,
      "width": 0,
      "height": 0,
      "content_type": "image/jpeg"
    },
    "thumbnail": {
      "bucket": "capture-derivatives",
      "path": ".../thumbnail-<sha256[:20]>.jpg",
      "sha256": "…", "bytes": 0, "width": 0, "height": 0,
      "content_type": "image/jpeg"
    }
  },
  "generated_at": "<UTC ISO-8601>"
}
```

- `geometry_strategy` is always `replace_and_reocr` in v1: desktop-corrected
  pixels have no homography chain to the phone's display rendition, so the
  phone drops OCR geometry and re-runs its Mistral OCR (existing
  `.cloud-reocr` marker + `CloudDisplayReocrWorker` machinery).
- Object path segment 4 starts with `desktop-`, which cannot collide with the
  Cloud Run worker's `r<revision>-<request_id>` segment.
- `thumbnail` is optional but the desktop publisher always writes it.

## Desktop publisher

Runs inside the credentialed cloud-sync pass (`_cloud_sync_run_with_configs`)
after the capture book-metadata sync, under the owner service credential;
silently skipped when the service credential is absent (contributor desktops).

Stateless diff-and-publish (no local outbox):
1. Candidate captures: capture-backed manual entries (`capture_id` present).
   `capture_transport` is never consulted (legacy entries imported before the
   field exists lack it); cloud-row existence in step 4 is the authoritative
   gate.
2. For each candidate, map committed correction-transform publications to
   `(capture_id, asset_id)`: the publication's `command.artifact_id` must
   resolve — transitively through prior transform outputs — to
   `capture:<opaque-ns>:display` or `:original`, where `opaque-ns` is the
   corrections artifact repository's `_opaque_identity` math: the first 40 hex
   of sha256 over the canonical JSON array
   `json.dumps((capture_id, asset_id), sort_keys=True, separators=(",", ":"))`
   — i.e. `["<capture_id>","<asset_id>"]`. Item pointers, publications,
   and receipts must bind to one another exactly before a publication
   participates. Once the desktop import row carries a validated
   `original_backup` marker, its `active_desktop_correction_id` is the current
   state authority: only that exact receipt-witnessed operation may publish,
   and a backed-up row without an active operation is restored and produces no
   candidate. The active operation must also match the validated
   `correction-display-head`. That head is authoritative only if its ancestry
   roots at the current authority of the selected capture rendition: the
   derivative checksum for a `:display` root or the immutable source checksum
   for an `:original` root. Either source replaces only the sibling logical
   `:display` slot; the original remains immutable history. An invalid or stale
   head fails closed, and the publisher never substitutes an unrelated history
   leaf. Legacy rows without the backup marker use the validated display head
   when present and otherwise retain the history-derived winner behavior.
   Publications for lifecycle-failed assets are skipped. Independent
   original/display branches converge on the last transform that commits
   successfully to their shared logical display head.

   Histories without a current-state marker or display head use this ordering
   signal: **chain ancestry** — a publication
   whose resolution chain passes through another candidate's outputs
   supersedes it (content-derived; the engine never stamps
   `outputs[].provenance.generated_at` for correction transforms, and
   pointer mtimes do not survive by-item index backfills or DATA_ROOT
   restores); chain-unrelated siblings order by recorded `generated_at`
   when non-empty (future-proofing), else by the by-item pointer mtime.
3. Transcode the committed `corrected-display` PNG → JPEG: RGB, long edge
   ≤ 1600 px, quality 90, no EXIF. Thumbnail: long edge ≤ 512 px, quality 80.
4. Read existing `capture_corrections` rows for the candidates; skip an
   `(capture_id, asset_id)` only when its complete writable row/result contract
   matches (the original anchor and both artifact envelopes included).
   `result.generated_at`, the server revision, and trigger-derived fields are
   intentionally ignored. A malformed or incomplete same-id row is therefore
   republished instead of remaining permanently un-installable on Android.
   Skip captures with no cloud row (FK would fail; LAN-only captures). When the
   row differs, two overwrite guards apply before publishing:
   - **Downgrade guard** — if the row's `correction_id` is one of this
     desktop's own candidate publications for the asset, a different local
     winner selected by a validated display head replaces it regardless of
     disturbed history mtimes. A pre-head winner publishes only when it sorts
     strictly newer under the step-2 order (chain ancestry, else a strictly
     later order signal). A legacy tie —
     e.g. equal or disturbed pointer mtimes — skips the asset with a
     per-capture notice instead of overwriting, so mtime damage becomes a
     no-op rather than a downgrade the phone would reinstall. The explicit
     active operation on a validated backup marker bypasses this local-history
     heuristic because the capture manifest is then the durable current-state
     authority.
   - **Foreign rows** — if the row's `correction_id` is not locally known
     (another desktop authored it), publish only when the local winner's
     `correction_id` is lexicographically greater; otherwise skip with a
     notice. Every desktop applying the same rule converges on one stable
     winner instead of ping-ponging the row.
5. Upload objects (`x-upsert`), then re-resolve the capture association,
   manifest, display head, complete local authority token, and immutable object
   checksum. Content-addressed uploads that lose this race are harmless orphan
   objects. For each capture whose token remains exact, hold its capture stripe
   and the Corrections authority lease through the row CAS (insert
   `on_conflict` ignore → PATCH `revision=eq.N`, mirroring
   `push_capture_book_metadata`).

Failures are reported per capture in the sync summary and never abort the
rest of the run. Re-running converges (uploads are content-addressed and
upserted; row writes are CAS'd). One exception is not a failure: when the
cloud project lacks the `capture_corrections` table (migration 024 not
applied), the publisher marks its stage skipped with a notice and the run
still succeeds — mirroring the Android consumer, which tolerates the
missing table.

## Android consumer

Pulled by `CaptureMetadataSyncWorker` for `Entries.recent()` cloud-uploaded
entries, alongside the existing three row families.

Validation (all must hold, else the row is ignored):
- `result` schema/version/processor/recipe exactly as above;
  `result.correction_id == row.correction_id`.
- Local asset found by `asset_id`; `asset.original.sha256 ==
  source_original_sha256`.
- `artifacts.display`: bucket `capture-derivatives`; path is exactly
  `<owner_id>/<capture_id>/<asset_id>/desktop-<correction_id[:20]>/display-<sha256[:20]>.jpg`
  for the row's own ids; `bytes` ≤ 32 MiB.

Install (mirrors `installCloudDisplayDerivative`):
- Download via the authenticated storage endpoint; verify byte count, MIME,
  JPEG structure, dimensions, sha256.
- Write `desktop_<assetId>_r<newRevision>_<sha256[:20]>.jpg` in the entry
  directory; bump `display.revision` by 1; set display recipe
  `whl-desktop-correction-v1`; record the applied `correction_id` and last
  acknowledged server row `revision` on the asset (optional contract fields;
  the revision is absent on older entries).
- Drop OCR geometry and write the re-OCR pending marker so
  `CloudDisplayReocrWorker` re-runs OCR against the corrected pixels.
- Never overwrite `photo_N.jpg` or `original_<assetId>.jpg`.
- Treat a currently deleted asset as not applicable; lifecycle rows are
  applied before correction rows in each pull.

A row whose `correction_id` matches the applied one is a no-op only after the
full row/artifact contract and installed display metadata validate and the
installed file's bytes, dimensions, and sha256 match the artifact envelope.
Missing or damaged same-id pixels are downloaded and replaced at the existing
display revision; a mismatched display contract is reinstalled at a new local
revision. A newer valid row with unchanged pixels only advances the persisted
ordering evidence. Overlapping pull and explicit-sync workers compare that
server revision under the entry lock so a newer row may replace an intervening
older install, while an older download can never roll it back. A newer row with
a different `correction_id` supersedes and bumps the display revision; the
server row revision alone never forces a pixel reinstall.

Curated title, author, and year do not ride inside `capture_corrections`.
They continue through the existing `capture_book_metadata` projection. The
capture list overlays any bibliography present there field by field on the
phone's extracted text. This includes live imported-but-unregistered captures,
whose projection intentionally has an empty `book_id`; an empty deletion
tombstone naturally falls back to the phone values.

## Known limits (acceptable pre-1.0)

- Corrections reach only entries still in `queue/`/`sent/` on the phone
  (`Entries.recent()` boundary); archived or deleted entries ignore them.
- Poll-driven: the phone notices corrections on its next metadata pull
  (Home resume / explicit sync), not in realtime.
- Page reorder is not expressible. Removal and restoration round-trip through
  the separate lifecycle transport and deliberately preserve order gaps.
- Metadata corrections outside the existing `capture_book_metadata`
  bibliography (categories, roles, captions) stay desktop-local; the
  corrections transport itself only round-trips corrected pixels in v1.
- Existing sealed `.lib` source packages remain immutable and may retain their
  embedded camera representation. Rebuilding one explicitly reads and verifies
  the cold object, then strips the desktop-private backup locator from the
  portable manifest; routine Corrections raster projection does neither.
- No revocation flow: deleting a correction locally does not retract a
  published row. Restoring the local original likewise suppresses future
  publication without deleting an existing cloud row or derivative object.
- Two desktops that each hold a different correction for the same
  `(capture_id, asset_id)` do not merge: the lexicographically greater
  `correction_id` wins the cloud row, and the losing desktop keeps its
  version locally, skipping with a notice on every sync.
- Until migration 024 is applied to the cloud project, the publisher skips
  its stage with a notice (the run still succeeds); corrections stay local
  and publish on the first sync after the migration.
