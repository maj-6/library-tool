# Library Tool Capture — Android Changelog

Android releases are listed newest first. Prerelease entries describe builds
intended for testing before a stable release.

## 0.5.2-alpha.6 — 2026-08-01

Android version code: `37`.

### Bugfixes

- A failed item in **Retry reading details** no longer cancels every capture behind it and leaves Sync stuck after its first successful upload.
- Captures stranded by 0.5.2-alpha.5 are recovered automatically after updating; **Retry sync** also remains available while processing or networking is waiting.
- Cloud continuations now use Android's connected-network scheduling, including Android 15 and 16 fixes from the current WorkManager release.
- Photo uploads have bounded write and whole-call timeouts, so an unresponsive connection cannot hold the capture queue forever.

## 0.5.2-alpha.5 — 2026-07-30

Android version code: `36`.

### Additions

- Captures that age out of the recent list are archived instead of deleted, and can be read from **Settings › Captures on this phone › Browse archive**.
- When the photo budget is reached an archived capture keeps its bibliography, notes, and processing history and drops only its photos, so the record survives rather than the whole capture disappearing.
- Captures can be exported to a folder you choose, including one on an SD card or a cloud provider, so they survive reinstalling the app.

### Bugfixes

- A capture whose text extraction failed no longer looks like a failed upload. Its photos had already been delivered; the row now says which of the two failed.
- Pressing **Sync** when nothing is outstanding explains why instead of only saying no captures are ready, so a screen of failed rows and an empty sync no longer contradict each other.
- Books are no longer marked **partial** because the extracted year arrived as a number rather than as text. Only a field whose value was actually lost makes a capture partial, and affected books stop reporting partial once reprocessed.

## 0.5.2-alpha.4 — 2026-07-29

Android version code: `35`.

### Additions

- Say **check** while a capture is open to photograph a book, extract its bibliography, and immediately search both the CH and World Herb Library catalogues.
- The World Herb Library snapshot now ships inside the APK for fast on-device matching, including distinct published, draft-only, and no-match results.
- Catalog-check results show the identified title, author, and year together with the CH and WHL verdicts without requiring the capture to be finished first.

### Other Changes

- CH and WHL catalogue searches run entirely on-device after OCR and extraction; no catalogue query is sent over the network.
- Check requests survive camera queuing, retries, activity recreation, and process interruption without attaching a result to the wrong photo.

## 0.5.2-alpha.3 — 2026-07-28

Android version code: `34`.

### Additions

- After a scan the phone checks the book against the CH master list on its own, with no network and no desktop, and shows the result on the book's row.
- Book rows carry a compact row of catalogue indicators for the CH list, the World Herb Library, and the Internet Archive, in a fixed order and readable without relying on colour.
- Tapping the CH indicator accepts a proposed match and opens a preview of the merge, marking which values came from the CH list; a long press rejects the match. Values you captured are never overwritten, and where the two disagree the CH reading is shown beside yours rather than replacing it.
- An accepted match contributes the publisher, city, edition, page count, condition, illustrations, price and subject keys the CH list holds for that book.

### Other Changes

- A rescan keeps a decision you already made when it finds the same book again, and asks afresh when it finds a different one.

## 0.5.2-alpha.1 — 2026-07-28

Android version code: `32`.

### Other Changes

- Sync captures and returning to Home now refresh the selected box from the cloud, while the last complete listing remains available offline if a refresh fails.
- Large box listings load in stable pages until the cloud reports no more rows, instead of silently stopping after the first 500 captures.

### Bugfixes

- Normal cloud metadata sync now brings import status and desktop archive confirmation back to retained captures after the initial upload polling window has ended.
- Late pending responses can no longer replace a newer terminal import result or archive confirmation on the phone.
- Cloud-only box summaries are explicitly restricted to the signed-in owner and rejected if a response contains another account, an unexpected box, a duplicate, or a nonadvancing page.
- Account changes and overlapping refreshes can no longer stamp stale box results onto the current cache, while deleted and merged collection keys are cleaned up conservatively.

## 0.5.1 — 2026-07-28

Android version code: `31`.

### Additions

- Collections now record where a batch came from, use stable printable tag IDs, synchronize between devices, and can be opened from a QR code in the Inspect tab.
- Inspect offers Tiles, Content, and Icons views and can list every account-owned book recorded in a box, including cloud-only books and books filed under a tag absorbed by a merge.
- Capture adds tap-to-focus, autofocus lock, zoom, exposure, continuous light, Fast and More Detail profiles, preview sharpening, page-margin guidance, and a fixed portrait or landscape orientation.
- Realtime voice notes recognize Price, Pages, Condition, Illustrations, and Remark, with commands to start or end notes, restart, undo, and edit the latest unsent scan.
- An explicit Sync captures action freezes a recoverable upload batch and exchanges desktop catalogue status, rights records, remarks, attention reasons, and review requests.
- Book details add photo roles, OCR-region overlays, a carousel, corrected-display and retained-original views, and collapsible extraction diagnostics.
- An elapsed timer now shows how long the current book capture has been open and resumes accurately after an activity or process restart.
- About includes the app changelog and documentation, while update checks can refresh validated Android text and icon resources without offering an uncertified APK.

### Other Changes

- Home groups scans by collection, keeps one section expanded, pages long histories, offers a compact list, and distinguishes waiting, uploaded, cloud-only, locally cleared, and desktop-archive-confirmed books.
- A lightweight, photo-free inventory keeps delivered books browsable after local media is cleared, and a fetched box listing stays available offline until the signed-in account changes.
- Captures keep their creator, collection, origin, start time, and destination fixed when the scan begins, and local captures remain local until the user explicitly claims or sends them.
- Cloud image cleanup installs validated corrected display photos atomically, keeps camera originals safe, and realigns OCR regions after perspective or page-curvature correction.
- Collection tags remain reserved after rename, deletion, or merge, and retired tags resolve only through an authoritative merge to the surviving collection.
- A capture's own title remains authoritative when present; otherwise the phone uses the desktop title, author, and year without treating a generated untitled placeholder as real metadata.

### Bugfixes

- Prevented app lifecycle or background processing from starting an upload batch, adding later captures to a batch, or silently changing its selected destination.
- Preserved photos, notes, originals, and durable voice drafts through restart, undo, edit, cancellation, backgrounding, and in-flight photo commits.
- Prevented stale collection editors, duplicate or malformed tags, alias cycles, and damaged inventory files from overwriting newer or recoverable collection data.
- Prevented large scan histories from blocking Home and fixed doubled separators, clipped controls, light-background icons, and ambiguous capture status text.
- Kept concurrent phone and desktop review reasons, rejected malformed or oversized sync rows independently, and recovered safely after a paired desktop revision-ledger reset.
- Fixed corrected images displaying OCR regions from the uncorrected geometry and prevented cloud cleanup from removing an original before its replacement was ready.
- Box inspection no longer reports an unreachable box as empty, loses books filed under merged tags, or drops its offline listing before an account change.
- Delayed, stale, malformed, or conflicting desktop archive confirmations no longer replace a newer confirmation or show an unconfirmed capture as safely archived.

## 0.5.1-alpha.11 — 2026-07-25

Android version code: `30`.

### Additions

- Scanning a box QR now lists every book recorded in that box from your account in the cloud, not only the ones still held on the phone. A reinstalled or second phone sees the whole crate.
- Books listed from the cloud are labelled as such and show the title, author and year the desktop has for them.

### Other Changes

- A box listing is kept on the device after it is fetched, so a crate browsed once stays readable offline, and it is discarded when a different account signs in.
- A box's listing covers the tags it absorbed through a merge, so books recorded under a retired label still appear under the surviving box.
- Inspect now distinguishes a book whose local photos were cleared from one this phone never captured, and no longer reports an empty box as empty in the cloud when it could not reach it.

## 0.5.1-alpha.10 — 2026-07-22

Android version code: `29`.

### Additions

- Added short, editable collection tag IDs for printed box labels. Tags are canonical, globally unique, and remain stable when a collection is renamed.
- Added an Inspect tab with an on-device QR scanner and Windows-like Tiles, Content, and Icons views for browsing the books recorded in a box.
- Added a durable, photo-free collection inventory so delivered scans remain visible in Inspect after old local media is cleared.

### Other Changes

- Collection sync now carries tag IDs in both directions, upgrades older local collection stores deterministically, and preserves retired tags so printed labels are never silently reused.
- Scanning a merged collection's former tag follows the authoritative merge to the surviving collection without changing the collection selected for the next capture.
- The Inspect layout choice is stored on the device, and cleared scans keep only a lightweight, photo-free bibliographic summary for box browsing.

### Bugfixes

- Prevented large scan histories from blocking Home by keeping one collection expanded and paging its rows in fixed-size windows.
- Prevented a collection editor opened before sync from overwriting a newer cloud tag when the tag itself was left unchanged.
- Rejected duplicate or malformed tags and broken merge-alias chains instead of opening an ambiguous collection.
- Preserved corrupt or unknown inventory files for recovery instead of replacing them during pruning.

## 0.5.1-alpha.9 — 2026-07-22

### Additions

- Added an explicit Sync captures action; cloud uploads now wait for the user and sync only the captures that were ready when the action was pressed.
- Added desktop-to-phone catalog status sync over cloud and paired LAN for copyright and registration records, WHL and Internet Archive availability, scan status, remarks, and review state.
- Added Needs attention and Needs review controls, with an optional reason, to scan rows and the latest-book capture preview.
- Added the Edit voice command for reopening the latest scan while it is still unsent so more photos or notes can be added.

### Other Changes

- Tapping a scan now opens its details, while long-pressing marks it as needing attention instead of entering selection mode.
- Scan rows use compact copyright, availability, scan, remarks, and attention indicators; tapping the copyright tag shows located registration and renewal records.
- Long-pressing a capture thumbnail now deletes that photo immediately and safely compacts the remaining page files, with recovery if Android stops during the operation.

### Bugfixes

- Prevented lifecycle and background-processing work from silently starting a new capture upload batch.
- Made explicit metadata/review sync crash-safe across delivery, additive when desktop and phone reasons change together, isolated from malformed or oversized rows, and recoverable after a paired desktop revision-ledger reset.
- Preserved capture photos, OCR, notes, and originals when a scan is reopened for editing while invalidating only stale extraction results.
- Prevented partial speech recognition from accidentally triggering the Edit command.

## 0.5.1-alpha.8 — 2026-07-20

### Additions

- Added an app-menu About view with the Library Tool Capture icon, linked Android documentation, and a scrollable release changelog.
- Added a camera-and-scan popup with tap-to-focus, autofocus locking, zoom, exposure compensation, continuous light, scan profile, and preview-sharpening controls.
- Added a page-margin guide with dimmed outer edges and a fixed-position portrait/landscape capture toggle.
- Added realtime Mistral voice notes with compact structured rows for Price, Pages, Condition, Illustrations, and Remark, plus notes, end notes, restart, and undo voice commands.
- Added catalog-oriented book details, title-page and cover/spine photo roles, OCR-region overlays, a photo carousel, and collapsible JSON and Mistral-response diagnostics.
- Check for updates now refreshes a validated remote catalog of Android strings and in-app icons without offering uncertified APK updates.

### Other Changes

- Home now uses separated icon tabs, a full-height botanical-green app mark, regular-case labels, long-press multi-selection, and an icon-marked New scan action.
- Collections use icon actions and a light-blue bordered current state; scans are grouped into collapsible collection sections with the current collection expanded initially.
- Capture keeps the last submitted book preview and exposes only additional detected fields through its compact extra-fields popup.
- Waiting and uploaded scans use animated or icon status indicators instead of Pending, Complete, and Uploaded text tags.
- The launcher icon is slightly larger on a botanical-green background, and the app menu now includes Sign out with About separated at the bottom.
- Android now follows cloud image-processing jobs through completion, validates artifact lineage and bytes, and atomically installs corrected display photos while retaining the camera originals.
- Nonlinear perspective and page-curvature corrections now regenerate OCR-region geometry against the corrected display image, with durable retry markers until the aligned regions are stored; original OCR text and catalog metadata remain unchanged.
- Pending cloud cleanup keeps the original safe and uses softened thumbnails until the corrected display photo is ready.

### Bugfixes

- Fixed the published prerelease omitting Home, collection, camera-control, voice-note, and book-detail work that was already described in the Android release notes.
- Fixed doubled separators between adjacent scan rows and corrected light-background chevrons and scan-page action icons.
- Fixed cancel requiring confirmation and hardened restart, undo, in-flight capture, and voice-note persistence behavior.
- Fixed OCR bounding regions failing to follow validated perspective transforms applied to corrected display photos.

## 0.5.1-alpha.7 — 2026-07-19

### Additions

- Camera voice notes use realtime transcription and recognize Price, Pages, Condition, Illustrations, and Remark as structured fields.
- Voice commands can start and end notes, restart a capture, or undo the most recent photo or note.
- Scans are grouped into collapsible collections, with the current collection highlighted and expanded by default.
- A compact scan-list option provides smaller thumbnails and tighter spacing.
- Book details distinguish primary, secondary, and other metadata and provide collapsible JSON and Mistral-response diagnostics.
- Photo views can show OCR regions, corrected display copies, and the retained camera original.
- Settings include publication-date presets for page cleanup and controls for OCR-region overlays.

### Other Changes

- The capture view now keeps the most recent book preview beside the camera controls and exposes additional detected fields in a compact popup.
- Cancelling a capture is immediate, and scan, collection, upload, edit, save, and delete actions use clearer icon controls.
- Pending scans use a waiting indicator, while completed and uploaded states rely on compact visual markers.

### Bugfixes

- Fixed restart and undo commands targeting an older photo when the latest capture had not finished committing.
- Fixed voice-note drafts being lost when transcription was still draining or the app moved to the background.
- Fixed adjacent scan rows drawing doubled separators.

## 0.5.0 — 2026-07-15

### Additions

- Added scan selection, deletion, and guided reprocessing.
- Added Google and GitHub sign-in, recent scans, extracted book details, and upload status.
- Added background OCR and cataloguing, with captured text and details available to the desktop app.

### Other Changes

- Scan sync now uses the same signed-in account as the desktop app.

## 0.4.0 — 2026-07-11

### Additions

- Scans can be sent directly to a paired desktop over a local network, including without internet access.
- Added page-edge guidance and an optional sharpened viewfinder.

## 0.3.0 — 2026-07-11

### Additions

- Added a home screen for recent scans so the app no longer opens directly to the camera.

## 0.2.0 — 2026-07-10

### Additions

- Added account sign-in, background processing, recent scans, and a desktop-aligned visual design.

### Other Changes

- Version numbering moved to the 0.x series to reflect the app's prerelease status.

## 1.0 — 2026-07-09

### Additions

- Added voice-driven book photography and cloud delivery for processing.
