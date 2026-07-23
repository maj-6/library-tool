# Library Tool Capture — Android Changelog

Android releases are listed newest first. Prerelease entries describe builds
intended for testing before a stable release.

## 0.5.1-alpha.10 — 2026-07-22

Android version code: `29`.

### Additions

- Added short, editable collection tag IDs for printed box labels. Tags are canonical, globally unique, and remain stable when a collection is renamed.
- Added an Inspect tab with an on-device QR scanner and Windows-like Tiles, Content, and Icons views for browsing the books recorded in a box.
- Added a durable, photo-free collection inventory so delivered scans remain visible in Inspect after old local media is cleared.

### Other Changes

- Collection sync now carries tag IDs in both directions, upgrades older local collection stores deterministically, and preserves retired tags so printed labels are never silently reused.
- Scanning a merged collection's former tag follows the authoritative merge to the surviving collection without changing the collection selected for the next capture.
- The Inspect layout choice is stored on the device, and cleared scans keep only the bounded bibliographic summary needed for box browsing.

### Bugfixes

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
