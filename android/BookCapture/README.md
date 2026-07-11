# Book Capture (Android)

Hands-free companion app for the Library Tool: photograph title/copyright
pages of old books, OCR and extract the bibliography in the background, and
upload everything to the cloud, where the desktop Library Tool files each
capture as an entry with its photos attached.

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
English model (~40 MB) downloads automatically on first launch. The
on-screen glyph buttons (▶ ● ✓ ✕) mirror the voice commands; the top bar
shows the open entry's photo count and a dropdown of recent scans
("Processing…" until the OCR/extraction pipeline turns a folder of photos
into a title, author and year).

## Build

1. Open `android/BookCapture` in Android Studio (Hedgehog or newer) and let
   it sync (AGP 8.5 / Kotlin 1.9; Android Studio supplies Gradle).
2. Optionally export `WHL_SUPABASE_URL` / `WHL_SUPABASE_ANON_KEY` before
   building to bake the Supabase project in (CI does; the anon key is public
   by design). Without them, point the app at a project in ⚙.
3. Run on a device with Android 8.0+ (minSdk 26). Grant camera + microphone.
4. Sign in with your Library Tool account (see
   `docs/cloud_capture_setup.md`); set the Mistral / DeepSeek API keys once —
   they are stored in your cloud profile and shared with the desktop.

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
