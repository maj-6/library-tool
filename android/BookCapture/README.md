# Book Capture (Android)

Hands-free companion app for the Library Tool: photograph title/copyright
pages of old books and upload them to the cloud, where the desktop Library
Tool picks them up, OCRs them (Mistral), extracts the bibliography, and files
each capture as an entry with its photos attached.

## Voice flow

| Say        | Effect                                            |
|------------|---------------------------------------------------|
| **start**  | begin a book entry                                |
| **photo**  | photograph the page shown in the preview          |
| **done**   | seal the entry and queue it for upload            |
| **cancel** | void the current entry (photos discarded)         |

Every registered command is confirmed with a tone + a spoken word
("started", "photo 2", "saved, 2 photos", "cancelled"). Recognition is
offline (Vosk, restricted to the four command words) — the small English
model (~40 MB) downloads automatically on first launch. The on-screen
buttons mirror the voice commands.

Captured entries are queued locally and uploaded by WorkManager whenever
the network allows, so capturing keeps working with no WiFi in the stacks.

## Build

1. Open `android/BookCapture` in Android Studio (Hedgehog or newer) and let
   it sync (AGP 8.5 / Kotlin 1.9; Android Studio supplies Gradle).
2. Run on a device with Android 8.0+ (minSdk 26). Grant camera + microphone.
3. In the app: ⚙ → paste the Supabase project URL and key (the same project
   the Library Tool's *Phone capture* settings point at, see
   `docs/cloud_capture_setup.md`), set a device name, **Test connection**.

## Data path

photo → `filesDir/queue/<entryId>/photo_N.jpg` → (upload) Supabase storage
`captures/<device>/<entryId>/photo_N.jpg` + a `captures` table row
(`status=pending`) → desktop Library Tool sync imports it as a manual entry
and marks the row `imported`.
