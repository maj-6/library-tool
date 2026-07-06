# World Herb Library tools

Turns dictated, timestamped transcripts plus dated photos into a reviewable
book catalogue, converts the legacy Excel catalogue to JSON, and provides a
local web app to review and finalize each book's metadata.

## Layout

- `libcommon.py` - shared helpers (paths, ids, EXIF, transcript parsing,
  metadata extraction, JSON IO).
- `build_books.py` - parses transcripts, builds `books/<id>/` folders, matches
  photos by capture time, writes the two JSON lists.
- `convert_xlsx.py` - converts `ch_library.xlsx` to `output/ch_library.json`.
- `webapp/` - Flask review app (`server.py`, `templates/`, `static/`).
- `catalog_checks.py` - offline copyright + WHL-catalogue checks (loaders,
  indexes, and the shared cross-database identity test).
- `scan_search.py` - Internet Archive + HathiTrust scan search + JSON CLI.
- `build_ol_index.py` - converts the Open Library works dump into
  `output/ol_works.db` (fallback index; also feeds author keys to the build
  below).
- `build_ol_search.py` - consolidates the editions + authors + works dumps
  into `output/ol_search.db`: only editions published up to --max-year
  (default 1950), with author names, publisher, place, year, edition and
  volume all local and FTS5-indexed (prefix indexes for search-as-you-type).
- `ol_client.py` - constrained search over those indexes; the consolidated
  editions index needs no Open Library API calls at all.
- `whl_client.py` - World Herb Library search client + JSON CLI.
- `whl_explorer/` - CAD-styled catalog explorer with WHL cross-reference,
  manual entry, and scan search.

## Setup

Use the `python3` interpreter (Python 3.13; it has `tkinter` and `pip`).

```
python3 -m pip install --user -r tools/requirements.txt
```

## 1. Build book folders and lists

```
python3 tools/build_books.py --force
```

- Reads `transcript/*.txt`. The filename `URecorder_YYYYMMDD_HHMMSS` gives the
  recording start; in-file `(M:SS - M:SS)` markers are offsets from it.
- Books are delimited by `Book.` ... `End book.`. Empty fumbles
  (`Book. End book.`) are dropped; duplicate transcripts and book-less
  recordings are skipped.
- A photo is assigned to a book when its EXIF capture time falls inside the
  book's absolute window. Use `--pad SECONDS` to widen each window on both
  sides (default 0 = strict, as specified).

Outputs:

- `books/<id>/transcript.txt` and `books/<id>/1.jpg, 2.jpg, ...`
- `output/books_index.json` (list 1: folder index, title page, metadata ref)
- `output/books_metadata.json` (list 2: per-book metadata fields)

Note: re-running assigns new random ids, so finalized entries in
`output/library_db.json` are keyed to the previous build. Re-review after a
rebuild, or keep the build stable once review has started.

## 2. Convert the Excel catalogue

```
python3 tools/convert_xlsx.py
```

Writes `output/ch_library.json` (one object per row, readable keys, ISO dates).

## 3. Review metadata in the web app

```
python3 tools/webapp/server.py
```

Open http://127.0.0.1:5000

- Left: sidebar list of books (shows image count, or `done` once submitted).
- Center: title-page image with prev/next and "Set as title page", plus the
  transcript region and its time window.
- Right: editable metadata fields and a Submit button.

Submitting upserts the finalized entry (metadata + chosen title page) into
`output/library_db.json`.
## 4. Cross-reference against World Herb Library (WHL-CAD explorer)
The explorer displays a library JSON, offers live title/author autocomplete,
and checks each book against worldherblibrary.org.
```
python3 tools/whl_explorer/server.py
```
Open http://127.0.0.1:5001 (separate port from the review app).
- Classic-CAD styled UI; the title bar reads `<ACTIVE TAB> :: CATALOG
  EXPLORER`. Table cells never wrap: overflowing text is ellipsized and the
  full text appears in a hover tooltip. Status tags are fixed-width, and a
  tag that matched a record is itself the link to that record (no separate
  OPEN links; on verifiable tags use Ctrl+click to open).
- `SETTINGS` (title bar): choose which columns are visible in the checked
  table and the catalog table; choices persist in the browser.
- `Catalog` tab: pick a dataset (the converted `ch_library.json` catalogue by
  default, the dictated books / reviewed library, or the manual entries) and
  type to filter. The autocomplete dropdown is an abbreviated table
  (title/author/year/publisher/categories); clicking a result adds it
  straight to Checked Books (the list stays open for adding several). Per
  row, `FIND` looks up the closest match on WHL and `SCANS` searches the
  Internet Archive and HathiTrust. Rows render up to 500 at a time.
- `Checked Books / Manual Entry` tab: a split pane. The left pane is the
  manual entry form — title, author, publisher, city, year, edition, volume
  number, language, pages, condition, price, illustrations, categories, notes
  (title required); entries are saved to `output/manual_entries.json`. Typing
  a Roman-numeral date into the year field (common on old title pages) shows
  the Arabic year live in the footer's right corner. The pane has two
  sub-tabs — `SEARCH` (constrained Open Library search, below) and
  `MANUAL ENTRY` (the form). The
  right pane is one combined table of manual entries and checked catalog
  books (`SRC` column) with `DEL` (manual) / `UNCHK` (catalog) actions.
  Metadata cells are edited in place: click a cell, type, Enter/blur commits
  (Escape cancels); manual-entry edits persist server-side. The toolbar has
  its own `FIND` search box (filters the table live), a `MARK` filter (ALL /
  SCAN / UPLOAD / APPROVED / UNMARKED), and a `SHOW CH CATALOG` checkbox that
  opens the CH catalogue in a split pane underneath — the same search filters
  it, and `+ADD` moves a row into the checked list. Batch actions: `CHECK
  SELECTED ON WHL` (live site lookups), `RUN SCANS` (queues rows that have
  no scan results yet), `DOWNLOAD APPROVED` (IA PDFs for every approved
  book), `EXPORT JSON`, `CLEAR CHECKED`. Checked state persists in the
  browser; manual entries persist server-side.

### Open Library search + autocomplete

Build the consolidated index once (streams the ~62 GB editions dump plus the
authors dump; the works index from `build_ol_index.py` fills in author keys):

```
python3 tools/build_ol_index.py     # once, works index (fallback + author keys)
python3 tools/build_ol_search.py    # consolidated editions index (~10-15 min)
```

The result is `output/ol_search.db` (~4.6 GB, ~7.7M editions published up to
1950) where title, author, publisher and place are one prefix-indexed FTS
table and year/volume are SQL columns — every query is local and answers in
milliseconds, including search-as-you-type.

- The `SEARCH` sub-tab's fields (title / author / publisher / city / year /
  edition / volume number) act as live constraints; results appear in the
  bottom pane's OPEN LIBRARY table as you type, in the FIND box or the form.
- `MANUAL ENTRY` autocomplete: from 3 characters the title field suggests
  editions, constrained by hand-typed fields. Picking one fills author,
  publisher, city, year, edition, and volume directly from the local record.
  Auto-filled fields are shaded light yellow, hand-typed fields light green;
  green fields constrain the search and are never overwritten (except the
  title itself, which the pick completes).

While `ol_search.db` is absent the app falls back to the works index + live
OL API (cached in `output/.ol_api_cache.json`).

### Generalized top/bottom tables

The right side of the checked tab is two panes:

- **Top pane** (dropdown): the working table with dedicated logic —
  `CHECKED BOOKS + MANUAL` (checks, scans, marks, verification, editing) or
  `WHL CATALOG (EDITABLE)` with the full column set (title, subtitle,
  authors, year, categories, description, status; subtitle/description start
  empty — the export lacks them — and are filled via corrections).
  Corrections never touch `whl_catalog.csv`; they live in
  `output/whl_corrections.json` (corrected/added rows are shaded and tagged).
  The WHL view has two modes, toggled with **Ctrl+E**: in EDIT mode click a
  cell to correct it, or Ctrl+click a row to load the whole record into the
  left panel's `WHL EDIT` tab (the comfortable place for descriptions); in
  SEARCH mode click a title to look it up on Open Library, then click a
  result to repopulate the row's metadata — the cleanup workflow for
  incomplete or mis-entered entries.
- The FIND box understands `@token` (author, last name), `#token`
  (publication year), and plain text (title words); the syntax drives the
  top-table filters, the bottom tabs, and the realtime Open Library query.
- **Undo/Redo** (titlebar buttons, Ctrl+Z / Ctrl+Y outside text fields)
  covers checking/unchecking and clearing books, cell edits, verification
  markers and manual sources, manual-entry creation/deletion/edits, and WHL
  corrections — undoing a WHL edit restores the previous correction or
  clears back to the CSV value. The last 100 actions are kept per session.
- In WHL SEARCH mode, `CONSTRAIN:` checkboxes choose which of the clicked
  row's columns narrow the Open Library lookup — `TITLE=` requires the
  title to appear verbatim (as a phrase), AUTHOR and YEAR filter by the
  row's values.
  The left panel is resizable by dragging the splitter. SETTINGS offers
  themes (Classic 95/CAD, CDE/Solaris, AutoCAD dark, XP/Office 2003) that
  restyle the palette without changing sizes. Note the COPYRIGHT tag reads
  `NO` for public-domain works and `YES` for works under copyright.
- **Bottom pane** (`SHOW SEARCH PANE`): a tabbed general-purpose viewer.
  `+` adds a tab; the active tab's dropdown selects its table (OPEN LIBRARY /
  CH CATALOG / WHL CATALOG). All tabs filter live from the FIND box (the
  Open Library tab queries the consolidated index server-side). Hovering a
  row shows a tooltip with every available field; clicking a row generates
  an entry in whatever table the top pane shows, with columns mapped (into
  CHECKED it becomes a manual entry — auto-checked and auto-scanned — or a
  checked catalog row for CH sources; into WHL it becomes an added
  correction row).

### Automatic checks + scans

There is no per-row scan button: rows are checked and scanned automatically.
Adding a book (manual submit, catalog checkbox, `FIND`, autocomplete `+ADD`,
CH-pane `+ADD`) queues it; editing any cell re-queues it (stale results are
cleared to `---` until the rescan lands). The queue runs one book at a time
so a burst of adds doesn't hammer the archives; progress shows in the status
bar. `RUN SCANS` queues everything that is still unscanned (e.g. rows from
before this feature).

### Tags

Tags are fixed-width and abbreviated; hover for the full detail and click to
open the matched record:

- `COPYRIGHT`: `YES` public domain / `NO` in copyright / `?` unknown.
- `WHL`, `IA`: `YES` found / `NO` not found / `?` undetermined / `DRFT`
  draft-only (WHL) / `ERR` lookup error.
- `HT`: as above, but `VIEW` when a full-view scan exists.
- `---` means not checked yet.
- Hovering a `NO` scan tag shows the closest result that stayed below the
  acceptance threshold (title, author, year, accuracy), so near-misses can
  be judged by eye.

### Internet Archive PDF downloads

`DOWNLOAD APPROVED` downloads the best IA match for every book whose mark is
`APPROVED`: the item's PDF derivative is saved to
`downloads/ia/<identifier>.pdf` and a cataloging entry is written to
`downloads/ia/catalog.json` combining the IA record (title, creators, date,
source URL, file) with the book's own catalogue metadata. Progress shows in
the IA tag cell and the status bar; already-downloaded volumes show `SAVED`
and are skipped on later runs.

### Submission checks (offline, local databases)

Every submitted manual entry is checked automatically and the results are
stored on the entry (`checks`) and shown as badges (`RUN SCANS` computes the
same checks for checked catalog books):

- `COPYRIGHT` via `copyright_renewals.csv`: public domain by age; renewal
  lookup for 1931-1963 (reports the renewal id); auto-renewed 1964-1977; in
  copyright from 1978.
- `WHL` via the local catalogue copy `whl_catalog.csv`: `IN WHL` with a link
  to the matched catalogue page, `DRAFT`, or `NOT FOUND`. The WHL website is
  never queried for this check (only `FIND` / `CHECK SELECTED ON WHL` are
  live). Hovering the WHL / IA / HT badges shows the matched record (title,
  author, year, accuracy, items).

The matching logic lives in `catalog_checks.py` (shared with the report
tool); the indexes load once at server start (~3 s).

### Match verification (false positives) and SCAN / UPLOAD marks

Every positive catalog match (`WHL` / `IA` / `HT` tag) carries a small marker
attached to the tag's right edge — shaded fill with a 1px border, matching
the tag: **yellow** = pending verification, **green** = approved (verified
accurate), **red** = rejected as a false positive. Clicking the **marker**
cycles pending → approved → rejected → pending; the tag itself stays a plain
link to the matched record. A rejected match renders as `NO` and stops
counting as found — so a book whose only IA hit was a false positive
correctly falls back to `SCAN`. Clicking a rejected tag opens a box to paste
the URL of a manually located source: once saved, the tag reads `YES` again
and links to that URL, the marker turns green, and the source (with an IA
identifier when the URL is an archive.org record) feeds the upload list and
`DOWNLOAD APPROVED`. Leaving the rejected state clears the pasted URL.
Verification persists (server-side for manual entries, browser-side for
catalog rows) and resets when the row's metadata is edited.

The `MARK` column classifies each book from the verified picture:

- `SCAN` — not in WHL, not under copyright, and no (surviving) scan found in
  the online archives: the physical book should be scanned.
- `UPLD` — not in WHL but a scan exists in another online archive; amber
  while its sources are unverified, green once at least one source is
  approved.
- otherwise no mark; the tooltip on the dash explains why.

The `MARK` filter narrows the table (APPROVED = upload rows with an approved
source).

### Upload list

The `UPLOAD LIST` tab collects every approved source across all rows — one
line per approved archive record with the book's title, subtitle (when
present), author, publisher, publication year, the archive, and the matched
record (linked). `DOWNLOAD LIST (JSON)` saves it as `whl_upload_list.json`
for the actual upload work.

### Scan search (Internet Archive + HathiTrust)

`SCANS` / `RUN SCANS` look for existing scans:

- Internet Archive: public advancedsearch API, quoted-title and
  surname-filtered queries, results ranked by the composite accuracy.
- HathiTrust: its catalog search is closed to programs (robots.txt), so the
  official Bib API is used instead — OCLC numbers are discovered through the
  Open Library search API, then looked up in one Bib API call. Results carry
  the record link, per-volume item links, and a `FULL VIEW` flag. When no
  OCLC is found the result is `UNKNOWN` with a link to search by hand.

Results are persisted (manual entries server-side, catalog rows in the
browser) and shown as `IA` / `HT` badges; the per-row `SCANS` button also
opens a results window with every match linked. The same lookup is available
standalone:
```
python3 tools/scan_search.py --title "American Medicinal Plants" --author "Millspaugh" --year 1887
```
The WHL lookup uses the site's public search API and searches by both title
and author so OCR/format differences in one field can be recovered via the
other. Matching is case-insensitive and ranked by a composite `accuracy`:
- title: similarity over the first 16 characters (so appended subtitles and
  late OCR noise do not derail the match), weight 0.5
- author: similarity over the first 8 characters, weight 0.3
- publishing year: exact match, weight 0.2
Only components present on both sides are scored; the remaining weights are
renormalized (WHL often omits the publishing date for book-level hits). A book
is reported `AVAILABLE` when the best `accuracy` is >= 0.6 with a catalog page,
otherwise `NOT FOUND`.
### WHL client CLI
The same lookup is available standalone, printing JSON:
```
python3 tools/whl_client.py --title "An Introduction to Botany" --author "Lindley" --date 1835
```
The JSON includes `available` (true/false/null) and the `best_match` with its
`accuracy` plus the `title_score`, `author_score`, and `date_match` breakdown,
the matched title/author/`pub_date`, the WHL `score`, the catalog `wp_url`, and
`alternatives`.
## 5. CH Library status report
Builds `output/ch_library_report.xlsx`: every `ch_library.xlsx` row and column,
plus `In WHL`, `Available online`, `In local library`, and `Copyright status`.
```
python3 tools/build_catalog_report.py              # offline columns
python3 tools/build_catalog_report.py --online --limit 200
```
- `Copyright status` (offline, via `copyright_renewals.csv`): public domain by
  age (published <= currentyear-96); for 1931-1963 it looks the book up in the
  renewals set and reports `In copyright (renewal <ID>)` or `Public domain (no
  renewal found)`; 1964-1977 `In copyright (auto-renewed)`; >=1978 in copyright;
  missing year `Unknown`.
- `In WHL` (offline, via `whl_catalog.csv`): `yes` (a published catalogue entry
  matches), `draft` (only unpublished entries match), or `no`.
- `In local library` (offline): matched against the dictated/reviewed books
  (and `local_library_partial.json` when present).
- `Available online` (network, opt-in `--online`): Internet Archive search,
  cached to `output/.online_cache.json`; defaults to `not checked`. `--limit`
  caps lookups (0 = no cap).
Matching across all three uses a shared title-forward, surname-based test
(`title_author_match`): a strong first-16-char title prefix plus a full-title
ratio, confirmed by an order-agnostic surname-token overlap. It is
case-insensitive and tolerant of `Lastname, Initials` vs `Firstname Lastname`,
appended subtitles, OCR typos, and differing edition years.
