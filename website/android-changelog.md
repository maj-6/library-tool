# Library Tool Capture — Android Changelog

Android releases are listed newest first. Prerelease entries describe builds
intended for testing before a stable release.

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
