# Activity-bar tab icons

One SVG per tab, named by the tab's `data-tab` id (`home.svg`,
`checked.svg`, `workbench.svg`, `publish.svg`, `infotab.svg`, ...).
Swap any file to change that tab's icon — the app inlines these at
startup, so use `stroke="currentColor"` (and no fixed width/height) and
the icon follows the theme's tab colors. 16x16 viewBox, stroke ~1.5 to
match the house line style. A tab with no usable icon falls back to a
two-letter label (with a note in the console).

## Icon styles

A theme may declare `--icon-style: <name>` to use the set in the
`<name>/` subfolder instead (same filenames). Files missing from a
style folder fall back to the base set, so a partial style still shows
every tab. Ships with `solid/` — filled, stamped glyphs
(`fill="currentColor"`, no stroke) used by the tactile themes (Ledger,
Manuscript); the base folder is the line set the flat themes use.

These files are treated as IMAGES, not code: scripts, event-handler
attributes, `foreignObject`, and external references are stripped on
load, so a downloaded icon pack can't run anything. An XML prolog
(`<?xml ...?>`) is fine.
