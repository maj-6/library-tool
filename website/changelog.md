# Library Tool Desktop — Changelog

Stable releases are listed newest first. Prerelease changes are included in the
next stable release. These notes are also available in the desktop app under
Help → View changelog.

## 0.7.0 — 2026-07-15

### Additions

- Analyze now combines OCR and text analysis in one workspace with a resizable page preview.
- Publish adds a browsable catalogue tree and an online-record preview.
- The Archive Browser adds search suggestions, author pages, and cover thumbnails.
- Copyright search now covers later U.S. registration records.

### Other Changes

- Desktop scan sync now uses the same signed-in account as Library Tool Capture.
- OCR progress and page-level failures are shown in the workspace, and running jobs can stop after the current page.
- Generated About text now updates the Editor description.

### Bugfixes

- Fixed Tesseract OCR being unavailable in installed desktop builds.
- Fixed OCR submissions retaining selected pages or starting twice.
- Fixed Mistral OCR sometimes appearing unconfigured until Settings was opened.
- Fixed PDF previews occasionally failing with permission errors on Windows.
- Fixed offline database status not refreshing after an index was added or downloaded.
- Fixed unsaved Editor changes and verification updates being lost when changing entries.
- Fixed multi-volume books being grouped by similar titles instead of saved volume sets.
- Fixed copyright searches preferring a registration from the wrong year.
- Fixed saved welcome and column-width preferences resetting after relaunch.
- Fixed prerelease update preferences not being applied consistently.

## 0.6.0 — 2026-07-11

### Additions

- The PDF reader adds page jumping and keyboard navigation.

### Other Changes

- Settings and menus were reorganized, with additional controls for editing, OCR, updates, and diagnostics.
- Large PDFs now load more quickly while browsing pages.
- The Downloads and Release notes pages were reorganized for easier scanning.

### Bugfixes

- Fixed long books stopping after the first 400 pages in the reader and text extraction.
- Fixed device-specific layouts and filters carrying over to other computers.

## 0.5.0 — 2026-07-11

### Additions

- Offline catalogue and copyright databases can now be added through the database folder without configuring a download address.

### Bugfixes

- Fixed manually added or downloaded copyright databases not being detected.
- Fixed an extra scrollbar appearing when the desktop interface was scaled.

## 0.4.0 — 2026-07-11

### Other Changes

- Version numbering was reset to the 0.x series to reflect the project's prerelease status.

### Bugfixes

- Fixed repeated launches opening multiple copies of the desktop app and interfering with updates.

## 3.2.1 — 2026-07-11

### Additions

- Added interface scaling under Settings → Appearance, with keyboard shortcuts to zoom and reset.

## 3.2.0 — 2026-07-11

### Additions

- Added AI-assisted summaries, margin annotations, and page-aligned translations for verified volumes.
- Added hierarchical categories for the collection.
- Added an online catalogue with an in-page reader.
- Desktop imports now reuse phone-generated text and book details while retaining contributor credit.

### Other Changes

- Theme selection was reduced to five options.
- The desktop app now opens maximized and restores to a smaller window.

### Bugfixes

- Fixed account confirmation links opening an unavailable local address.

## 3.1.2 — 2026-07-10

### Other Changes

- Future updates install without opening the installer and relaunch the app automatically.

### Bugfixes

- Removed excess blank space from the update progress window.

## 3.1.1 — 2026-07-10

### Additions

- Home now includes the review queue, with comments and controls to resolve or reopen items.

### Other Changes

- Theme selection was reduced to six options, and Sage received an updated palette.
- Cloud sign-in, shared activity, and release downloads now work on a fresh install without additional project setup.

## 3.1.0 — 2026-07-10

### Additions

- Release notes are now available under Help → View changelog and on the website.

### Other Changes

- Updates now install before the main window opens, with progress shown during startup.

### Bugfixes

- Fixed the title bar showing an outdated app version.

## 3.0.1 — 2026-07-10

### Additions

- A first-run guide walks through sign-in, OCR, cloud features, and optional offline search data.
- The desktop app can check for updates on launch, download them in the background, and offer a restart when ready.
- The Downloads page now lists current desktop and Android releases.

### Other Changes

- The Windows installer now allows choosing an installation folder and preserves library data when the app is removed.
- Web links now open in the default browser.

### Bugfixes

- Fixed Layout view for image-only scans processed with Tesseract or Textract.
- Fixed the window close button styling.
- Fixed alignment of actions on website download cards.

## 3.0.0 — 2026-07-10

### Additions

- Added a Windows desktop installer with no separate Python setup.
- Books can include multiple PDF scans, with OCR documents and page actions tracked separately for each scan.
- Signed-in contributions now show the contributor's name in the shared activity feed.
- Home summarizes work in progress, recent drafts, contributors, and open reviews.
- Review items support shared comments and can be resolved or reopened.

### Other Changes

- Editor and OCR panes can be resized, and the scan preview includes a page-layout view.
- Added Platinum, Redmond, and Motif themes.

### Bugfixes

- Fixed temporary cloud connection failures ending signed-in sessions.
- Fixed OCR and page actions for one scan affecting another scan attached to the same book.
