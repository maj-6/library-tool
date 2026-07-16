# Library Tool

A cataloguing workbench that turns a shelf of old botanical and medical books
into a public online library. For each book it answers three questions — is it
already in the [World Herb Library](https://worldherblibrary.org)? is it out
of copyright? does a scan already exist in another archive? — and then helps
prepare the worthwhile ones for publication: catalogue metadata, a written
description, and a readable scan.

**[Website](https://maj-6.github.io/library-tool/) ·
[Downloads](https://maj-6.github.io/library-tool/downloads.html) ·
[Documentation](https://maj-6.github.io/library-tool/docs.html) ·
[Release notes](https://maj-6.github.io/library-tool/releases.html)**

![The Catalogs tab: a table of checked books, with archive and copyright
verdicts for the selected book in the Info panel](website/assets/docs/app-catalogs.png)

## The parts

| Part | Where | What it is |
| --- | --- | --- |
| Desktop app | `desktop/` + `tools/whl_explorer/` | The workbench: an Electron shell around a local Flask app. Windows installer with auto-update. |
| Book Capture | `android/BookCapture/` | Android companion: voice-driven camera capture with on-the-fly OCR; captures sync to the desktop. |
| Website | `website/` | This repo's public face on GitHub Pages: downloads, docs, and the **Archive Browser** — the published catalogue with a built-in reader (vendored pdf.js, no build step, no CDN). |
| Cloud | `docs/cloud/` | Supabase schema + object storage behind accounts, capture sync, and the published catalogue. The site falls back to local fixtures without it. |

## The workflow

1. **Check** — each book (from the library spreadsheet, hand-entered, or
   captured by phone) is checked offline: WHL presence, U.S. copyright
   registration/renewal records, and existing scans in the Internet Archive
   or HathiTrust.
2. **Verify** — every automatic match is only a suggestion; each is approved
   or rejected by hand, and a rejected match can be replaced with a manually
   located source.
3. **Decide** — books end up marked `SCAN` (public domain, no scan exists:
   worth scanning) or `UPLD` (a scan exists elsewhere and can be re-homed).
4. **Prepare** — verified sources become draft catalogue entries in the
   Editor: metadata, a Markdown description, and the PDF. The Analyze tab
   extracts and corrects the text and (with AI keys) drafts summaries,
   categories, translations, and annotations.
5. **Publish** — finished entries go to the online catalogue, readable by
   anyone in the Archive Browser.

Checks run against local copies and indexes (the WHL catalogue export, a
copyright-renewals CSV, and a ~7.7M-edition Open Library index in SQLite),
so day-to-day work is offline; the network is used for scan search,
downloads, sync, and publishing.

## Running from source

```
python3 -m pip install -e ".[dev]"      # runtime deps + pytest + ruff
python3 tools/whl_explorer/server.py    # the app: http://127.0.0.1:5001
```

The desktop shell wraps the same server: `cd desktop && npm install && npm
start`. The website is static: `python3 website/serve.py 8080`. Book Capture
builds from `android/BookCapture/` with Gradle or Android Studio.

Before pushing: `scripts/check.ps1` (`check.sh` on POSIX) runs ruff + pytest.
Tests use a throwaway `WHL_DATA_ROOT` and never touch live state.

See [tools/README.md](tools/README.md) for the full tool documentation —
index builders, every explorer feature, data layout, and the standalone CLIs
— and [docs/](docs/) for design notes and the cloud setup.

## Data

The packaged app keeps its state (document store, entries, downloads,
indexes) in `%APPDATA%\Library Tool`; a source checkout uses the repo root.
`WHL_DATA_ROOT` overrides either. Shipped read-only assets (the catalogue
exports) live alongside the code.

## Releases

The project is pre-1.0 and ships intermediate builds deliberately: stable
releases plus `alpha`/`beta` prerelease channels, both built by CI from `v*`
tags and published to GitHub Releases and the website's Downloads page.
Mechanics and standards are in [docs/releasing.md](docs/releasing.md).
