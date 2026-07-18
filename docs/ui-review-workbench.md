# Workbench tab — layout critique and ergonomics review

2026-07-17. A four-lens review (information architecture, task-flow
ergonomics, toolbars/density, discoverability/safety) of the Workbench
tab as of the theme-refresh branch, after the activity-bar and
icon-conversion work landed. 35 raw findings deduplicated to 24.
Line anchors reference `tools/whl_explorer/` (index.html = templates/,
app.js + style.css = static/).

**Follow-up, 2026-07-18:** the duplicate readiness-chip navigator has been
retired. Readiness, badges, current phase, and the suggested next phase now
live on the existing phase rail. The redundant New-entry icon was also removed
from the fixed-width book-list bar; New entry remains in the activity bar and
the empty-state prompt. Together these changes remove both clipped button rows
without removing a distinct action. The same pass made book rows keyboard
operable, named the narrow-sidebar controls for assistive technology, and
updated setup and status copy that still routed people to the retired Editor
and Analyze tabs.

## Verdict

The Workbench's core geometry is right: books on the left, work in the
center, reference on the right, and a phase rail that matches the real
pipeline (Record → Source → Text → Knowledge → Publish). What fails is
**scope legibility** — controls that act on targets the screen never
names (the jobs drawer's stage-all, a delete-pages trash that stays
live in views where it cannot act), state that persists through a
*different* phase's machinery (Rights and the verify toggle ride the
Record form's save), three tab tiers with three different semantics on
one screen, phase-invariant chrome taxing phases it doesn't serve (the
Artifacts tree in Record/Source/Publish), and an expert-grade hidden
keyboard layer with almost no on-surface hints. The single worst
ergonomic defect is structural: the pipeline's one mandatory gate —
verify — lives at the END of the rail while blocking the MIDDLE, so
every book pays a Publish detour to unlock Text.

## Top issues (ranked by impact per effort)

### 1. Publish reads stale form values — S, high — a real bug
`#b-rights` only sets the dirty flag (app.js:8692-8694); `uploadBuild`
reads the *saved* rights with no dirty check (app.js:11792-11817).
Pick Rights, click publish → either a false "Set Rights before
publishing" block, or a silent publish under the old value.
**Fix:** at the top of `uploadBuild`, if `buildIsDirty()` await
`saveBuildFields()` and bail on failure — the same "publishing
decisions are save actions" rationale the verify toggle already uses.

### 2. The verify detour breaks the forward pipeline twice per book — M, high
Text and Knowledge lock for drafts (`applyWorkbenchGates`,
app.js:8547-8556) but the only unlock — `#b-ready` — is in Publish
(index.html:942-943). Real path per book: Record → Source → *jump to
Publish, toggle, jump back* → Text.
**Fix:** extract the toggle's click body (app.js:17955-17970) into
`setVerified(b)` and render a "Mark verified" button inside the
`#wb-text-locked` / `#wb-knowledge-locked` notes. Same action, same
semantics, where the user hits the wall. The Publish toggle stays.

### 3. Delete-pages trash: mid-bar, live in views where it can't act — S, high
The danger trash (index.html:894-895) sits between the view toggles
and the page-jump box; `setOcrView` hides every other pdf-only control
but not this one (app.js:13232-13239), and with no selection it
silently no-ops (app.js:14780-14783).
**Fix:** move it to the end of the pdf-only `#ocr-pagenav` span — it
then auto-hides with the page controls via the existing hidden toggle
— and give the empty-selection case an `#ocr-msg` hint ("Select pages
first — click / Ctrl+click").

### 4. Staging says "press Submit" while Submit is hidden — S, high
After digit-key staging, the message says "press Submit"
(app.js:14384-14397) but `#ocr-submit` lives in the collapsed jobs
drawer (index.html:1008-1019).
**Fix:** on the 0→N staged transition, `setJobsDrawer(true, false)` so
the queue and its Submit appear the moment there is something to
submit.

### 5. The hidden keyboard layer has no on-surface hints — S, high (×2)
The page-view legend documents 2 of ~8 gestures
(`buildOcrKeymapLegend`, app.js:13210-13224) — click-select,
Ctrl+click range, selection-first digit staging, Del/Esc are all
invisible. Smart check's TAB/SPACE bake mode is invisible at the point
of use, and TAB silently stops moving focus while checks are pending
(app.js:16993-16995).
**Fix:** extend the existing legend string (it already shows only in
pdf view); render the same one-line muted strip over the form while
smart-check results are held ("TAB extracted/original · SPACE over a
row bakes · wand dismisses"), toggled from `scRerender`.

### 6. Bars wrap mid-cluster at narrow widths — M, high
`#ocr-mainpane .pane-bar` wraps its ~14 children individually
(style.css:3406); wrap points fall between a label and its control.
**Fix:** group the bar into four inline-flex cluster spans (view
segment + pdf sub-toggles / pagenav + trash / find + replace /
quality + star + save) so wraps only occur at cluster boundaries.
While there: swap the horizontal bars' `.tb-sep` (a vertical-rail
primitive that renders as a floating dash, style.css:1273) for the
existing `.act-sep`.

### 7. The Artifacts tree squats in the left column for 3 of 5 phases — M, high
`#ocr-side` permanently stacks books over Artifacts
(index.html:614-628); the tree only serves Text/Knowledge. In Record
the column space is exactly what the Verified-sources workflow is
starved of.
**Fix:** in `setWorkbenchPhase` (app.js:8508-8530) auto-collapse the
Artifacts pane to its bar (click-to-expand, override persisted) for
record/source/publish.

### 8. Publish-phase controls persist through Record's form — M, high
Rights and verify render in Publish but save via the Record form
(app.js:8636-8637, 12046); toggling verify off + a later Save silently
demotes the entry, and the toggle's tooltip never says it saves the
whole record (app.js:17955-17962).
**Fix (incremental):** honest reporting first — tooltip "also saves
the whole record", explicit status lines on verify/unverify, and a
build-msg note when a save transitions ready→draft. The stale-rights
guard from issue 1 removes the worst trap.

### 9. Page deletion has a .bak but no in-app undo — M, high — DONE
Delete/Backspace both trigger it (app.js:14704-14709); recovery meant
finding the `.bak.pdf` by hand, while `deleteBuild` already showed the
house pattern (no-confirm + `pushOp` undo, app.js:12068-12094).

**Shipped instead of the proposed .bak swap:** a trash store. The
removed pages and a whole copy of every collateral file the renumbering
rewrites go to `output/trash/<id>/`, listed and restorable from
**Info › Trash** for 30 days. Restore is a verbatim write-back rather
than an inverse renumber — inverting a lossy transform is the part that
would have been hard to get right — and it declines to overwrite
anything edited since the delete, reporting it instead. The `.bak.pdf`,
`.txt.bak` and `.page-delete-backup` siblings this item was written
about are retired: the trash holds the same pre-images with an expiry
policy and a UI. The confirm stays, per the note below.

### 10. OCR finishes silently — S, med
The poll loop surfaces only errors (app.js:14536-14548); the footer
jobs chip disappears exactly when the news matters.
**Fix:** in the success branch, `status("OCR COMPLETE :: … — review in
Text")` + a one-shot attention class on the Text readiness chip.

## Quick wins (S effort, med+ impact)

- **Ctrl+S in the Text phase** — the handler excludes the phase with
  the most typing (app.js:18062-18073); three lines to route it to
  `ocrSaveDoc`.
- **Stage-all scope** — render "on: {title} / {src}" in the jobs
  drawer and set the + tooltip dynamically (app.js:14608-14628);
  disable when no book is selected.
- **Readiness chips as navigation** — append the destination to each
  chip tooltip ("No PDF attached — open Source") and mark the first
  todo/warn chip as `next` with a thin underline; chips already jump
  on click (app.js:8684-8688).
- **New-entry path** — add a + icon-btn to the `#builds-tabs` bar and
  rewrite `#build-empty` as two working affordances (new blank entry /
  seed from the Verified source below); focus `#b-title` after create.
- **Diff select + Quality select placement** — hide `#ocr-diff-with`
  outside diff view and seat it beside the diff button; move Quality
  across the bar-spacer into the doc-disposition cluster
  (quality | star | save).
- **View modes as a segment** — wrap edit/diff/pdf in `.ctl-join` so
  the choice-of-three reads as one control; leave layout/furniture
  outside the join as pdf options.
- **Publish earns text + confirm** — `#build-upload` is the tab's one
  public, hard-to-reverse action: give it a "Publish" label and a
  `confirmDialog` listing title/rights/PDF (fits the confirm policy —
  this is not a quick single-item delete).
- **Stop vs clear glyphs** — the jobs bar uses `remove` for both
  un-stage and stop (index.html:1015-1022); give stop a square glyph.
- **Analyze → Knowledge naming** — the Record-phase "Analyze" button
  jumps to the phase labelled Knowledge (app.js:9707-9717); rename or
  delete it.
- **Smart check surfacing** — add the wand to `.build-actions` beside
  Save/Delete; it acts on the whole form but hides on one field's
  label (index.html:723-725).

## Structural moves (M/L effort — sequence later)

- Splitter affordances: one shared `::after` grip rule (three quiet
  dots) + tooltip across all six gutters; port `#upload-splitter` to
  `initSplitter` for dblclick-reset (app.js:17762 vs 17800s).
- Retire the legacy hidden `#analyze` section (index.html:1024-1152)
  and let Knowledge markup live in Knowledge.
- Collapse the Verified-sources inbox to its bar while an entry is
  open in Record; expand it on the empty state.
- Single-source the default-engine setting (jobs drawer modal vs
  Settings, index.html:1325-1346 vs 1687-1688).
- Merge the phase rail and readiness chips into one phase authority —
  the chips already encode state and jump; the rail duplicates them
  without the state.
- Restyle the Pending/Uploaded filter out of the pane-tab vocabulary —
  reserve pane-tabs for content views.

## What was NOT flagged

The three-pane split geometry, the phase-rail order, VS-density row
heights, the jobs drawer's existence (only its scope opacity), and the
keyboard-first staging design — all sound. The critique is about
making the existing design legible, not redesigning it.
