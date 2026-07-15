# Library Tool — Changelog

Newest first, grouped by major version. Within a release the biggest changes
come first and the smaller ones follow; on the Release notes page the lesser
items fold under "Other changes". The Downloads page shows only the highlights
of the most recent releases. The desktop app reads this same file
(Help → View changelog).

## 0.7.0 — 2026-07-15
- Analyze now brings OCR and text analysis into one workspace: a resizable facsimile, book artifacts, page-level analysis staging, shared OCR/text jobs, and credential-aware engine defaults.
- Book Capture and desktop cloud sync use signed-in user sessions instead of asking users for a Supabase service-role key; phone scans, profile OCR credentials, and captured metadata flow back into the desktop catalogue.
- Publish adds a catalogue tree and an online-record preview, grouped by book set, author, category, or date.
<!--more-->
- Generated About articles populate the Editor description automatically, while OCR, full text, translations, images, PDFs, and analysis output remain distinct artifacts.
- Android adds scan management and reprocessing controls; desktop OCR adds packaged Tesseract support, clearer diagnostics, safer staged-job handling, and improved multi-volume editing.
- Copyright lookup includes post-1978 CPRS records with year-aware matching, and the public library adds author pages, search suggestions, and cover thumbnails.
- Desktop startup, updater behavior, persistent view settings, and PDF-preview concurrency are more reliable.

## 0.6.0 — 2026-07-11
- PDF reader: long books now expose every page, with faster near-viewport loading, background page caching, thumbnail placeholders, keyboard navigation, and a page-jump control.
- Settings and menus follow desktop conventions; preferences are organized by task, with new editing, AI, OCR, update, and logging controls.
- Credentials now stay in a local-only secrets store, while device-specific layout state no longer syncs between machines.
<!--more-->
- Release notes are grouped by major version, with lesser changes folded away; the Downloads page has clearer platform rows and recent highlights.
- Local API requests now reject untrusted Host headers globally, closing the remaining DNS-rebinding path to client state, local PDFs, and the in-app fetch proxy.

## 0.5.0 — 2026-07-11
- Databases: drop Open Library or copyright files into a `~/.library-tool` folder in the home directory — used offline, with no URL and no download. A source URL is only needed to *fetch* a database not already present.
- Book Capture (Android 0.3.0): opens on a Home page of recent scans — page thumbnails, extracted title / author / year, and upload status — instead of dropping straight into the camera. "New scan" leads into capture.
<!--more-->
- UI scale: fixed a stray page scrollbar when the interface is zoomed.

## 0.4.0 — 2026-07-11
- Version numbering reset to a pre-1.0 line — the project is early and still changing fast, so it continues as 0.4.0 (Book Capture 0.2.0) rather than presenting as a finished 3.x product. Nothing about the app changed here; the earlier 3.x / 2.x history is kept below as-is.

## 3.2.1 — 2026-07-11
- UI scale setting under Settings → Appearance; Ctrl +/− to zoom, Ctrl 0 to reset.

## 3.2.0 — 2026-07-11
- Analyze tab: AI summaries, margin annotations, and page-aligned translations for a verified volume (requires a DeepSeek or Mistral API key).
- Hierarchical category tree for the collection; the online Library reads as an archival catalogue with an in-page reader.
- Book Capture 2.0 catalogues on the phone — OCR and extraction run there, and the desktop reuses that work, crediting whoever photographed the book.

## 3.1.2 — 2026-07-10
- Updates install silently in the background and relaunch on their own — no installer window, no clicks. (Applies to updates from this version onward.)
- Windows installer is code-signed.
<!--more-->
- Update progress window: removed the empty space above the title.

## 3.1.1 — 2026-07-10
- Theme set refreshed to a focused lineup of six, with a warmer Sage.

## 3.1.0 — 2026-07-10
- Automatic updates: checks on launch and installs a new version before opening.
- In-app changelog under Help → View changelog.
<!--more-->
- Website: a Downloads page with the latest notes and a Release notes page listing every version.

## 3.0.1 — 2026-07-10
- Minor interface fixes.

## 3.0.0 — 2026-07-10
- Windows desktop app: one installer, no Python setup.
- Multiple PDF scans per book, plus an OCR facsimile that lays text over the page image.
- Cloud sign-in: contributions carry the contributor's name in the shared activity feed.
<!--more-->
- First-run setup for OCR, cloud, and the offline search index.
