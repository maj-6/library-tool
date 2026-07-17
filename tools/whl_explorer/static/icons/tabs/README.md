# Activity-bar tab icons

One SVG per tab, named by the tab's `data-tab` id (`home.svg`,
`checked.svg`, `workbench.svg`, `publish.svg`, `infotab.svg`, ...).
Swap any file to change that tab's icon — the app inlines these at
startup, so use `stroke="currentColor"` (and no fixed width/height) and
the icon follows the theme's tab colors. 16x16 viewBox, stroke ~1.5 to
match the house line style. A tab with no icon file falls back to a
two-letter label.
