# Library Tool Capture 0.5.1-alpha.8

This Android prerelease completes the capture, collection, photo-review, and
cloud-cleanup work introduced during the 0.5.1 refactor.

## Highlights

- The renamed Library Tool Capture interface now uses icon tabs, a full-height
  botanical-green app mark, regular-case labels, direct scan selection, and a
  separated app menu with Sign out and About.
- The camera has a compact camera-and-scan popup for focus lock, zoom, exposure,
  light, resolution/profile, sharpening, and page-cleanup presets, plus fixed
  portrait/landscape controls and a two-pixel page-margin guide.
- Realtime Mistral voice notes recognize Price, Pages, Condition,
  Illustrations, and Remark, with notes, end notes, restart, and undo commands.
- Collections are shown as collapsible scan groups with a highlighted current
  collection, nested location paths, compact layout, bordered thumbnails, and
  icon-only edit controls.
- Book details now provide catalog-oriented primary and secondary fields, a
  compact Other Details table, volume and spine metadata, a large title-page
  image, photo carousel, OCR boxes, retained-original comparison, collapsed OCR,
  and JSON/Mistral diagnostics.
- Android now follows cloud image-processing jobs through completion, verifies
  request and artifact lineage, checks the downloaded JPEG bytes, and installs
  corrected display revisions atomically while retaining camera originals.
- Nonlinear perspective and page-curvature corrections now regenerate OCR
  region geometry against the corrected display image, with durable retry
  markers retained until the aligned regions are stored. Original OCR text and
  catalog metadata remain unchanged.
- Check for updates refreshes a bounded, hashed remote catalog of Android
  strings and in-app PNG icons without offering an uncertified APK update.

## Fixes and polish

- Removed the detail Discard action and capture-source panel, joined the main
  detail panels with a dotted divider, and removed explanatory UI copy.
- Removed the empty camera-preview band and the old book-selector control; the
  last submitted book now remains as an image-and-metadata preview with a dummy
  state and an extra-fields popup.
- Added a lower camera-resolution option and kept camera buttons fixed while
  their orientation glyphs rotate.
- Pending and uploaded scans use compact visual indicators; adjacent list rows
  draw one separator, and thumbnails have a one-pixel dark frame.
- Cancelling a capture is immediate, and restart/undo remain safe while photo or
  transcription work is still committing.

This is a testing build. Keep the retained camera originals until cloud cleanup
has completed, and report camera, transcription, sync, or photo-display issues
with the device model and Android version when possible.
