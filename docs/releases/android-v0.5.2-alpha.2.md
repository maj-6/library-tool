# Library Tool Capture 0.5.2-alpha.2

Prerelease. `versionCode` 33.

## Catalogue indicators on book rows

Every book row now carries a compact three-slot indicator row — CH, WHL,
Internet Archive — in a fixed order, so position alone tells you which source
an icon is speaking for once you have seen the row twice. States are
distinguished by shape and a short text tag as well as colour, so the row still
reads for a red/green colour-vision deficiency.

The row lives in `res/layout/view_catalog_indicators.xml` as a `<merge>` and is
included by `item_home.xml`, so every book surface renders the identical thing
rather than three drifting copies.

The CH slot is interactive: tap approves a proposed match, long press rejects
it. It holds its 48 dp target even with nothing to say, so states resolving in
the background never reflow the row under your thumb.

## Approving a CH match merges its fields

Approving opens a preview of exactly what the merge did, field by field, with
CH-sourced values marked. Two rules decide it:

- **A blank never overwrites.** CH filling a field you do not have is the point;
  CH blanking one you captured is data loss.
- **A disagreement is never resolved silently.** Your value came off the title
  page in front of you; CH's came from a catalogue with known-dirty rows
  (impossible years, `_x000D_` artefacts). A conflict therefore keeps *your*
  value and shows CH's alongside it, marked as not applied.

The merge preserves every key it does not own, so the diagnostics tab still
renders the exact persisted extraction bytes.

## Known gaps in this build

- **CH matching does not run yet.** The generator
  (`tools/build_ch_index.py`), the on-device store, the indicator row, the
  gestures and the merge are all in place, but nothing yet loads the index on
  the phone and populates a candidate. In practice the CH slot stays hidden.
  The remaining work is the Kotlin matcher plus bundling `ch_index.json.gz`
  into the APK's assets; `build_ch_index.py` already emits
  `ch_match_fixtures.json` for the conformance test that will pin it to the
  desktop matcher.
- WHL and Internet Archive indicators still render from the desktop-supplied
  metadata that was already synced to the entry. Moving those checks to the
  cloud so they run as books are scanned is not in this build.
- This build is cut from `main`, which does **not** contain
  `android-v0.5.2-alpha.1`. That tag was cut from an unmerged branch, so the
  Android cloud-capture-sync fix it carried is not here. `versionCode` 33 keeps
  updates monotonic, but treat this as a parallel line rather than a successor
  until that branch lands.
