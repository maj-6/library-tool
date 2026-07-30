# Library Tool Capture 0.5.2-alpha.4

Prerelease. `versionCode` 35.

## Check a book while capturing it

Say **check** while a capture is open. The phone takes a photo, runs OCR,
extracts the title, author, and year, and searches both the CH master list and
the World Herb Library catalogue. The result appears in the capture screen
without waiting for **done**.

CH results distinguish a possible match from a completed no-match search. WHL
results distinguish a published match, a draft-only match, and no match.

## WHL search is bundled and offline

The APK now includes `assets/whl_index.json`, generated from the tracked
`whl_catalog.csv` snapshot. It contains 5,533 searchable records and the same
candidate postings, thresholds, normalization, and published-before-draft
selection rules as the desktop matcher.

OCR and bibliography extraction still require the configured provider and a
network connection. Once the bibliography is identified, neither the CH nor
WHL catalogue search sends a query over the network.

## Capture reliability

The app persists a check request before submitting the shot to CameraX and
binds it to the committed photo's stable asset identity. Queue compaction,
capture failure, retries, activity recreation, process death, and a newer check
cannot attach an older result to the wrong image or leave the request pending
forever.

An open check uses a read-only CH lookup and does not overwrite the captured
book's entry-wide CH candidate or an operator's existing decision.

## Validation

- 499 Android unit tests passed.
- Android debug assembly and `lintDebug` passed.
- The assembled APK contains both `assets/ch_index.json` and
  `assets/whl_index.json`.
- 205 sampled WHL searches matched the desktop implementation with no flag
  differences.
