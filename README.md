# World Herb Library — cataloging workbench

A personal, local-only workbench for reconciling a private herbal library
against the [World Herb Library](https://worldherblibrary.org) (WHL) and
preparing new catalog entries for submission there.

## Scope and intent

Everything in this repository serves one workflow:

1. **Check** — take a book (from the CH private-library spreadsheet, or
   hand-entered from its title page) and determine, offline:
   is it already in the WHL catalog? is it under copyright? does a scan
   already exist in the Internet Archive or HathiTrust?
2. **Verify** — every automatic match can be a false positive, so each one
   is approved or rejected by hand; rejected matches can be replaced with a
   manually located source URL.
3. **Decide** — each book ends up marked `SCAN` (public domain, no scan
   exists anywhere: the physical book should be scanned) or `UPLOAD` (a
   scan exists in another archive and can be re-homed to WHL), or needs
   nothing.
4. **Prepare** — approved sources feed the *upload list*, where the book
   builder assembles finished WHL catalog entries (full metadata, a
   Markdown description, and a PDF source — an archive URL or a locally
   downloaded file) ready for submission.

Along the way the WHL catalog itself can be cleaned up: the explorer shows
the whole WHL catalog as an editable table, and corrections are kept in a
local overlay file (`output/whl_corrections.json`) — the exported CSV is
never modified.

Everything is designed to run **offline** against local copies and indexes:
the WHL catalog CSV (plus metadata scraped once from the website API), a
copyright-renewals CSV, and a consolidated Open Library editions index
(~7.7M pre-1950 editions, local SQLite FTS). Only the scan search (Internet
Archive / HathiTrust), the WHL metadata scrape, and IA PDF downloads touch
the network.

## The application

The single application is the **catalog explorer** — a Flask web app with a
classic-CAD styled UI (seven selectable themes):

```
python3 tools/whl_explorer/server.py    # then open http://127.0.0.1:5001
```

See [tools/README.md](tools/README.md) for the full tool documentation:
building the Open Library indexes, every explorer feature, and the
standalone CLI tools.

## Layout

| Path | What it is |
| --- | --- |
| `ch_library.xlsx` | the CH private-library spreadsheet (source of truth) |
| `whl_catalog.csv` | exported WHL catalog (read-only; corrections overlay it) |
| `copyright_renewals.csv` | US copyright-renewal records for the offline check |
| `tools/` | all code: the explorer, index builders, checkers, CLIs |
| `output/` | everything generated: converted catalogs, indexes, manual entries, corrections, builds |
| `downloads/ia/` | Internet Archive PDFs downloaded for approved books (+ `catalog.json`) |

Key generated files in `output/`:

- `ch_library.json` — the spreadsheet converted to JSON (`convert_xlsx.py`)
- `manual_entries.json` — hand-entered books with their check/scan results
- `whl_scraped.json` — full metadata for every published WHL book (website API)
- `whl_corrections.json` — local edits/additions to the WHL catalog
- `whl_builds.json` — catalog entries being prepared for WHL submission
- `ol_search.db` / `ol_works.db` — local Open Library indexes
