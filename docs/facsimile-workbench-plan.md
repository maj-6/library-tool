# Facsimile pipeline + Replica workbench — plan

Drafted 2026-07-15. Status: **Phase 0 in progress** (validation + prerequisite
fixes). Everything else is design, not contract — expect revision after the
Phase 0 findings land at the bottom of this file.

## The problem

Mistral OCR produces the cleanest transcriptions of the engines we run, but its
output is flat markdown: no word boxes, so no Layout facsimile, and no reliable
way to tell a margin note from the main text. On hand-press-era books
(marginalia, running heads, catchwords, signature marks) the compiled text is
"dirty" — furniture bleeds into the body, which then flows into translations
and `volume_pages`. Separately, we want to support producing **modernized
facsimiles**: translated or refreshed editions of old books (e.g. 1700s
Linnaeus) that follow the original's layout and typography with modern faces —
a largely human-assisted process that needs rich per-page data to work from.

## The discovery that shapes the plan

Mistral OCR 4 (released 2026-06-23; `mistral-ocr-latest` already resolves to
it, so we are already paying its rate, ~$4/1k pages, $2 batch) accepts
`include_blocks: true` and returns **typed, reading-ordered, paragraph-level
bounding boxes** — 13 types including `aside_text` (marginalia), `header`,
`footer`, `caption`, `title`, `signature` — plus page `dimensions`. We send
one page image per call and discard everything but the markdown and figure
crops. So margin-note separation is a first-party API field we throw away.

What Mistral still does *not* provide: word- or line-level geometry (word
*confidences* only). Word boxes remain Tesseract/Textract territory. The plan
therefore targets **region (block) grain** as the primary substrate — enough
for clean text separation, a paragraph-grain facsimile, and every re-typeset
goal — with word boxes as an optional finer layer where a local engine ran.

Two caveats that gate everything:

- OCR-4's block classifier is trained on modern documents. Whether
  `aside_text` fires on Latin marginalia in 1700s print is **unvalidated** —
  hence Phase 0.
- `bbox_annotation_format` applies to figure bboxes only; it is **not** a
  mechanism for classifying text regions. (Vision-LLM help on text regions
  means a crop sent to a chat model, not the annotations API.)

## Known bugs this plan depends on / collides with

1. **Box clobber** — `_ocr_job_run` passes `[]` to `_ocr_save_page_words` when
   an engine returns no geometry, which *deletes* the page's existing
   Tesseract boxes. "Engine has no geometry" (leave sidecar alone) and "page
   is empty" (delete) must be distinct. Prerequisite for any engine-mixing.
   **Fixed in Phase 0.**
2. **Malformed mime** — `capture_pipeline.py` built `"image\png"` (literal
   backslash) for PNG uploads to Mistral. Tolerated by the API, still wrong.
   **Fixed in Phase 0.**
3. **Translation staleness** — translation resume skips pages already present
   in `translations/<lang>.txt`, keyed by page number only. The moment region
   separation cleans `compiled.txt`, every existing translation of dirty text
   silently never refreshes. Needs a per-page source-text hash before body-only
   recompile ships (Phase 1).

## Phase 0 — validate (before designing further)

Add `include_blocks` support to `mistral_ocr_pages`, then probe ~20
representative pages from real 1700s scans (body pages with marginalia, an
index page, a plate, a title page): dump the raw responses and overlay the
typed boxes on the page rasters for human inspection.

| Outcome | Consequence |
| --- | --- |
| Blocks + types good | Plan runs as written |
| Boxes good, types unreliable | Keep geometry, reclassify locally (Phase 1 heuristics) |
| Both poor on old print | Local segmentation (projection profiles + Tesseract lines) provides geometry; Mistral supplies text only |

The workbench design is unchanged in all three cases; only the seeding quality
differs. Findings get appended to this doc.

## Phase 1 — region substrate (pipeline, no UI)

- **One canonical region store**: a `"regions"` key in `ocr/layout.json`
  beside `words`/`images` — per source, per page, boxes normalized 0..1, same
  merge lock, and the page-delete renumber (`_renumber_layout_words`) extended
  to cover it. Region: `{id, role, box, order, text: {diplomatic,
  normalized?, t?}, prov: {source, conf, verified}}`.
- **One role vocabulary**, PAGE-XML-aligned (`body`, `marginalia`, `footnote`,
  `header`, `footer`, `catch-word`, `signature-mark`, `page-number`,
  `drop-capital`, `caption`, `title`, `figure`, `table`, `ornament`), with a
  documented mapping from Mistral's block types (`aside_text` → `marginalia`,
  …). Open enum: unknown roles degrade to `text`, never fail.
- **Persist what's discarded**: blocks → regions; page `dimensions` → per-page
  record. Note Mistral's dpi describes *our raster* (default 1400 px wide),
  not the physical book — physical size comes from the PDF MediaBox or human
  entry, and only matters for print export.
- **Clean text at the source**: "recompile body text" writes only body-role
  regions, in reading order, to the compiled `.txt`. Furniture lives in its
  regions. Optionally marginalia can be offered into `annotations.json` as
  *suggested* notes (never auto-approved), lighting up the website margin
  panel.
- **Local geometric verifier/fallback** (numpy only — Kraken has no Windows
  support, Surya's weights license is restrictive, LayoutParser is dormant):
  book-level column-band model from line-edge histograms; margin text =
  outside the band + smaller glyphs; **catchword = last line, right-aligned
  single token that must equal the next page's first body word** (self-checking
  QA unique to hand-press books); signature mark = bottom-center
  `[A-Z]{1,2}\d?`; drop cap = >2.5× median line height.
- **Translation source hashes** (bug 3 above).

## Phase 2 — facsimile display for Mistral pages

Extend the existing renderer, don't fork it: word boxes → `fillWordLayout` as
today; regions only → each region rendered as an absolutely-positioned box
(same percent math) with its text flowed inside — a paragraph-grain facsimile
with margin notes visually in the margin. Role-based styling (small italic
marginalia, centered running heads) and a hide-furniture toggle. Per-page
fallback chain: word facsimile → region facsimile → whole-page flow.

## Phase 3 — Replica workbench (new top-level tab)

Region editing is modal and keyboard-heavy; Analyze is saturated. New tab via
the standard registration pattern (nav button + `section.panel-view` +
`TAB_TITLES` + `initTabs` hook), reusing the Books/Artifacts sidebar,
splitters, lazy-fill observer, and jobs queue. Core screen, CAD-dense: page
strip | raster + editable region overlay | region text panel.

- Machine proposes, human disposes: regions seed from blocks + figure boxes.
  Drag/resize, `S`plit (the fix for a fused margin-note-plus-body block),
  `M`erge, digit keys assign roles (mirrors the OCR engine keymap), Tab/`[`/`]`
  walk and reorder reading order. Thin 1px role-colored borders, tiny corner
  labels — house under-styling rules.
- Text panel: diplomatic ⇄ normalized toggle (long-s ſ and ligatures preserved
  in one layer, mechanical modernization *proposed* in the other),
  source-compare (Mistral vs Tesseract-clip vs PDF text layer), and **clip
  from words** — pull every stored word box inside the region (this is why the
  clobber fix is a prerequisite).
- Layout templates: recto/verso grids defined once, propagated across a page
  range; outlier pages flagged by low overlap so attention goes only where the
  grid broke (plates, chapter openings).
- Review states per page: `raw → segmented → verified → styled → translated`,
  chips on the page strip.

**MVP cut** (a useful alpha alone): the tab, seeded regions, role reassignment
+ split/merge, text panel with word-clip, recompile-body-text. That alone
kills the dirty-margin-note problem end to end.

## Phase 4 — `.lib` export and the modernized edition

`.lib` is a **sealed zip export serialized from the working store** — not a
replacement for the entry folder (R2 entry sync unchanged):

```
book.lib (zip)
  book.json        # bibliographic snapshot, role→style sheet, templates,
                   # front/back-matter outline, illustration inventory
  pages/N.json     # dims, regions with all text layers + provenance
  assets/img/…     # figure crops
  assets/fonts/…   # OFL-only (IM Fell, EB Garamond); never system fonts
```

Validated, sanitized import (theme-editor export pattern). The **style board**
maps roles → modern typefaces/sizes/leading with a live re-typeset preview:
real text flowed into the region grid at mapped styles beside the original
raster. A language selector on that preview (body regions take the
page-aligned translation; marginalia get short per-region translations via the
resumable job pattern) *is* the translated modernized facsimile. Print/PDF
export reuses the same renderer; Chromium's paged-media support is partial, so
print-quality pagination is its own later work item. Interchange exports
(TEI `<note place="margin">`/`<fw>`, PAGE XML) map 1:1 from the role
vocabulary when wanted.

## Deliberately deferred (gated escalations)

- **Segment-then-OCR**: re-OCR region crops individually — contamination
  becomes geometrically impossible, but 3–6× API cost; only for flagged pages.
- **Word-level Mistral↔Tesseract fusion** (sequence alignment): high-effort
  algorithmics no stated goal requires; region grain suffices.
- **On-demand vision-LLM region transcription** in the workbench (a Claude
  vision call per crop — not `bbox_annotation_format`).

## Known caveats (fine for 0.x alphas; burn down before stable)

- Two-page spreads and gutter skew: no deskew today; axis-aligned boxes suffer.
- Printed folio vs PDF-page numbering (roman front matter, unnumbered plates)
  needs a book-level mapping eventually.
- Text engraved inside plates becomes pixels in figure crops.
- Blocks require re-OCR of already-processed books (~$1–2 per 300-page book);
  do it per book as it's worked on, not as a bulk sweep.

## Sequencing

Phase 0 → 1 → 2 → 3-MVP ship as separate `v0.x-alpha` prereleases per
`docs/releasing.md` → Release standards. Phase 4 follows once MVP usage shows
how much machinery templates/style board actually need.

## Phase 0 findings

*(pending — appended when the validation run completes)*
