# UI review — dialogs, popups & transient surfaces

_Reviewed 2026-07-16 against the sidecar app (`tools/whl_explorer`) at desktop
`0.8.0-alpha.4`. Scope: the modal/popup layer — the reusable dialog shell, the
~16 overlay windows, menus, tooltips, and the status/toast channel. Goal: does
it read as **polished, professional, no-nonsense** enterprise software?_

**Method.** Two passes, cross-checked: (1) the running app driven headless
against a throwaway `WHL_DATA_ROOT` on port 5199, screenshotting each dialog;
(2) a six-dimension static audit of `app.js` / `style.css` / `index.html`, every
issue adversarially re-verified against the source (2 candidate findings were
refuted, 2 downgraded as overstated — those are recorded below, not in the
issue list).

> **Status (2026-07-17):** the **quick wins** below — items 2, 4, 5, 6, 7, 8, 9,
> 10 — have been implemented (`aria-label`s, a WCAG-tuned `--ink-mut` token, the
> status-bar live region, toast-copy normalization, the popup Escape/resize, the
> tooltip z-index, and the auth backdrop dismiss), live-verified with zero
> console errors and the confirm-detail contrast raised from ~2.4:1 to 4.77:1.
> Still open: the **shared-overlay refactor** (items 1 & 3 — modal ARIA + focus
> trap), and an app-wide repoint of the other `--face-sh2`-as-text spots (the
> same contrast issue outside the dialog scope).

---

## Verdict

**Visually it already clears the bar; the gap to a full enterprise standard is
almost entirely under the hood — accessibility and focus management — plus two
small CSS bugs.** To a sighted mouse user the dialogs look finished and of a
piece: one shared chrome, compact CAD styling, concise copy, correct
destructive-action treatment. To a keyboard or screen-reader user, only **2 of
~16 modal surfaces** are actually modal; the rest leak focus and are anonymous
to assistive tech. None of this is deep — it is uniformity + a11y hygiene, and
most of it collapses into a single shared helper.

The tell: the app already contains a **reference-quality** implementation
(`confirmDialog` + the image lightbox). The work is making the other dozen
windows match what those two already do.

---

## Strengths (already right — keep these)

- **One shared shell.** ~15 windows reuse `.overlay > .win > .win-titlebar`
  with shared size tokens (`.win-sm/.win-md/.win-lg`) and a common titlebar
  grammar (title left, `×` close right). Chrome is consistent across sign-in,
  Settings, Categories, About, etc. (`style.css` ~1479+).
- **`confirmDialog()` is a proper modal** (`app.js:771`, `:807`). Destructive
  confirms paint the action red **and** move initial focus to the safe choice
  (`:788`); a capture-phase key handler traps Esc/Tab/Enter and blocks app-wide
  shortcuts behind the dialog (`:824`); focus returns to the original opener on
  close (`:797`), surviving dialog-replaces-dialog. `#confirm-window` also has
  full modal ARIA (`role="alertdialog"`, `aria-modal`, `aria-labelledby`,
  `aria-describedby`; `index.html:1947`). This is the model for the rest.
- **The image lightbox** likewise sets `role="dialog"`/`aria-modal`, focuses
  close on open, and pins Tab (`index.html:1794`, `app.js:14662+`).
- **Copy is concise and professional.** "Reset every interface setting? Catalog
  data is kept." / "Contributions are recorded under your account. Everything
  else works without one." No emoji, no exclanation-heavy tone.
- **Menus follow desktop convention** — grouped with separators, right-aligned
  shortcuts (`Ctrl+Z`, `Ctrl+,`), `✓` for toggles, `…` on items that open a
  dialog.
- **Good empty states and a real status bar** ("Nothing awaiting review", "No
  contributors recorded yet"; `> READY`, index/mode readouts bottom-right).
- Backdrop-click **and** Escape dismissal are wired for nearly every overlay.

---

## Issues, by priority

Each is verified against the source. `file:line` is the anchor.

### High

1. **Modal semantics missing on ~14 of 16 windows.** Only `#confirm-window`
   and `#img-lightbox` declare `role="dialog"`/`aria-modal`/`aria-labelledby`.
   Settings, sign-in, wizard, Categories, Changelog, About, the PDF/markdown/
   file-browser/manual-source/IA/webview/engine viewers are all bare
   `<div class="win">` — a screen reader announces them as anonymous groups with
   no name and no modal boundary. `index.html:1347` (+ the sibling overlays).
   _Fix: add `role="dialog" aria-modal="true" aria-labelledby="<title-id>"` to
   each `.win`, mirroring `confirm-window`._
2. **Icon-only buttons have no accessible name.** Console copy/clear
   (`index.html:1154/1156`), file-browser go (`:1242`), needs-attention
   save/clear (`:1261/1263`), IA/webview "open external" (`:1775/1972`), webview
   reload (`:1974`) expose only a `data-tip` mouse tooltip (`app.js:1978`),
   never mirrored to `aria-label`/`title`. To assistive tech they announce as
   "button" or a stray arrow glyph. _Fix: add `aria-label` to each; keep
   `data-tip` for the visual tooltip._

### Medium

3. **No focus trap / initial focus / focus restore on the real modals.**
   `openSettings()` (`app.js:3000`) and the auth dialog (`:1342`, `:1349`) just
   unhide the overlay: no initial focus, Tab walks straight into the app behind
   the dialog, and on close focus drops to `<body>` instead of the opener. The
   app root is never made `inert`, so even the two correct modals leave the
   background reachable by a screen reader's virtual cursor. _Fix: a shared
   `trapFocus(overlay)` — initial focus, Tab wrap, restore opener, toggle
   `inert` on the app root._
4. **Muted text reuses a border token as its colour → WCAG AA failure.**
   `#confirm-detail`, `.info-k`/`.info-empty`, `.ia-meta th`, `.dl-progtext`,
   `.foot-jobs` use `--face-sh2` (the "strong border" colour) as foreground:
   ~2.4:1 on the dialog face in the sage theme — below even the 3:1 large-text
   floor, on 10–13px text. (Visible on the confirm's "Catalog data is kept."
   line.) `style.css:1561`. _Fix: add a dedicated muted-ink token tuned to
   ~4.5:1 per theme._
5. **Undefined CSS tokens silently break the wizard's muted copy.**
   `--ink-soft` / `--ink-faint` are used in the wizard (`style.css:1676`,
   `:1679/1690/1698`) but **never defined** in the app stylesheet (they exist
   only in the website CSS). The whole `color` declaration is invalid, so the
   "(optional)" label, "Tesseract not detected" line and service-card copy
   render at full `--ink` weight instead of faded. _Fix: define both per theme,
   or swap to the existing `--ink-light`._
6. **Status bar is not a live region.** `#status-msg` (`index.html:1756`) is the
   single channel for success / error / "data did not persist" notices
   (`status/statusErr/statusCrit`, `app.js:577`), conveyed by colour + text
   only — no `role="status"`/`aria-live`, so none reach a screen reader. _Fix:
   `role="status" aria-live="polite"` (promote to `assertive` for critical)._
7. **Status/toast copy mixes two voices.** ALL-CAPS terminal style with `::`
   ("SIGNED IN :: …", "HISTORY CLEARED") coexists with Sentence case ("Added to
   the review queue", "Attach failed") on the same line. `app.js:4297` (and
   ~60 call sites). _Fix: normalise to one register — the CAD-house ALL-CAPS
   `::` is the dominant one._
8. **Filter/columns popup ignores Escape and orphans on resize.**
   `#popup-menu` is the only transient surface with no Escape path
   (`closePopup`, `app.js:2347`); every sibling honours Escape. Being
   `position:fixed` and positioned once, a window resize strands it.
   _Fix: `Escape → closePopup()` + a resize handler._
9. **Tooltip renders under its own overlay.** `#cad-tooltip` is `z-index:60`
   (`style.css:2512`) but the IA viewer sits at `z-index:61` (`:1579`) and
   carries `data-tip` triggers, so hovering them shows nothing. _Fix: raise the
   tooltip to `z-index:63+` (still below the confirm layer)._
10. **Auth is the lone form modal that ignores a backdrop click** (`app.js:1396`)
    — every other dismissable overlay closes on backdrop mousedown. _Fix: add
    the handler, or deliberately document login as sticky, but be consistent._

### Low (verification flagged these as overstated — optional)

- **Footer layout has minor variants** (`.dlg-actions` vs `.mf-actions` vs
  `.wizard-nav` vs centered `.about-links`; `style.css:1538`). Mostly serve
  genuinely different roles; consolidate opportunistically, not urgently.
- **The setup wizard titlebar has no `×`** (`index.html:1809`) — dismissable via
  "Set up later"/Escape. Likely a deliberate forced-flow choice; add the `×`
  only if strict chrome uniformity is wanted.

### Considered and dismissed (not issues)

- _Translate/delete confirms interpolate a raw language code_ — refuted: the
  user types that code themselves in the adjacent field, so echoing it is
  correct.
- _IA/webview titlebars "break" the title-left/close-right grammar_ — refuted:
  a `flex:1` title yields the same visual result.

---

## Recommended path

**Quick wins (localized, mostly mechanical — ~half a day):** items 2, 4, 5, 6,
7, 8, 9, 10. Each is a small, contained edit.

**One refactor that pays for the rest:** a shared
**`openOverlay()` / `closeOverlay()`** (or extend the existing confirm/lightbox
pattern) that, for any `.win`, stamps `role/aria-modal/aria-labelledby`, sets
initial focus, traps Tab, restores the opener, and toggles `inert` on the app
root. This resolves both **High** items plus the auth/settings focus cluster
(**item 3**) at once, and structurally prevents the _next_ new dialog from
regressing — the highest-leverage change here.

**Bottom line:** the dialog layer looks polished today; roughly a day of quick
wins plus the shared-overlay helper takes it from "looks polished, fails a11y"
to a genuinely no-nonsense enterprise bar.

_Files: `tools/whl_explorer/templates/index.html`,
`tools/whl_explorer/static/app.js`, `tools/whl_explorer/static/style.css`._
