# Library Tool Capture 0.5.2-alpha.11

Android prerelease. `versionCode` 42.

This release lets a photo say what part of the book it shows, and stops the
cataloguer from filling in blanks it cannot actually read.

## Say what you are photographing

Alongside **photo**, three new spoken commands take the shot *and* label it:

- **spine**
- **cover**
- **title** (or **title page**)

Plain **photo** is unchanged and leaves the app's own guess in place.

Until now nothing could record a spine at all. The automatic guess decides
whether a photo is a spine by comparing the whole frame's proportions against a
3:1 ratio, and the camera only ever produces 4:3 — so of the 1,928 photos taken
so far, not one was labelled a spine, even though spines are roughly one shot in
ten. A spoken label is recorded as a manual choice and the automatic pass leaves
it alone.

## Extraction knows which page it is reading

Every photo's text is now handed to the cataloguer tagged with its role, and the
prompt ranks them: a title page outranks a cover, a spine is read only for the
spine title, and endpapers, dealer descriptions and price tags are treated as
untrusted. Previously all the text arrived as one undifferentiated block.

Two consequences of that block are fixed:

- **Bookseller and library marks are no longer read as publication dates.** A
  dealer's "6/52" pencilled inside a front cover had been taken as the year 1952
  for a book published decades earlier.
- **A book whose photos yield no readable text no longer gets invented
  metadata.** Pages OCR cannot read come back as image placeholders rather than
  as an empty result, which counted as text — one capture was catalogued as
  Gibbon's *Decline and Fall*, published by John Murray in 1854, on the strength
  of two unreadable photos. Extraction now reports that it could not read the
  capture instead of answering from memory.

## Updating

Install this APK over the existing Library Tool Capture app. Do not uninstall
the existing app or clear its data: Android preserves the local capture queue
only during an in-place, same-signing-key update.
