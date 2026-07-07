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
bar** (File / Edit / View / Tools — every common function lives here;
Run scans, Scrape WHL, and the Search pane toggle live *only* here), and
the **tab strip** — **Catalogs** (the working area), **Editor** (the
book builder + verified sources), and **OCR** (OCR review tooling) on
the left, with the action icons inline on the right: undo/redo, the
active tab's commands (Editor: new entry, export builds, download
sources), and the settings gear.

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
  are resizable** (drag a header's right edge; widths persist) — except
  the tag/action columns, whose compact widths are locked — and a
  designated column stretches so the table never leaves empty space on
  its right. Every table has a **column-visibility icon** in the bar
  above it. The maximum number of displayed rows is a setting (TABLE
  VIEW).
- **Ctrl+click a row in any table to open it in the EDIT tab**.
- Status tags are fixed-width and abbreviated; a tag that matched a record
  is itself the link to that record.
- **Undo/Redo** (tab-strip icons, EDIT menu, Ctrl+Z / Ctrl+Y) covers
  checking/unchecking, cell and record edits, verification markers and
  manual sources, manual-entry creation/deletion, WHL corrections, and
  builder create/edit/delete/attach. Deletes never ask for confirmation —
  they are undoable. The last 100 actions are kept per session.
- The settings gear opens a categorized window (sidebar: GENERAL /
  APPEARANCE / TABLE VIEW / AI / FILE PATHS). Nine themes, each a full
  rework of the interface chrome with element and text sizes preserved:
  CLASSIC CAD (modernized flat chrome over the dark drafting canvas),
  ARCHIVE LEDGER (neutral archival paper), PLATINUM, BLUEPRINT (warm
  paper over a warm neutral-dark board), MODERN LIGHT, MODERN DARK,
  STONE (warm-gray light), MIDNIGHT (deep blue-black), and SAGE (muted
  green-gray). Status tags are square in every theme. Retired theme ids
  migrate automatically.
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
  subtitle, edition, volume number, language, pages, condition, price,
  illustrations, categories, notes; title required). Entries are saved to
  `output/manual_entries.json` and checked automatically on submit.
  **Titles are parsed on submit**: text after a colon becomes the
  subtitle, and volume/edition indicators (`vol. 1`, `v2`, `v. iii`,
  `2nd ed.`, `Third Edition`) are removed from the title and land in
  their own fields — existing entries were migrated the same way (their
  scans and verifications were preserved), and the same parse applies to
  master-list and Open Library rows at display time and on add. Typing
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
  manual entry or updates the checked copy and re-queues its checks); a
  master-list row from the bottom pane gets the book fields plus ACQUIRED,
  and SAVE checks the record into CHECKED BOOKS with the edits applied.

### Top pane (dropdown selects the working table)

**Both tables have EDIT and SEARCH modes**, toggled with the `MODE:`
button or **Ctrl+E** (the current mode shows as a footer tag). In EDIT
mode cells are edited in place; in SEARCH mode click a row's title to
look it up on Open Library (constrained by the target-icon group — TITLE
requires the title verbatim), then click a result in the bottom pane to
repopulate the row: titles/subtitles are title-cased and "Last, First"
authors flipped on the way in. Title, author, and year copy by default;
**Ctrl+click an Open Library column header** (green) to force-copy it
(publisher/language for WHL rows; also city/edition/volume for books)
and **Shift+click** (red) to exclude one. Ctrl+click a row opens it in
the `EDIT` tab from either mode.

- `CHECKED BOOKS + MANUAL` — one combined table of manual entries and
  checked catalog books (`SRC` column) with icon actions (trash = delete a
  manual entry, minus-circle = uncheck a catalog book). Any edit or
  repopulation re-queues the row's checks and scans. IA download state
  shows as a dot inside the tag's right edge (the label stays centered):
  **green** = saved (tooltip: the file path), **red** = failed (tooltip:
  the error).
- `WHL` — the whole WHL catalog with the full column set (title, subtitle,
  authors, year, publisher, pages, language, subject, description,
  status). Corrections never touch `whl_catalog.csv`; they live in
  `output/whl_corrections.json`. Rows are visually distinct: **edited**
  rows carry a cyan left bar and tint, **added** rows a green one,
  **draft** rows an amber one (SRC column: `CSV` / `WEB` / `EDITED` /
  `ADDED`). **Holding Alt over an edited row shows the original record**
  (grayed, yellow-tinted); the same works in the `EDIT` panel while a WHL
  record is loaded.
  A published entry's `PUB` tag opens its publication PDF in a viewer
  window (with an optional parallel OCR pane — GENERAL settings) instead
  of a browser tab.

### Bottom pane (`SEARCH PANE` toolbar toggle)

A tabbed general-purpose viewer. `+` adds a tab; the active tab's dropdown
selects its table (Open Library / **Master list** — the private-library
catalogue, with Subtitle, Vol, and Ed columns fed by the title parse /
WHL catalog). The Master list doubles as the **Google Sheets publish
preview**: manual entries appear as **light-yellow** rows (they would be
appended to the sheet) and already-checked catalogue rows are **light
blue**. **Tools > Sync master list to Google Sheets** publishes it —
always a manual action; Settings > Sync holds the spreadsheet ID,
service-account key file, and sheet name (no credentials yet, so the
sync is TODO-verify). The search pane has a **clear button** and empties
itself when you switch to another pane tab. All tabs
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
  online: the physical book should be scanned. **Clicking the SCAN tag
  opens the file picker to attach the scanned PDF** — the tag turns
  green with a **green dot marking it as an approved source**, the row
  counts as a **verified source** (a "Local scan" row in the Editor's
  verified-sources table), and its build icon seeds a new WHL entry with
  the local PDF attached. Clicking again replaces the file;
  **Shift+click detaches it**. The attached-scan tag stays visible (and
  clickable) even when the row's computed mark changes later.
  Download/approval dots inside tags that carry a verification marker
  sit on the tag's **left** edge so the two indicators stay distinct.
  **Shift+click anywhere else on a row marks it purple ("needs
  attention")**; Shift+click again clears it.
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

- **Pending / Uploaded** (the book builder) — catalog entries being
  prepared for WHL submission, persisted in `output/whl_builds.json`.
  **Pending means awaiting upload to WHL**; the upload icon in the entry
  actions moves a verified entry to the **Uploaded** sidebar tab and out
  of the queue (the actual WHL upload API call is a later feature — the
  button currently just performs the queue transition, undoably). The
  sidebar is compact: each entry shows its title with the **status icon
  inline on the right** (pencil = draft, green check = verified, export
  arrow = uploaded) over an author · year line; verified entries are
  tinted green; **Shift+click marks an entry purple ("needs
  attention")**. The build icon on a
  verified source starts an entry prefilled from the book's metadata, the
  provenance URL, and the PDF source; when the PDF was already downloaded
  the local `downloads/ia/<id>.pdf` path is attached automatically
  (locally attached scans arrive with the local path as the PDF).
  The editor puts the save and delete icons side by side at the top with
  the VERIFIED toggle (a check icon; pressing it reveals a VERIFIED tag),
  over two sub-tabs (their content scrolls):
  - `ENTRY` — the metadata fields (their scrollbar is hidden) with the
    **live Markdown description editor** occupying the space to their
    right (Obsidian-style — markers hide on rendered lines; the line under
    the caret shows its dimmed source). Next to the DESCRIPTION label: a
    **sparkle icon** generates an AI summary from the PDF's OCR text via
    the OpenAI-compatible endpoint configured under SETTINGS > AI (base
    URL, model, API key, custom instructions), and a **file icon** loads
    the description from a local text file. Both leave the result unsaved
    until SAVE.
  - `Source (PDF)` — an embedded, **undecorated PDF viewer** (no browser
    toolbar or scrollbars; the file size shows in the bar) with an **OCR
    icon** that opens the text layer in a parallel pane. Large scans load
    fast because the viewer shows a **compressed, truncated preview
    derivative** (page limit and a preview-the-original toggle live in
    GENERAL settings). The PATH TO PDF field has folder (browse) and
    attach icons. The **OCR row** lists the entry folder's OCR files as
    chips — click one to make it the ACTIVE OCR (it feeds the OCR pane);
    load additional OCR files for comparison with the file icon (PDFs
    without a text layer get their OCR supplied this way). The
    **folder-sync icon builds the entry folder**
    (`output/entries/<id>/`): `metadata.json`, `preview.pdf`, and
    `ocr/extracted.txt`; when KEEP IA ORIGINALS is off, the downloaded
    original is treated as a temporary artifact and removed after the
    preview is built — the entry's PDF is repointed at the folder's
    `preview.pdf` and the IA download catalog entry is retired (removal
    only happens when the sync just produced a fresh preview). **Saving
    the entry marks the active OCR file as verified** (`ocr_verified`).
- **VERIFIED SOURCES** (bottom table) — every verified source across all
  rows: title, subtitle, author, publisher, year, the archive, the
  matched record (linked, with the URL in the tooltip), and a **Status**
  column: a source whose entry is in the editor is **yellow / DRAFT**;
  once that entry is verified it turns **green / DONE**. The **filter
  icon** hides statuses (e.g. hide done sources). Each row's build icon
  starts a prefilled entry.

## OCR tab

The OCR workbench. OCR **targets are the books' PDFs**; the sidebar
lists every **book folder** (entries with `output/entries/<id>/`,
author · year · OCR file count, status icon; the check icon in the pane
bar filters to verified books only — built for working through a large
queue of entries). Selecting a book loads its `ocr/*.txt` files into
the **documents** list below (**a book without OCR files gets its PDF
text layer extracted and saved automatically** as `ocr/extracted.txt`);
loose text files can still be loaded with the file icon.

Three views, toggled by the icon buttons (all controls are icons with
tooltips):

- **Edit** (pencil) — plain-text corrections with find / replace-all.
- **Diff** (columns) — line-level comparison against any other loaded
  document, unchanged runs collapsed.
- **PDF pages** (page icon) — the PDF displayed **in parallel with the
  OCR text**: one row per page, the page image (rendered server-side via
  PyMuPDF, cached) beside that page's OCR text, the text box stretched
  to the page's height. Both live in one scroll container, so they
  scroll together. Page texts are editable; edits flow back into the
  document. Files without `--- page N ---` markers show their full text
  beside page 1.

In the page view, **pages are queued for OCR individually**: hover a
page and press a digit — the digit → service mapping is customizable
(Settings > OCR, keys 1–5; key 1 defaults to Tesseract). **Ctrl+digit
arms a service for a range**: Ctrl+click marks the range start (dashed
outline), Ctrl+click on another page queues everything between them.
Queued pages carry a cyan outline and a service chip. **Pressing T over
a page marks it as a title page** (purple T chip; stored on the entry
as `title_pages` — intelligent metadata extraction will use these
later). Escape clears the armed service / pending range.

**OCR processing is live**: jobs rasterize each page server-side
(PyMuPDF, width configurable in Settings > OCR — the knob for
experimenting with how compression/shrinking affects OCR quality) and
run it through the chosen service — **Tesseract (local, tested and
working)**, **Claude**, or **Amazon Textract** (both TODO-verify: no
API keys yet; Azure/OpenAI remain queue stubs). Every finished page is
merged into **one compiled OCR document** (`ocr/compiled.txt`) and
saved immediately — results from different services land in the same
single file, which appears in the documents list when the job ends.
The queue table shows live progress (`Running — n/total`).

Quality assessment persists on the entry (`ocr_quality`); the star icon
sets the document as its entry's **active OCR**; save writes folder
documents back (local documents download). Service credentials live in
**Settings > OCR** (Tesseract path override, Anthropic key + model,
AWS key/secret/region, Azure endpoint + key; OpenAI reuses the AI
settings).

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
