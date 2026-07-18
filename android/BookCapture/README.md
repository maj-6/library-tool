# Book Capture (Android)

Hands-free companion app for the Library Tool: photograph title/copyright
pages of old books, OCR and extract the bibliography in the background, and
upload everything to the cloud (or straight to a paired desktop over the
LAN), where the desktop Library Tool files each capture as an entry with
its photos attached.

## Screens

The app opens on **Home** — the recent-scans list (page thumbnail,
extracted title / author / year or "Processing…" until the OCR/extraction
pipeline catches up, and status: pending upload / uploaded / imported),
with multi-select delete. Home owns the sign-in gate; **New scan** opens
the capture screen. Tapping a scan opens its **detail**: all photos, the
OCR text, every extracted field, and re-running the extraction with
per-book custom instructions.

## Voice flow

| Say        | Effect                                            |
|------------|---------------------------------------------------|
| **start**  | begin a book entry                                |
| **photo**  | photograph the page shown in the preview          |
| **done**   | seal the entry and queue it for upload            |
| **cancel** | void the entry (photos discarded)                 |

Every registered command is confirmed with a short distinct tone.
Recognition is offline (Vosk, restricted to the four command words, firing
on partial results so a command lands in well under a second) — the small
English model (~40 MB) downloads automatically the first time the capture
screen is opened. The on-screen icon buttons (new-entry camera, camera,
check, cross) mirror the voice commands; the top bar shows the open entry's
photo count and a dropdown of recent scans.

## Build

1. Open `android/BookCapture` in Android Studio (Koala 2024.1.1 or newer)
   and let it sync (AGP 8.5 / Kotlin 1.9; Android Studio supplies Gradle).
2. Fork maintainers export `WHL_SUPABASE_URL` / `WHL_SUPABASE_ANON_KEY` before
   building to bake their public project configuration in (release CI already
   does this for official builds). App users never enter a Supabase key.
3. Run on a device with Android 8.0+ (minSdk 26). Grant camera + microphone.
4. Sign in with your Library Tool account (see
   `docs/cloud_capture_setup.md`); set the Mistral / DeepSeek API keys once —
   they are stored in your cloud profile and shared with the desktop.

## Transport

Settings picks how sealed entries leave the phone: **Cloud** (Supabase,
the default), **LAN** (a paired desktop on the local network — host +
token, with a connection test), or **Auto** (LAN when the desktop answers,
else cloud). Over the LAN the entry POSTs straight to the desktop, which
imports it synchronously — reusing the phone's OCR and fields when they
arrived with the POST, else doing its own OCR on ingest — no cloud upload
and no signed-in account needed for that leg.

## Data path

photo → `filesDir/queue/<entryId>/photo_N.jpg`
  → (background) standardized in place, OCR → `photo_N.jpg.txt`,
    fields → `meta.json`
  → (upload, as the signed-in user) Supabase storage
    `captures/<device>/<entryId>/photo_N.jpg` + a `captures` table row
    (`status=pending`, with `created_by`/`contributor`/`ocr`/`meta`)
  → folder moves to `filesDir/sent/<entryId>` (the recent list's history;
    pruned to the last 15) → desktop Library Tool imports it and marks the
    row `imported`.

Over LAN the upload step instead POSTs the entry to the paired desktop —
a 200 response IS "imported", so there is nothing to poll afterwards.
