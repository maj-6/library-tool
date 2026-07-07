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

Open http://127.0.0.1:5001. Classic-CAD styled UI; the title bar reads
`<ACTIVE TAB> :: CATALOG EXPLORER`. Below it sits the **application
toolbar**: `UNDO` / `REDO`, the active tab's commands, and `SETTINGS`.
Two tabs: **CHECKED BOOKS** (the working area) and **UPLOAD LIST** (the
book builder + approved sources).

UI conventions used everywhere:

- Table cells never wrap: overflowing text is ellipsized and the full text
  appears in a hover tooltip. Table views hide their scrollbars (scrolling
  still works). Links show their target URL in the hover tooltip.
- Status tags are fixed-width and abbreviated; a tag that matched a record
  is itself the link to that record.
- **Undo/Redo** (toolbar buttons, Ctrl+Z / Ctrl+Y outside text fields)
  covers checking/unchecking and clearing books, cell edits, verification
  markers and manual sources, manual-entry creation/deletion/edits, WHL
  corrections, and builder create/edit/delete/attach. The last 100 actions
  are kept per session.
- `SETTINGS` (toolbar): **theme** and **font** dropdowns plus per-column
  visibility for the checked table; everything persists in the browser.
  Seven themes, each a full rework of the interface chrome (borders, tab
  shapes, table rulings, tag geometry, tooltips, scrollbars) with element
  and text sizes preserved: CLASSIC CAD, ARCHIVE LEDGER (neutral archival
  paper), WORKSTATION 2000, SLATE STUDIO, PLATINUM, BLUEPRINT, MAINFRAME
  TERMINAL. Work canvases are flat colors (no background gridlines).
- Reusable components in `static/app.js`: `createMdEditor(container)` (the
  Obsidian-style live Markdown editor), `createPdfViewer()` (embedded PDF
  viewer), and `openFileBrowser(start, onPick)` (local PDF picker) — built
  to be mounted anywhere else in the interface.

## CHECKED BOOKS tab

A split layout: a left panel (resizable via the splitter), a top working
table, and an optional bottom search pane.

### Toolbar commands (application toolbar) + filter bar

Toolbar, when this tab is active:

- `RUN SCANS` — queues every row that has no scan results yet.
- `SCRAPE WHL` — fetches the complete metadata for every published book
  from the WHL website's REST API (~2 min for the whole catalogue,
  incremental and resumable; rows gain SRC `WEB`). Draft entries have no
  public page, so their extra fields stay empty. Scraped values sit under
  your corrections in precedence.
- `DOWNLOAD APPROVED` — IA PDFs for every approved book (below).
- `EXPORT JSON`, `CLEAR CHECKED`.
- `SEARCH PANE` — toggle button for the bottom pane (pressed = shown).

The tab's own filter bar:

- `FIND` — live filter, understood by every table on the tab and by the
  realtime Open Library query: `@token` = author (last name), `#token` =
  publication year, plain text = title words.
- `MARK` — filter by mark state (ALL / SCAN / UPLOAD / APPROVED / UNMARKED).

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
- `WHL EDIT` — appears when a WHL row is Ctrl+clicked (below): the full
  record editor. The DESCRIPTION field has a pencil button that opens a
  **Markdown editor window** (the live Obsidian-style editor: the text
  renders in place; the line under the caret shows its source. APPLY
  writes back to the field, SAVE CORRECTIONS persists).

### Top pane (dropdown selects the working table)

- `CHECKED BOOKS + MANUAL` — one combined table of manual entries and
  checked catalog books (`SRC` column) with `DEL` (manual) / `UNCHK`
  (catalog) actions. Metadata cells are edited in place: click a cell,
  type, Enter/blur commits (Escape cancels); manual-entry edits persist
  server-side, and any edit re-queues the row's checks and scans.
- `WHL CATALOG (EDITABLE)` — the whole WHL catalog with the full column
  set (title, subtitle, authors, year, publisher, pages, language, subject,
  description, status). Corrections never touch `whl_catalog.csv`; they
  live in `output/whl_corrections.json`. Rows are visually distinct:
  **edited** rows carry a cyan left bar and tint, **added** rows a green
  one, **draft** rows an amber one (SRC column: `CSV` / `WEB` / `EDITED` /
  `ADDED`).
  Two modes, toggled with the `MODE:` button or **Ctrl+E** (the current
  mode also shows as a tag in the footer): in EDIT mode click a cell to
  correct it, or Ctrl+click a row to load the whole record into the left
  panel's `WHL EDIT` tab; in SEARCH mode click a title to look it up on
  Open Library, then click a result to repopulate the row's metadata — the
  cleanup workflow for incomplete or mis-entered entries. `CONSTRAIN:`
  checkboxes choose which of the clicked row's columns narrow the lookup —
  `TITLE=` requires the title to appear verbatim (as a phrase), AUTHOR and
  YEAR filter by the row's values. The STATUS tag links to the catalogue
  page; its tooltip shows the target URL.

### Bottom pane (`SEARCH PANE` toolbar toggle)

A tabbed general-purpose viewer. `+` adds a tab; the active tab's dropdown
selects its table (OPEN LIBRARY / CH CATALOG / WHL CATALOG). All tabs
filter live from the FIND box (the Open Library tab queries the
consolidated index server-side). Hovering a row shows a tooltip with every
available field; clicking a row adds it to whatever table the top pane
shows, with columns mapped — into CHECKED it becomes a manual entry
(auto-checked and auto-scanned) or a checked catalog row for CH sources;
into WHL it becomes an added correction row.

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
already-downloaded volumes show `SAVED` and are skipped on later runs.

## UPLOAD LIST tab

The submission-preparation area. Toolbar commands when this tab is active:
`NEW ENTRY` (start a blank catalog entry), `EXPORT BUILDS` (download all
prepared entries as `whl_submission_entries.json` — the submission
package), `DOWNLOAD SOURCES` (save the approved-sources list as
`whl_upload_list.json`).

Two parts, separated by a **drag-to-resize splitter**:

- **The book builder** (top) — catalog entries being prepared for WHL
  submission, persisted in `output/whl_builds.json`. `BUILD` on an
  approved source starts an entry prefilled from the book's metadata, the
  provenance URL, and the PDF source; when the PDF was already downloaded
  the local `downloads/ia/<id>.pdf` path is attached automatically.
  The editor puts `SAVE`, the `READY FOR SUBMISSION` flag, and `DELETE` at
  the top, over two sub-tabs (their content scrolls):
  - `ENTRY` — the metadata fields (title, subtitle, authors, year,
    edition, publisher + city, language, pages, categories, PDF source
    URL, provenance URL, internal notes) with the **live Markdown
    description editor** occupying the space to their right: the
    description renders in the same box it is typed in (Obsidian-style —
    markers hide on rendered lines; the line under the caret shows its
    dimmed source).
  - `SOURCE (PDF)` — an embedded **PDF viewer** for verifying the actual
    PDF before marking the entry ready, plus the local-file interface: a
    path field, `BROWSE...` (a local-directory picker that lists drives,
    folders, and PDFs), and `ATTACH` (validates the file exists, then
    saves the path on the entry). A PDF auto-sourced from a URL that has
    already been downloaded gets its local path populated automatically;
    with no local file the viewer falls back to the remote URL.
- **APPROVED SOURCES** (bottom table) — every approved source across all
  rows: title, subtitle, author, publisher, year, the archive, and the
  matched record (linked, with the URL in the tooltip). Each row has a
  `BUILD` button.

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
