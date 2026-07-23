# Library Tool Capture (Android)

Library Tool Capture `0.5.1-alpha.10` (version code 29) is the Android
companion for Library Tool. It photographs books, runs OCR and catalog
extraction in the background, and sends captures either through the cloud or
directly to a paired desktop on the local network.

The app is still a prerelease. **Check for updates** currently refreshes the
validated remote catalog of Android strings and in-app icons; it does not offer
or install an uncertified APK.

## Home and book details

The app opens on **Home**, which has **Scans**, **Collections**, and **Inspect**
tabs.

The Scans tab groups books into collapsible collection sections. The current
collection is listed first and expanded initially. Waiting work uses an
animated indicator, delivered work uses an icon, and the colored state marker
carries the successful/complete state.

Scan-row gestures are literal:

- Tap a row to open its book details.
- Long-press a row to mark or clear its needs-attention note.

Uploads are opt-in: **Sync captures** creates an explicit batch for ready
captures and review-only changes. Background work resumes an already-authorized
batch after interruption, but never silently authorizes a new upload batch.

Book details present title, author, and year as primary catalog fields;
publisher, language, edition, and subtitle as secondary fields; and remaining
metadata in a compact table. The title page is the large hero image, cover and
spine roles are inferred when the evidence supports them, a cover can supply
the list thumbnail, OCR text is collapsed initially, and JSON/Mistral
diagnostics live in a separate collapsible panel. OCR polygons can be drawn on
the corrected display image. Press and hold any photo with a retained camera
original to compare it directly; releasing restores the corrected revision and
its revision-bound overlays.

## Capture view

A collection must be current before a scan can start. The camera preview has a
faint page-margin frame that remains at least two physical pixels wide, with
the area outside the intended title-page margin slightly darkened. The fixed
right-side controls open voice notes and camera/scan settings and switch
portrait/landscape framing; only the orientation glyph changes.

The camera popup contains practical capture controls only: tap/manual focus and
focus lock, zoom, exposure compensation, continuous light, low/fast/detail
resolution profiles, and preview sharpening where the Android version supports
it. Display-overlay and post-processing choices remain in the main Settings
screen.

The old recent-book selector is gone. The space between the camera/captured-page
preview and the bottom controls contains one **last captured book** card:

- It shows the newest sealed capture submitted for processing, not the open
  capture currently being photographed.
- It remains visible while the next book is open and changes only after that
  next capture is sealed.
- With no submitted capture, it shows the app-mark dummy thumbnail and an empty
  state.
- Its title, author, and year area opens book details. A bracketed-list action
  appears only when additional fields exist and opens a popup containing only
  those extra fields.

## Collections and provenance

A collection represents the catalog batch into which a book was scanned. It
has two deliberately separate location concepts:

- `parentId` is the durable collection-to-collection hierarchy edge. It builds
  paths such as `Office > Periodicals` and is synchronized as `parent_id`.
- `from` is physical provenance: where that batch came from, such as `Storage`
  or `Christopher Office`. It never identifies a parent collection.

A root collection may display its physical origin as a prefix, but nested
identity is always resolved by `parentId`. Missing/deleted parents and cycles
stop safely rather than inventing hierarchy from matching names. The collection
editor excludes the collection itself and its descendants from the parent
choices.

`CaptureSession.start()` requires a `BookCollection`, so both the Home button
and spoken **start** command are gated. The collection UUID, name, and `from`
value are frozen before the first photo in
`filesDir/queue/<entryId>/collection.json`. A later hierarchy or collection
rename can improve the live group label through the UUID without changing the
book's stored provenance snapshot.

Collections remain available offline in `filesDir/collections.json`. When
signed in, a background worker reconciles their edits, hierarchy, tombstones,
and merges with the shared cloud rows. Each capture sends
`scan_collection_id`, `scan_collection`, and `scan_from`.

Every collection also has a short, unique **tag ID** separate from its durable
UUID. New collections derive an editable label from their name (for example,
`Fungi` becomes `FUNGI_1`). This is the human-facing value printed on a box and
encoded as its QR label; renaming a collection does not silently change a tag
that may already be printed.

The **Inspect** tab gives a compact collection overview and opens a box directly
from its QR label. A selected box can be browsed as Windows-like **Tiles**,
**Content**, or **Icons**. The display choice is kept on the device and scanning
a box does not change the collection used by the next capture. Inspect retains
only a small bibliographic summary when old delivered scan media is cleared, so
the list remains useful without defeating the app's storage limit; cleared
photos and their local detail view are not retained.

## Voice commands and notes

General hands-free commands use the optional offline Vosk recognizer. Voice
notes use Mistral realtime speech-to-text and therefore require microphone
permission, a Mistral API key, and network access. Vosk pauses while Mistral
owns the microphone.

| Say | Effect |
| --- | --- |
| **start** | Begin a capture in the current collection. |
| **photo** | Photograph the page shown in the preview. |
| **done** | Seal the capture and submit it for background processing/upload. |
| **cancel** | Discard the open capture immediately; no confirmation dialog. |
| **restart** | Discard the open capture and start a fresh one in the same/current collection. |
| **undo** | Discard the most recent committed photo or saved/in-progress note. |
| **notes** | Start a Mistral voice note for the open capture. |
| **end notes** | Finish and save the active note. |

The floating note button provides the same start/finish action. While a note is
active, a compact translucent overlay shows the evolving transcript. The words
**Price**, **Pages**, **Condition**, **Illustrations**, and **Remark** become
colored field rows; later transcript updates may retroactively classify text
that first appeared unstructured. Note checkpoints survive lifecycle changes,
and in-flight photo/note mutations defer destructive commands until they can be
applied safely.

## Cloud image derivatives

After OCR/extraction assigns title-page, cover, and spine roles, Android freezes
a versioned post-processing request for those photos. Settings provide
automatic-by-date, modern (1950+), older (1850-1949), and early (before 1850)
presets plus feature controls for page/perspective dewarping, detected-margin
cropping, contrast normalization, and spine cropping.

For cloud captures, the active derivative flow is:

1. Android uploads verified immutable camera originals and the capture row.
2. Supabase migration 015 creates owner-scoped `photo_processing_jobs` and
   holds desktop import while jobs are processing.
3. The Cloud Run image worker verifies the original hash, corrects perspective
   and supported page curvature, crops detected margins/spines, and produces
   display, OCR, thumbnail, and transform artifacts in the private
   `capture-derivatives` bucket.
4. Android polls the jobs, validates ownership, request/revision lineage,
   hashes, dimensions, MIME type, byte count, and complete JPEG structure, then
   atomically installs the corrected display revision.

The camera original is never replaced. Pending derivatives use softened
thumbnails, press-and-hold comparison reads the retained original, and OCR
geometry is transformed only when the worker returns a mapping that is valid
for the exact source/display revisions. See
[`services/image_processor/README.md`](../../services/image_processor/README.md)
for deployment and worker details.

## Transport

Settings chooses how sealed entries leave the phone:

- **Cloud** uploads through Supabase with the signed-in account.
- **LAN** sends directly to a paired desktop using its address and token and
  works without internet or an account.
- **Auto** prefers the paired desktop when reachable and otherwise uses cloud.

LAN import reuses phone OCR/fields when available; otherwise the desktop runs
its normal ingest processing. A successful LAN response is the terminal
`imported` state and bypasses the Supabase derivative queue.

## Data path

```text
photo
  -> filesDir/queue/<entryId>/photo_N.jpg
  -> collection.json provenance + versioned photo-assets contract
  -> background Mistral OCR (*.jpg.txt) + extraction (meta.json)
  -> role/date-specific derivative request
  -> Cloud: immutable originals in captures + captures row
       -> photo_processing_jobs -> capture-derivatives
       -> verified corrected display revision installed on Android
       -> desktop import
  -> LAN: direct authenticated POST -> desktop import
  -> local history in filesDir/sent/<entryId>
```

The strict cloud `captures.photos` array remains the original-photo transport
contract. Corrected display artifacts never masquerade as camera originals.

## Build

1. Open `android/BookCapture` in Android Studio Koala Feature Drop (2024.1.2)
   or newer and let it sync. The project uses AGP 8.6, Kotlin 1.9.24, JDK 17,
   and the checked-in Gradle 8.12 wrapper.
2. Fork maintainers export `WHL_SUPABASE_URL` and `WHL_SUPABASE_ANON_KEY`
   before building. Official release CI injects the project configuration; app
   users never enter a Supabase key.
3. Run on Android 8.0+ (`minSdk 26`) and grant camera/microphone access as
   needed.
4. Sign in for cloud sync and save Mistral/DeepSeek credentials in Settings.
   Account profile secrets synchronize with the desktop; local-mode values stay
   on the device until sign-in.

## Running on an emulator

`tools/emulator.ps1` starts a headless AVD and waits for bounded boot
completion:

```powershell
powershell -File tools/emulator.ps1 -Action start
powershell -File tools/emulator.ps1 -Action status
powershell -File tools/emulator.ps1 -Action stop
./gradlew :app:assembleDebug
adb install -r -t app/build/outputs/apk/debug/app-debug.apk
```

Create the AVD once (with Android SDK command-line tools installed):

```powershell
sdkmanager "system-images;android-34;default;x86_64"
avdmanager create avd -n whl_test -k "system-images;android-34;default;x86_64" -d pixel_6
```

The script launches the emulator detached, kills it after a bounded boot
timeout, and supplies an explicit DNS server to avoid VPN-adapter boot loops.

## Launcher icon

The launcher icon is generated from the 1024 px `icon.png` master. From the
repository root, run:

```powershell
python tools/make_android_icon.py
python tools/make_android_icon.py --check
```

The generator writes the five `ic_launcher_fg.png` density buckets. The
adaptive-icon foreground uses the botanical-green background and a 13.5 dp safe
inset so circular launcher masks do not clip the square mark; the resource
contract tests enforce that geometry.
