# Library Tool Capture 0.5.1-alpha.7

This Android prerelease focuses on faster collection work, clearer book review,
and hands-free capture notes.

## Highlights

- Camera voice notes now use realtime transcription and recognize Price, Pages,
  Condition, Illustrations, and Remark as compact structured fields.
- Voice commands can start or end notes, restart a capture, and undo the latest
  photo or note.
- The scan list is grouped into collapsible collections, highlights the current
  collection, and includes a compact layout option.
- Book details now separate primary, secondary, and other metadata, with OCR
  regions, corrected display photos, retained originals, and collapsible JSON
  and Mistral-response diagnostics.
- The capture screen keeps the most recent book preview beside the controls and
  exposes additional detected fields in a compact popup.

## Fixes and polish

- Cancelling a capture is immediate.
- Pending and uploaded states use compact visual indicators, and adjacent scan
  rows no longer draw doubled separators.
- Restart and undo target the newest capture correctly, even while a photo is
  still being committed.
- Voice-note drafts survive transcription drain and app backgrounding.

This is a testing build. Please report capture, transcription, sync, or display
regressions with the device model and Android version when possible.
