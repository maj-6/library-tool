# Library Tool Capture 0.5.2-alpha.5

Prerelease. `versionCode` 36.

This build is a response to a single report — "numerous captures say failed,
syncing again says nothing is ready, and some say partial" — which turned out
to be three unrelated problems wearing one costume.

## "failed" was never about syncing

A capture row reading **failed** was reporting this phone's text extraction,
not delivery. The photos had already been uploaded. That is why pressing
**Sync** afterwards correctly reported nothing outstanding: there genuinely was
nothing left to send.

The two failures are now named separately. A row says whether the upload failed
or the reading of the book failed, and a capture whose photos are safely
delivered no longer presents as data at risk.

The immediate cause of the extraction failures on 2026-07-29 was the OCR
provider rejecting the configured key with HTTP 401. Nothing was lost: the
photos are on the desktop and in the cloud, and the affected captures can be
reprocessed once the key is valid.

## Sync explains an empty run

Pressing **Sync** with nothing outstanding used to say only that no captures
were ready, which reads as a contradiction next to a screen of red rows. It now
distinguishes the cases: a queued review edit, an open live capture, everything
delivered, and — the case that caused this report — everything delivered but
some captures having failed their processing.

## "partial" no longer means "the year wasn't quoted"

Extraction asks the model for a JSON object of bibliographic fields. It kept a
single list of problems, and any deviation from the requested shape marked the
result partial. A year returned as the JSON number `1897` instead of the string
`"1897"` was enough, even though the value was perfectly intact.

Because extraction runs at temperature 0, that verdict was permanent: the same
book reproduced the same shape on every retry, so the capture stayed partial
forever.

Deviations are now graded. A **defect** means a field's value was not
recovered — missing, null, or an object where a string belongs. An **advisory**
means only the shape differed and the value came through: an unquoted number or
boolean, or an omitted `extra` block. Advisories are still reported in the
diagnostic warning, but only defects make a capture partial.

Books already marked partial for a shape-only reason report as complete once
reprocessed.

## Retention archives instead of deleting

Delivered captures used to be deleted once they aged out, which is what made
the above impossible to investigate after the fact. They are now archived: the
directory moves aside and keeps every textual sidecar — bibliography, notes,
processing state, catalogue decisions.

When the photo budget is reached, an archived capture drops its photos and
keeps its record, rather than the capture disappearing. Such a capture is
labelled **record only** rather than showing zero pages, because a photo-free
capture is a deliberate outcome of the budget and must not read as a bug.

Archived captures are read-only. No worker enumerates the archive, so an edit
made there could never reach the cloud; offering one would be a lie.
`Entries.recent()` remains the boundary that keeps archived captures out of
upload and inventory.

## Browsing and exporting

**Settings › Captures on this phone** gains a storage readout and two new
abilities:

- **Browse archive** opens a read-only list of archived captures, newest first,
  each showing when it was archived, how many pages it still has, and its
  status. Tapping one opens the usual detail screen.
- **Export** copies captures to a folder you choose through the system file
  picker — internal storage, an SD card, or a cloud provider. Exported captures
  survive uninstalling and reinstalling the app.

Export runs as a background worker and is safe to leave; it writes each capture
into its own folder and skips ones already present at the destination.

## Known caveats

- Reprocessing a capture whose extraction failed still requires a working OCR
  provider key. This build does not add a key-validity check to Settings.
- The archive has no automatic size ceiling of its own beyond the existing photo
  budget; a phone that captures heavily will accumulate record-only entries.
- `WhlIndexTest` fails on Windows checkouts because Git rewrites the tracked
  catalogue's line endings, changing the hash the test compares. It passes on
  CI. This is a test-harness defect, not a product one.
