# Staged capture review — 2026-08-03

`staged_review.json` is a point-in-time review of every phone capture in the
cloud (555 rows, 2005 photos). Nothing in it has been applied. Apply it with:

```bash
python tools/apply_capture_review.py --review data/capture-review/staged_review.json
```

That is a dry run; add `--apply` to write, and `--flags` to also record the
review reasons in each entry's `attention` field. The script backs up
`manual_entries.json` before it writes, and refuses to move a field whose live
value has drifted from the one that was reviewed.

## How it was produced

Every capture was judged against **its own Mistral OCR text only** — no world
knowledge, no images (the originals of imported captures are deleted from the
`captures` bucket after import). Each proposed change was then re-checked by a
second, adversarial pass whose default was to reject. That pass rejected 7 of
134 proposals and revised 3; the rejections were mostly attempts to blank a year
that the title page did in fact carry, plus one expansion of "W. M. RAMSAY" to a
full forename that appears nowhere in the capture.

## What is in it

| | |
|---|---|
| captures reviewed | 555 |
| captures with accepted corrections | 96 (106 field changes) |
| flagged for manual review | 122 |
| marked not-a-book | 57 |
| photo role labels | 2005 |

Field changes: title 38, subtitle 29, author 28, year 22, publisher 10.

Photo roles assigned: content 790, title_page 458, cover 368, other 246,
**spine 143**. The live data contains **zero** spine labels, because
`PhotoAssets.kt` decides `spineLike` from the whole frame's aspect ratio against
a 3:1 threshold and every asset is 4:3 — so the branch cannot fire. 362 of the
2005 labels carry confidence ≤ 0.5 and are honest guesses from text alone; a
spine and a half-title are not reliably distinguishable without the image. Going
forward the spoken `spine` / `cover` / `title` commands record the role at the
shutter instead of inferring it.

## Scope limits

- Only **title, subtitle, author, publisher, year** were reviewed. `volume` and
  `edition` conflicts are flagged, not resolved.
- Of those, only **title, author and year reach the handset**:
  `_capture_bibliography` publishes a deliberately bounded three-field snapshot
  and Android's `DesktopBookMetadata` parses exactly those three. Corrections to
  `subtitle` and `publisher` stop at the desktop catalogue until that contract is
  widened on both sides — which the code notes is "a rights and payload decision,
  not a formatting one".
- 14 of the 96 corrected captures are not imported to the desktop yet, so they
  have no `manual_entries.json` row to carry the correction. Re-run the applier
  after importing them.
- The 2005 photo role labels are **not** applied by the applier. They belong in
  `captures.meta._capture_photo_assets[].role.manual_override`, which is a cloud
  write; publishing them needs a live session.
