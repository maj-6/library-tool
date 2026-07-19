# Book Capture (Android)

Hands-free companion app for the Library Tool: photograph title/copyright
pages of old books, OCR and extract the bibliography in the background, and
upload everything to the cloud (or straight to a paired desktop over the
LAN), where the desktop Library Tool files each capture as an entry with
its photos attached.

## Screens

The app opens on **Home**, which has two tabs.

**Scans** is the recent-scans list (page thumbnail, extracted title /
author / year or "Processing…" until the OCR/extraction pipeline catches
up, where the book came from, and status: pending upload / uploaded /
imported), with multi-select delete. Home owns the sign-in gate; **New
scan** opens the capture screen. Tapping a scan opens its **detail**: all
photos, the OCR text, every extracted field, and re-running the extraction
with per-book custom instructions.

**Collections** is where books get their provenance. A collection is the
batch a book was scanned into — a shelf, a crate, a room — and it carries a
**From**: where that batch physically came from ("Storage", "Christopher
Office"). Add, rename, re-origin and delete collections here; tapping one
makes it the collection the next book lands in.

## Collections and provenance

**A collection must be chosen before a book scan can start.** Both routes
in are gated — the Home button and the spoken word "start" — and
`CaptureSession.start()` takes the collection as a parameter, so the
requirement is enforced by the type rather than by remembering to ask. A
single existing collection selects itself; with several, the choice is
explicit.

Provenance is frozen per book at `start()`, before the first photo, into
`filesDir/queue/<entryId>/collection.json`. Re-selecting a different
collection halfway down a shelf therefore never relabels books already
captured, and a crash mid-book is recovered with the provenance it was
started under. A single book's **From** can be overridden from its detail
screen up until it uploads; after that the cloud row is insert-only, so the
field is locked rather than allowed to disagree with what the desktop
already holds.

Collections always live on the phone (`filesDir/collections.json`), so adding,
editing, deleting and scanning remain available while signed out or offline.
When signed in, a background job reconciles them two-way with the shared cloud
`collections` rows. Local edits and soft-delete tombstones sync on reconnect;
the first sign-in pushes collections created in local mode without changing
their UUIDs.

Each capture sends `scan_collection_id` for durable identity alongside the
frozen `scan_collection` name and `scan_from` snapshot. Renaming a shared
collection therefore updates its current row without relabelling books already
scanned into it. The `scan_` prefix also keeps these passthrough fields out of
the desktop's fallback-OCR metadata test — see `tests/test_phone_capture.py`.

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

## Running it on an emulator

`tools/emulator.ps1` starts a headless AVD, installs nothing itself, and exits
either when the guest reports `sys.boot_completed` or when it gives up:

```
powershell -File tools/emulator.ps1 -Action start     # boots, waits, exits
powershell -File tools/emulator.ps1 -Action status
powershell -File tools/emulator.ps1 -Action stop
./gradlew :app:assembleDebug
adb install -r -t app/build/outputs/apk/debug/app-debug.apk
```

Create the AVD once (needs `cmdline-tools` in the SDK):

```
sdkmanager "system-images;android-34;default;x86_64"
avdmanager create avd -n whl_test -k "system-images;android-34;default;x86_64" -d pixel_6
```

Three things about that script are deliberate, each from a failure:

- **The emulator is launched detached.** It is a server and never exits, so
  running it as a tracked child of a build/tool runner leaves that runner
  waiting forever and holding file handles.
- **The boot wait is bounded** and kills the emulator on timeout. A hung
  emulator is otherwise indistinguishable from a slow one.
- **It passes an explicit `-dns-server`.** With a VPN adapter up (NordLynx and
  friends) the emulator otherwise re-enumerates the tunnel's addresses forever —
  the log fills with `Ignore IPv6 address` and the guest never boots at all.

The launcher icon is generated, not hand-placed: `icon.png` here is the
1024 px master, and `python tools/make_android_icon.py` (from the repo root)
rewrites the five `ic_launcher_fg.png` density buckets from it.
`--check` verifies the committed bitmaps still match. It exists because
adaptive-icon framing is easy to get wrong — the launcher's mask is
inscribed in the centre 72 dp of the 108 dp canvas and can be a circle, so
artwork has to fit that circle's *diameter*, not the square. The script
documents the arithmetic; the 14.5 dp inset it works back from is asserted
by `ResourceContractTest`.

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
  → provenance frozen at start → `collection.json` (its own sidecar: the
    ownership sidecar `capture.json` is rewritten wholesale when a legacy
    capture is repaired, which would erase provenance folded in beside it)
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
