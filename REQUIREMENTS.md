# Catalog cross-reference requirements

Reference for the cross-database catalog work (title-page ingestion, local
library consolidation, and the CH Library status report). Captured for later;
the current build focuses on the copyright/report utility.

## Deliverables

### 1. Title-page ingestion tool (ChatGPT API)
- Ingest a collection of book title-page images and read them with the ChatGPT
  (OpenAI) API to generate structured metadata per book.
- Store the output in a JSON object named `scanned_titles`.
- Fields should align with the existing metadata schema (title, subtitle,
  author, publisher, published_date, language, edition, page_count, notes).

### 2. Consolidated local library (`in_local_library`)
- Combine `scanned_titles` with the contents of `local_library_partial`.
- `local_library_partial` is sparse: only author last name and book title, and
  not all entries have a publisher or published date.
- Expect duplicates across the two sources; de-duplicate on a fuzzy key.
- Output a new JSON named `in_local_library` (the merged, de-duplicated set),
  preferring the richer record when two sources describe the same book.

### 3. CH Library status report (spreadsheet)
- Generate a spreadsheet containing every book in CH Library, using the same
  formatting/columns as `ch_library.xlsx`, plus these added columns:
  - `In WHL` — matched against the WHL catalogue export `whl_catalog.csv`
    (`yes` published / `draft` unpublished / `no`). No scraping needed.
  - `Available online` — searched against a public online library
    (Internet Archive).
  - `In local library` — cross-referenced against `in_local_library`
    (currently the manual entries / partial set until scanning is wired up).
  - `Copyright status` — determined via `copyright_renewals.csv`.

## Data sources
- `ch_library.xlsx` / `output/ch_library.json` — 5264 rows. Columns: AUTHORS,
  PUBLICATION (underscore-joined title), YEAR_OF_PU, EDITION, CONDITION,
  PAGE_REFER, CITY_PUBLI, PUBLISHER, KEY, KEY_2, KEY_3, ILLUSTRATI, NOTES,
  PRICE, DATE.
- `copyright_renewals.csv` — Catalog of Copyright Entries renewal records.
  Columns: `ID, DATE, TITLE, AUTHOR, OREG, DREG, ODAT, CLNA, OCLS, INAN, NOTE,
  LINM, MISC, EDST, XREF, ADTI, SEST`.
  - `ID` renewal id (e.g. `R64009`); `DATE` renewal year; `TITLE`/`AUTHOR` the
    work; `OREG` original registration no.; `DREG` renewal date; `ODAT`
    original copyright date (e.g. `24Jun23` -> 1923); `CLNA` claimant.
  - Presence in this file means the copyright WAS renewed. Absence (for a work
    in the renewal-required era) implies it was not renewed.
- `whl_catalog.csv` — World Herb Library catalogue export. Columns: `Title,
  Authors, Year Published, Library Categories, Permalink, Status, Publication
  File`. `Status` is `publish` or `draft`; the file has many near-duplicate
  rows per book. Used offline for the `In WHL` column (no scraping).
- `output/manual_entries.json` — the hand-entered local books; stand in for
  the local library until scanning + the `local_library_partial` source are
  available. (The earlier dictation/photo pipeline and its
  `books_metadata.json` / `library_db.json` outputs are retired.)
- `local_library_partial` — not yet present; sparse (author-last + title).

## Copyright status logic (US-centric heuristic)
Let `Y` = current year and `pub` = YEAR_OF_PU.
- No/!parseable year -> `Unknown (no year)`.
- `pub <= Y-96` -> `Public domain (published <year>)` (95-yr term expired).
- `1931..1963` (renewal-required era) -> look up the renewals set:
  - match found -> `In copyright (renewal <ID>)`.
  - no match -> `Public domain (no renewal found)`.
- `1964..1977` -> `In copyright (auto-renewed)`.
- `>= 1978` -> `In copyright`.
Notes/caveats: rules are US-centric; most CH herbals predate 1930 and are PD by
age. Foreign works and edge cases (serials, contributions) are approximate.

## Cross-database matching heuristics
Databases disagree on formatting, spelling (OCR), and completeness, so identity
is decided by a tolerant composite rather than exact strings:
- Normalize case-insensitively: lowercase, strip punctuation/diacritics,
  collapse whitespace; expand `PUBLICATION` underscores to spaces.
- Title: compare the first ~16 normalized characters (subtitles are frequently
  appended to the main title, and OCR noise clusters later in the string).
- Author: normalize name order — flip `Lastname, Firstname` to
  `Firstname Lastname` (guarding credential tails like `M. D.`, `Jr.`), then
  compare the first ~8 characters and/or match on last-name token.
- Year: exact 4-digit match when both sides have one; used as a tie-breaker /
  confirmer, not required (many records omit dates).
- Scoring: weighted blend (title ~0.5, author ~0.3, year ~0.2) renormalized
  over whichever fields are present; accept above a threshold (~0.6-0.72
  depending on how much corroborating metadata exists).
- Indexing for scale: bucket the renewals set by author last-name token and by
  title prefix so each CH book only compares against plausible candidates.
- Only run the renewal search for books in the renewal-relevant window
  (1931-1963); books PD by age skip the lookup entirely.

## Status / decisions
- `whl_client.py` implements the case-insensitive composite match and a live
  WHL search; its text utilities (`_normalize`, `similarity`, `flip_author`,
  `_year`) are reused by the report utility.
- `tools/build_catalog_report.py` computes `Copyright status`, `In WHL`
  (against `whl_catalog.csv`), and `In local library` offline. Only
  `Available online` (Internet Archive) needs the network; it is opt-in
  (`--online`) with on-disk caching and defaults to `not checked`.
- Cross-database matching is centralized in
  `tools/catalog_checks.py::title_author_match`: a strong first-16-char title
  prefix plus a full-title ratio, confirmed by an order-agnostic
  surname-token overlap; year is never required. The module also owns the
  renewals/WHL-catalogue loaders and indexes.
- The WHL-CAD explorer (`tools/whl_explorer/`) consolidates Checked Books and
  Manual Entry in one split-pane tab: the manual entry form (title, author,
  publisher, city, year, edition, volume, language, pages, condition, price,
  illustrations, categories, notes; stored in `output/manual_entries.json`)
  next to a combined table of manual entries + checked catalog books. Every
  submitted entry is checked against `copyright_renewals.csv` and the local
  `whl_catalog.csv` via `catalog_checks.check_entry` — fully offline, no WHL
  website queries; `RUN SCANS` computes the same checks for catalog rows.
- `tools/scan_search.py` searches for existing scans: Internet Archive via
  its public advancedsearch API; HathiTrust via its official Bib API keyed on
  OCLC numbers discovered through Open Library (HathiTrust catalog search is
  robots-disallowed for programs). Runs automatically through the scan queue
  when a row is added or edited, plus a `RUN SCANS` batch.
- Per-source verification: each positive WHL/IA/HT match carries a marker on
  the tag's right edge (shaded fill + 1px border; yellow pending, green
  approved, red rejected as a false positive). Clicking the MARKER cycles the
  state (the tag stays a link); a rejected match renders as `NO` and is
  excluded from classification, so a false-positive IA hit falls back to
  `SCAN` when appropriate. Clicking a rejected tag opens a paste box for the
  URL of a manually located source, which then acts as the verified record
  (link, upload list, IA download). Roman-numeral years typed into the
  manual-entry year field show their Arabic value in the footer.
- Open Library integration: `tools/build_ol_index.py` (works dump ->
  `output/ol_works.db`, fallback) and `tools/build_ol_search.py`
  (editions + authors + works dumps -> `output/ol_search.db`: ~7.7M editions
  published <= 1950, denormalized with author names/publisher/place/year/
  edition/volume, column-filtered FTS5 with prefix indexes — all searches
  local, no API). The manual pane is split into SEARCH (constraint fields
  driving the realtime OPEN LIBRARY table) and MANUAL ENTRY (title
  autocomplete constrained by hand-typed fields; picks populate from the
  local edition record). Provenance shading: auto light yellow, hand-typed
  light green; green fields constrain and are never overwritten.
- Generalized panes: top pane dropdown = CHECKED BOOKS (full logic) or WHL
  CATALOG (full columns incl. subtitle/description; in-place corrections
  stored in `output/whl_corrections.json`, never in the CSV). WHL modes via
  Ctrl+E: EDIT (cell edits; Ctrl+click opens the full-record WHL EDIT tab in
  the resizable left panel) and SEARCH (title click -> OL query; result
  click repopulates the row). Bottom pane = tabbed viewer (+ adds tabs;
  per-tab table dropdown: OL / CH / WHL), all filtered live from the FIND
  box; row hover shows an all-fields tooltip; row click adds the record to
  the top-pane table with column mapping.
- FIND syntax: `@token` = author (last name), `#token` = publication year,
  plain text = title words.
- WHL metadata scrape: `tools/whl_scrape.py` pages through the site's open
  WordPress REST API (whl_catalog post type, ACF fields + embedded taxonomy
  terms) and stores publisher / print length / subtitle / description /
  language / subject per published book in `output/whl_scraped.json`, keyed
  by permalink slug. Drafts are not exposed by the API and stay empty. The
  explorer merges: CSV < scraped < corrections; SCRAPE WHL button runs it
  as a background job with progress.
- Undo/Redo (Ctrl+Z / Ctrl+Y + titlebar buttons; 100-step session history):
  inverse operations for client state (snapshot restore of the checked map)
  and server-backed changes (manual entry create/delete/edit via a restore
  endpoint; WHL corrections restore the prior correction or clear back to
  the CSV; verifications/manual sources revert). WHL SEARCH mode has
  CONSTRAIN checkboxes (TITLE= verbatim phrase match via FTS,
  AUTHOR, YEAR) applied to the row-click Open Library lookup. COPYRIGHT tag semantics: NO = public domain,
  YES = under copyright. All tags are one uniform width with verification
  markers fused inside the tag. Themes are full chrome redesigns with
  preserved geometry, chosen from a SETTINGS dropdown: CLASSIC CAD, ARCHIVE
  LEDGER (card-catalog paper), WORKSTATION 2000 (thin-bevel listview era),
  SLATE STUDIO (mid-2000s steel/pill styling), PLATINUM (pinstriped late-90s
  Mac), BLUEPRINT (blue graph-paper drafting), MAINFRAME TERMINAL (green
  phosphor); legacy theme ids migrate. A FONT dropdown swaps the UI typeface
  (monospace, proportional, and serif options).
- The project is a git repository; dumps, built indexes, API caches and
  downloaded PDFs are gitignored. MARK column: `SCAN` = not in
  WHL + public domain + no surviving scan online; `UPLD` = not in WHL + scan
  exists (amber unverified, green once a source is approved). The UPLOAD
  LIST tab lists every approved source with title/subtitle/author/publisher/
  year + matched record and downloads as `whl_upload_list.json`. WHL/IA/HT
  badges show the matched record in a hover tooltip, are padded to one fixed
  width, and link to the record (Ctrl+click on verifiable tags); table cells
  never wrap (overflow hidden + full-text tooltip). Titlebar reads
  `<ACTIVE TAB> :: CATALOG EXPLORER`.
- Explorer extras: SETTINGS window for per-table column visibility
  (persisted); the checked tab has its own search box plus a SHOW CH CATALOG
  split pane (search filters both, `+ADD` pulls rows into the checked list);
  `DOWNLOAD APPROVED` fetches the IA PDF for every APPROVED book into
  `downloads/ia/` with cataloging entries in `downloads/ia/catalog.json`.
- Checked-books cells are click-to-edit (Enter/blur commits, Escape cancels;
  manual entries persist via PATCH and re-run the offline checks). Checks +
  scans run automatically through a serialized queue when a row is added or
  edited — there are no per-row SCANS/DL buttons. Tags are abbreviated to
  <=4 chars (YES / NO / ? / DRFT / VIEW / ERR / ---), uniform width, with
  full details in the hover tooltip; APPROVE/APPROVED remain as the
  clickable mark tag.
- v2.0 legacy trim + submission builder: the Catalog tab, dataset switching,
  the live CHECK SELECTED ON WHL action, and all dictated-entry logic are
  removed — two tabs remain (CHECKED BOOKS, UPLOAD LIST; titles debracketed,
  never wrapped) and per-tab instruction blurbs are gone. SCRAPE WHL lives
  in the main toolbar. Table views hide their scrollbars. In the WHL table,
  corrected rows (cyan), added rows (green), and drafts (amber) are
  highlighted with a left bar + tint. The UPLOAD LIST tab hosts the book
  builder (`output/whl_builds.json`, CRUD with undo): NEW ENTRY starts a
  blank record; BUILD on an approved source prefills metadata, provenance
  URL, and PDF source (local `downloads/ia/<id>.pdf` when already
  downloaded, else the archive URL); each entry has a FIELDS tab, a
  DESCRIPTION (MARKDOWN) tab with live preview, and a READY FOR SUBMISSION
  flag; EXPORT BUILDS emits `whl_submission_entries.json`. The WHL EDIT
  description field opens the same Markdown editor via a pencil button.
- v2.1 toolbar + live editing + PDF sources: an application toolbar under
  the titlebar carries UNDO/REDO, SETTINGS, and the active tab's commands
  (checked: RUN SCANS / SCRAPE WHL / DOWNLOAD APPROVED / EXPORT JSON /
  CLEAR CHECKED / SEARCH PANE toggle; upload: NEW ENTRY / EXPORT BUILDS /
  DOWNLOAD SOURCES). The WHL mode button drops its Ctrl+E hint; the mode
  shows as a footer tag. STATUS-column links show their URL in the
  tooltip. Background gridlines are removed from every theme, and ARCHIVE
  LEDGER is desaturated to neutral archival paper. The builder merges the
  description into the ENTRY tab as a live Obsidian-style Markdown editor
  (rendered in the box it is typed in; caret line shows source) to the
  right of the fields, with SAVE/READY/DELETE at the top, a taller notes
  field, and a new SOURCE (PDF) tab: embedded PDF viewer (streams local
  files via /api/pdf), BROWSE local-directory picker (/api/pdf/browse),
  ATTACH with existence validation, and auto-attachment of already
  downloaded IA PDFs (`pdf_file` build field). The approved-sources pane
  is resizable via a splitter. createMdEditor / createPdfViewer /
  openFileBrowser are reusable components for future integration points.
- v2.2 menu bar + icons + generalized editor: tabs renamed CATALOGS and
  EDITOR; a FILE/EDIT/VIEW/TOOLS menu bar carries the common functions;
  undo/redo are toolbar icons; SETTINGS is a gear opening a categorized
  window (GENERAL / APPEARANCE / TABLE VIEW / FILE PATHS sidebar) with
  separate interface (--ui, sans by default) and data (--mono) font
  dropdowns and two modern neutral themes (MODERN LIGHT, GRAPHITE DARK).
  The find label is a magnifier icon ("[title] @author #year"). EXPORT
  (filter-aware) and the download-verified icon sit above the top table
  with a filter icon (MARK / SOURCE / DOWNLOAD-status popup) and per-table
  column-visibility icons; CLEAR CHECKED is removed; all grid columns are
  drag-resizable and persisted. IA download state renders as a black `*`
  (saved; tooltip = path) or red `**` (failed; tooltip = error) beside the
  still-centered tag. Action buttons are icons. The EDIT tab (renamed from
  WHL EDIT, save button = SAVE, PRINT LENGTH label) opens via Ctrl+click
  from every table — WHL rows in both modes, checked/manual rows (book
  field set; manual entries hide ACQUIRED), and CH-catalog rows (SAVE
  checks the record with the edits). Builder: save/delete/browse/attach/
  build are icons, READY FOR SUBMISSION is now VERIFIED, and the PDF
  viewer is undecorated (#toolbar=0) with the file size displayed.
  Tooltips abbreviate long notes/description values.
- v2.3 conventional utility chrome: the action icons move inline with the
  tab strip (undo/redo, EDITOR commands, settings gear); RUN SCANS /
  SCRAPE WHL / SEARCH PANE are menu-only. Column-header clicks sort the
  checked and WHL tables (arrow indicator); the max displayed rows is a
  setting; one shared font list feeds both the interface and data font
  dropdowns. Theme set rebuilt: CLASSIC CAD modernized (flat 1px, radius,
  dark canvas kept), ARCHIVE LEDGER neutralized, PLATINUM kept, BLUEPRINT
  now warm paper over a warm neutral-dark board, MODERN LIGHT contrast
  raised, plus new MODERN DARK and STONE; Workstation 2000 / Slate Studio /
  Mainframe Terminal / Graphite Dark removed with id migration. Open
  Library fills title-case titles/subtitles and flip "Last, First"
  authors; Ctrl/Shift+click on OL column headers mark them green
  (copy to the selected WHL row) or red (exclude) for repopulation.
  Alt over an edited WHL row (or in the EDIT panel) shows the original
  record grayed/yellow (server now ships pre-correction values in
  row.orig). PUB tags with a publication file open a PDF viewer window
  (optional parallel OCR pane via settings; /api/pdf/text extracts the
  text layer with pypdf, fetching remote PDFs into downloads/cache).
  Download state became a green/red dot inside the still-centered tag;
  thead z-index fixed (tags no longer paint over headers); deletes skip
  confirmation (undoable). EDITOR tab: PENDING list, save+delete adjacent,
  VERIFIED icon toggle with tag, VERIFIED SOURCES with an N ROWS count,
  PATH TO PDF label, browse/attach icons, instructional text stripped
  throughout, taller description/notes fields.
