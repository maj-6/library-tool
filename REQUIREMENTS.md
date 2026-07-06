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
    (currently the dictated books / partial set until scanning is wired up).
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
- `output/books_metadata.json`, `output/library_db.json` — the dictated /
  reviewed local books; stand in for the local library until scanning + the
  `local_library_partial` source are available.
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
  robots-disallowed for programs). Wired to `SCANS` buttons on catalog rows
  and the combined table, plus a `RUN SCANS` batch.
- Per-source verification: each positive WHL/IA/HT match carries a marker on
  the tag's right edge (shaded fill + 1px border; yellow pending, green
  approved, red rejected as a false positive). Clicking the MARKER cycles the
  state (the tag stays a link); a rejected match renders as `NO` and is
  excluded from classification, so a false-positive IA hit falls back to
  `SCAN` when appropriate. Clicking a rejected tag opens a paste box for the
  URL of a manually located source, which then acts as the verified record
  (link, upload list, IA download). Roman-numeral years typed into the
  manual-entry year field show their Arabic value in the footer.
- Open Library integration: `tools/build_ol_index.py` converts the
  `ol_dump_works_*.txt.gz` dump (~30M works) into `output/ol_works.db`
  (SQLite + contentless FTS5 on title/subtitle, work_authors index). The
  manual pane is split into SEARCH (constrained search: title/author via the
  local index + cached OL author lookup; publisher/city/year/edition/volume
  verified against live-fetched editions) and MANUAL ENTRY (title
  autocomplete constrained by hand-typed fields). Provenance shading:
  auto-populated fields light yellow, hand-typed light green; green fields
  constrain and are never overwritten. Author names + edition details come
  from the OL API, cached in `output/.ol_api_cache.json`. MARK column: `SCAN` = not in
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
