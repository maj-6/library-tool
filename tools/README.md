# World Herb Library tools

Code for the cataloging workbench described in the [root README](../README.md):
checking a private herbal library against the World Herb Library (WHL),
locating existing scans, and preparing new catalog entries for WHL
submission. The single application is the catalog explorer
(`whl_explorer/`); everything else is a shared module, an index builder, or
a standalone CLI.

## Layout

- `whl_explorer/` — **the catalog explorer** (Flask; `server.py`,
  `templates/`, `static/`). The whole workflow lives here.
- `libcommon.py` — shared helpers (repo paths, ids, JSON IO).
- `convert_xlsx.py` — converts `ch_library.xlsx` to `output/ch_library.json`.
- `catalog_checks.py` — offline copyright + WHL-catalogue checks (loaders,
  indexes, and the shared cross-database identity test).
- `scan_search.py` — Internet Archive + HathiTrust scan search + JSON CLI.
- `whl_scrape.py` — scrapes complete metadata for every published WHL book
  (publisher, print length, subtitle, description, language, subject) via
  the site's WordPress REST API into `output/whl_scraped.json`.
- `build_ol_index.py` — converts the Open Library works dump into
  `output/ol_works.db` (fallback index; also feeds author keys to the build
  below).
- `build_ol_search.py` — consolidates the editions + authors + works dumps
  into `output/ol_search.db`: only editions published up to `--max-year`
  (default 1950), with author names, publisher, place, year, edition and
  volume all local and FTS5-indexed (prefix indexes for search-as-you-type).
- `ol_client.py` — constrained search over those indexes; the consolidated
  editions index needs no Open Library API calls at all.
- `whl_client.py` — WHL live-search client + JSON CLI (also provides the
  normalization/accuracy helpers shared by the checks and scan search).
- `build_catalog_report.py` — one-shot spreadsheet report over the whole
  CH library (see the last section).

## Setup

Use the `python3` interpreter (Python 3.13).

```
python3 -m pip install --user -r tools/requirements.txt
python3 tools/convert_xlsx.py       # once: ch_library.xlsx -> output/ch_library.json
```

### Open Library indexes (once)

```
python3 tools/build_ol_index.py     # works index (fallback + author keys)
python3 tools/build_ol_search.py    # consolidated editions index (~10-15 min)
```

The result is `output/ol_search.db` (~4.6 GB, ~7.7M editions published up to
1950) where title, author, publisher and place are one prefix-indexed FTS
table and year/volume are SQL columns — every query is local and answers in
milliseconds, including search-as-you-type. While `ol_search.db` is absent
the app falls back to the works index + live OL API (cached in
`output/.ol_api_cache.json`).

# The catalog explorer

```
python3 tools/whl_explorer/server.py
```

Open http://127.0.0.1:5001. The chrome, top to bottom: title bar, **menu
bar** (FILE / EDIT / VIEW / TOOLS — every common function lives here;
RUN SCANS, SCRAPE WHL, and the SEARCH PANE toggle live *only* here), and
the **tab strip** — **CATALOGS** (the working area) and **EDITOR** (the
book builder + verified sources) on the left, with the action icons
inline on the right: undo/redo, the active tab's commands (EDITOR: new
entry, export builds, download sources), and the settings gear.

UI conventions used everywhere:

- The interface font (labels, buttons, menus) and the data/table font are
  independent settings drawing from the same font list (sans, serif, and
  monospace faces for either role).
- Table cells never wrap: overflowing text is ellipsized and the full text
  appears in a hover tooltip (long notes/description values are
  abbreviated). Links show their target URL in the tooltip. Table views
  hide their scrollbars.
- **Click a column header to sort** by it (again to reverse; arrow shows
  the direction) in the checked and WHL tables. Every table's **columns
  are resizable** (drag a header's right edge; widths persist) and every
  table has a **column-visibility icon** in the bar above it. The maximum
  number of displayed rows is a setting (TABLE VIEW).
- **Ctrl+click a row in any table to open it in the EDIT tab**.
- Status tags are fixed-width and abbreviated; a tag that matched a record
  is itself the link to that record.
- **Undo/Redo** (tab-strip icons, EDIT menu, Ctrl+Z / Ctrl+Y) covers
  checking/unchecking, cell and record edits, verification markers and
  manual sources, manual-entry creation/deletion, WHL corrections, and
  builder create/edit/delete/attach. Deletes never ask for confirmation —
  they are undoable. The last 100 actions are kept per session.
- The settings gear opens a categorized window (sidebar: GENERAL /
  APPEARANCE / TABLE VIEW / FILE PATHS). Seven themes, each a full rework
  of the interface chrome with element and text sizes preserved: CLASSIC
  CAD (modernized flat chrome over the dark drafting canvas), ARCHIVE
  LEDGER (neutral archival paper), PLATINUM, BLUEPRINT (warm paper over a
  warm neutral-dark board), MODERN LIGHT, MODERN DARK, and STONE
  (warm-gray light). Retired theme ids migrate automatically.
- Titles and subtitles filled from Open Library are converted to
  conventional title case; "Last, First" author names are flipped to
  "First Last" when repopulating WHL rows.
- Reusable components in `static/app.js`: `createMdEditor(container)` (the
  Obsidian-style live Markdown editor), `createPdfViewer()` (embedded PDF
  viewer with an optional parallel OCR-text pane, fed by `/api/pdf/text`),
  and `openFileBrowser(start, onPick)` (local PDF picker).

## CATALOGS tab

A split layout: a left panel (resizable via the splitter), a top working
table, and an optional bottom search pane. `RUN SCANS` (queue checks +
scans for rows that have none) and `SCRAPE WHL` (fetch complete metadata
for every published book from the WHL website's REST API — incremental and
resumable; rows gain SRC `WEB`; scraped values sit under your corrections)
are in the TOOLS menu; the SEARCH PANE toggle is in the VIEW menu.

The bar above the table carries `EXPORT` (JSON of the table **as
filtered**), a download icon ("Download all verified sources"), the
**filter icon** (MARK / SOURCE / DOWNLOAD-status popup; highlighted while
any filter is active), and the column-visibility icon.

The find bar: the magnifier field filters every table on the tab live and
drives the realtime Open Library query — `[title]` words, `@author`
(last name), `#year`.

### Left panel (three sub-tabs)

- `SEARCH` — constrained Open Library search: title / author / publisher /
  city / year / edition / volume fields act as live constraints; results
  appear in the bottom pane's OPEN LIBRARY table as you type.
- `MANUAL ENTRY` — the entry form (title, author, publisher, city, year,
  edition, volume number, language, pages, condition, price, illustrations,
  categories, notes; title required). Entries are saved to
  `output/manual_entries.json` and checked automatically on submit. Typing
  a Roman-numeral date into the year field (common on old title pages)
  shows the Arabic year live in the footer's right corner.
  **Autocomplete:** from 3 characters the title field suggests editions
  from the local index, constrained by hand-typed fields. Picking one fills
  author, publisher, city, year, edition, and volume. Auto-filled fields
  are shaded light yellow, hand-typed fields light green; green fields
  constrain the search and are never overwritten (except the title itself,
  which the pick completes).
- `EDIT` — the record editor, opened by **Ctrl+clicking a row in any
  table**. It adapts its field set to the source: a WHL row gets the WHL
  fields (title, subtitle, authors, year, publisher, print length,
  language, subject, categories, description — the description's pencil
  opens the live Markdown editor window; SAVE writes to the corrections
  overlay); a checked/manual row gets the book fields (SAVE patches the
  manual entry or updates the checked copy and re-queues its checks); a CH
  catalog row from the bottom pane gets the book fields plus ACQUIRED, and
  SAVE checks the record into CHECKED BOOKS with the edits applied.

### Top pane (dropdown selects the working table)

- `CHECKED BOOKS + MANUAL` — one combined table of manual entries and
  checked catalog books (`SRC` column) with icon actions (trash = delete a
  manual entry, minus-circle = uncheck a catalog book). Metadata cells are
  edited in place: click a cell, type, Enter/blur commits (Escape
  cancels); manual-entry edits persist server-side, and any edit re-queues
  the row's checks and scans. IA download state shows as a dot inside the
  tag's right edge (the label stays centered): **green** = saved (tooltip:
  the file path), **red** = failed (tooltip: the error).
- `WHL` — the whole WHL catalog with the full column set (title, subtitle,
  authors, year, publisher, pages, language, subject, description,
  status). Corrections never touch `whl_catalog.csv`; they live in
  `output/whl_corrections.json`. Rows are visually distinct: **edited**
  rows carry a cyan left bar and tint, **added** rows a green one,
  **draft** rows an amber one (SRC column: `CSV` / `WEB` / `EDITED` /
  `ADDED`). **Holding Alt over an edited row shows the original record**
  (grayed, yellow-tinted); the same works in the `EDIT` panel while a WHL
  record is loaded.
  Two modes, toggled with the `MODE:` button or **Ctrl+E** (the current
  mode also shows as a tag in the footer): in EDIT mode click a cell to
  correct it; in SEARCH mode click a title to look it up on Open Library,
  then click a result to repopulate the row's metadata — titles/subtitles
  are title-cased and "Last, First" authors flipped on the way in. Title,
  author, and year copy by default; **Ctrl+click an Open Library column
  header** (green) to force-copy it — publisher and language become
  available this way — and **Shift+click** (red) to exclude one.
  Ctrl+click opens the record in the `EDIT` tab from either mode. The
  constraint group (target icon) chooses which of the clicked row's
  columns narrow the lookup — TITLE requires the title to appear verbatim.
  A published entry's `PUB` tag opens its publication PDF in a viewer
  window (with an optional parallel OCR pane — GENERAL settings) instead
  of a browser tab.

### Bottom pane (`SEARCH PANE` toolbar toggle)

A tabbed general-purpose viewer. `+` adds a tab; the active tab's dropdown
selects its table (OPEN LIBRARY / CH CATALOG / WHL CATALOG). All tabs
filter live from the find box (the Open Library tab queries the
consolidated index server-side). Hovering a row shows a tooltip with every
available field; clicking a row adds it to whatever table the top pane
shows, with columns mapped — into CHECKED it becomes a manual entry
(auto-checked and auto-scanned) or a checked catalog row for CH sources;
into WHL it becomes an added correction row. Ctrl+click opens CH and WHL
records in the `EDIT` tab instead.

### Automatic checks + scans

There is no per-row scan button: rows are checked and scanned automatically.
Adding a book queues it; editing any cell re-queues it (stale results are
cleared to `---` until the rescan lands). The queue runs one book at a time
so a burst of adds doesn't hammer the archives; progress shows in the status
bar.

Checks are offline, against local databases (logic in `catalog_checks.py`;
indexes load once at server start):

- `COPYRIGHT` via `copyright_renewals.csv`: public domain by age; renewal
  lookup for 1931–1963 (reports the renewal id); auto-renewed 1964–1977; in
  copyright from 1978. The tag answers "is it under copyright?": `NO`
  (green) = public domain, `YES` (red) = in copyright.
- `WHL` via the local catalogue copy `whl_catalog.csv`: `YES` with a link
  to the matched catalogue page, `DRFT` (draft only), or `NO`. The WHL
  website is never queried for this check.

Scan search (in `scan_search.py`) is the only per-row network step:

- Internet Archive: public advancedsearch API, quoted-title and
  surname-filtered queries, results ranked by a composite accuracy.
- HathiTrust: its catalog search is closed to programs (robots.txt), so the
  official Bib API is used instead — OCLC numbers are discovered through
  the Open Library search API, then looked up in one Bib API call. `VIEW`
  marks a full-view scan.
- Hovering a `NO` scan tag shows the closest result that stayed below the
  acceptance threshold (title, author, year, accuracy), so near-misses can
  be judged by eye.

### Match verification and SCAN / UPLOAD marks

Every positive match (`WHL` / `IA` / `HT` tag) carries a small marker fused
to the tag's right edge: **yellow** = pending, **green** = approved,
**red** = rejected as a false positive. Clicking the **marker** cycles
pending → approved → rejected → pending; the tag itself stays a plain link.
A rejected match renders as `NO` and stops counting as found. Clicking a
rejected tag opens a box to paste the URL of a manually located source:
once saved, the tag reads `YES` again and links to that URL, and the source
feeds the upload list and `DOWNLOAD APPROVED`. Verification persists
(server-side for manual entries, browser-side for catalog rows) and resets
when the row's metadata is edited.

The `MARK` column classifies each book from the verified picture:

- `SCAN` — not in WHL, not under copyright, and no (surviving) scan found
  online: the physical book should be scanned.
- `UPLD` — not in WHL but a scan exists in another online archive; amber
  while its sources are unverified, green once at least one is approved.
- otherwise no mark; the tooltip on the dash explains why.

### Internet Archive PDF downloads

`DOWNLOAD APPROVED` downloads the best IA match for every book with an
approved IA source: the item's PDF derivative is saved to
`downloads/ia/<identifier>.pdf` and a cataloging entry is written to
`downloads/ia/catalog.json` combining the IA record with the book's own
catalogue metadata. Progress shows in the IA tag cell and the status bar;
already-downloaded volumes show the `*` marker and are skipped on later
runs.

## EDITOR tab

The submission-preparation area. Its tab-strip icons: new entry, export
builds (`whl_submission_entries.json` — the submission package), and
download sources (`whl_upload_list.json`); the same commands are in the
FILE menu.

Two parts, separated by a **drag-to-resize splitter**:

- **PENDING** (the book builder) — catalog entries being prepared for WHL
  submission, persisted in `output/whl_builds.json`. The build icon on a
  verified source starts an entry prefilled from the book's metadata, the
  provenance URL, and the PDF source; when the PDF was already downloaded
  the local `downloads/ia/<id>.pdf` path is attached automatically.
  The editor puts the save and delete icons side by side at the top with
  the VERIFIED toggle (a check icon; pressing it reveals a VERIFIED tag),
  over two sub-tabs (their content scrolls):
  - `ENTRY` — the metadata fields with the **live Markdown description
    editor** occupying the space to their right (Obsidian-style — markers
    hide on rendered lines; the line under the caret shows its dimmed
    source).
  - `SOURCE (PDF)` — an embedded, **undecorated PDF viewer** (no browser
    toolbar; the file size shows in the bar) with an **OCR icon** that
    opens the extracted text layer in a parallel pane, for verifying the
    actual PDF before marking the entry VERIFIED. The PATH TO PDF field
    has folder (browse) and attach icons; attach validates the file
    exists before saving the path. With no local file the viewer falls
    back to the remote URL.
- **VERIFIED SOURCES** (bottom table) — every verified source across all
  rows: title, subtitle, author, publisher, year, the archive, and the
  matched record (linked, with the URL in the tooltip). Each row's build
  icon starts a prefilled entry.

# Standalone CLIs

Scan search:

```
python3 tools/scan_search.py --title "American Medicinal Plants" --author "Millspaugh" --year 1887
```

WHL live search (uses the site's public search API; matching is
case-insensitive and ranked by a composite `accuracy` over title prefix
0.5 / author prefix 0.3 / year 0.2, renormalized when fields are missing):

```
python3 tools/whl_client.py --title "An Introduction to Botany" --author "Lindley" --date 1835
```

WHL metadata scrape (also available from the explorer toolbar):

```
python3 tools/whl_scrape.py
```

# CH Library status report

Builds `output/ch_library_report.xlsx`: every `ch_library.xlsx` row and
column, plus `In WHL`, `Available online`, `In local library`, and
`Copyright status`.

```
python3 tools/build_catalog_report.py              # offline columns
python3 tools/build_catalog_report.py --online --limit 200
```

- `Copyright status` (offline, via `copyright_renewals.csv`): as in the
  explorer's COPYRIGHT check.
- `In WHL` (offline, via `whl_catalog.csv`): `yes`, `draft`, or `no`.
- `In local library` (offline): matched against the manual entries (and
  `local_library_partial.json` when present).
- `Available online` (network, opt-in `--online`): Internet Archive search,
  cached to `output/.online_cache.json`; defaults to `not checked`.
  `--limit` caps lookups (0 = no cap).

Matching across all three uses a shared title-forward, surname-based test
(`title_author_match`): a strong first-16-char title prefix plus a
full-title ratio, confirmed by an order-agnostic surname-token overlap. It
is case-insensitive and tolerant of `Lastname, Initials` vs `Firstname
Lastname`, appended subtitles, OCR typos, and differing edition years.
