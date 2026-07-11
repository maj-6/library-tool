# The Library Tool website

Two designs under one roof, sharing a data layer but not a stylesheet:

- **The marketing pages** — `index.html` (About), `docs.html`, `downloads.html`,
  `404.html` — use `assets/site.css`: a calm, prose-width, sidebar layout.
- **The World Herb Library** — `browse.html`, `book.html`, `read.html` — use
  `assets/library.css`: a denser, application-like archival catalogue in the
  manner of archive.org collections and the HathiTrust catalogue. It has its own
  masthead ("World Herb Library", with a quiet link back to the About page) and
  is light-only. The two stylesheets are deliberately independent — the
  `@font-face` blocks and the Archive Ledger palette are duplicated into
  `library.css` so the catalogue pages stand on their own.

No build step, no framework, no CDN — `fetch` against PostgREST is the whole
data layer (`assets/data.js`), and it speaks the same HTTP as
`tools/supabase_sync.py`. Roboto Slab is served from `assets/fonts/`, subset to
latin and latin-ext, for the same reason.

```
python3 -m http.server 8080 --directory website
```

To exercise the **reader** locally, serve with the bundled helper instead:

```
python3 website/serve.py 8080
```

The reader imports pdf.js as an ES module, and browsers refuse to run a module
script served with a non-JavaScript MIME type. On some platforms (Windows in
particular) the stock `http.server` returns `.mjs` as `text/plain`, so pdf.js
fails to load; `serve.py` is the same server with the correct MIME forced.
GitHub Pages, the production host, already serves `.mjs` as `text/javascript`,
so this only matters for local development.

Without `assets/config.js` the browser reads the `fixtures/` folder, so the site
works before the cloud holds anything. Regenerate the volumes fixture from the
local builds with:

```
python3 tools/cloud_setup.py fixture
```

## The library pages

- **`browse.html`** — the faceted catalogue. A search box and sort in the
  toolbar; a left facet rail with a Categories tree (counts, click to filter a
  whole subtree), a Year range, and Languages (counts); catalogue records
  linking to their item pages. Every view deep-links: the query, category,
  language, year range, sort, and page all live in the URL
  (`browse.html?q=…&cat=…&lang=…&from=…&to=…&sort=…`), so the back button and
  shared links work without a router.
- **`book.html?slug=…`** — the item record. A title block, the rendered About
  article (Markdown from `volume_texts`, via the escaping-first renderer in
  `assets/markdown.js`), and an annotations preview; a formal metadata table and
  Read / Download actions in the side column, with availability affordances
  driven by `volumes.assets`. An unknown slug renders an in-page not-found
  state (the HTTP status is still 200).
- **`read.html?slug=…`** — the reader (see below).

## The reader and vendored pdf.js

`read.html` streams the PDF with **pdf.js**, vendored under
`assets/vendor/pdfjs/` to keep the no-CDN promise. Only three files are
committed: `build/pdf.min.mjs`, `build/pdf.worker.min.mjs`, and `LICENSE`
(Apache-2.0), taken from the official `pdfjs-dist` distribution.

- **Version:** pdf.js **5.7.284**. To update, download the matching
  `pdfjs-dist` release, replace those two `.mjs` files and the `LICENSE`, and
  bump this note. Nothing imports pdf.js except `assets/read.js`, which loads it
  as an ES module and points `GlobalWorkerOptions.workerSrc` at the worker via
  `new URL(…, import.meta.url)`.
- **Streaming:** `getDocument({ disableAutoFetch: true })` uses HTTP Range
  requests, so a large scan loads lazily as you scroll (the host must allow
  Range and expose it via CORS — Supabase Storage and R2 both do).
- **Virtualized scroll:** only pages near the viewport hold a canvas
  (IntersectionObserver); the rest keep a pre-sized placeholder so the scrollbar
  never jumps. Thumbnails, zoom / fit-width / fit-page, keyboard nav
  (←/→, PgUp/PgDn, +/−, `t`), remembered position + zoom per slug
  (`localStorage["whl_reader_<slug>"]`), page-anchored margin annotations from
  `volume_notes`, and a page-aligned text / translation panel from
  `volume_pages`.

## Fixtures (offline development)

`fixtures/volumes.json` carries `category_paths` and `assets` on every entry.
Alongside it:

- `fixtures/texts.json` — `{ "<slug>": { "about": "<markdown>" } }`
- `fixtures/notes.json` — `{ "<slug>": [ { note_id, page, quote, kind, body } ] }`
- `fixtures/pages.json` — `{ "<slug>": { "": { "1": "…" }, "es": { … } } }`
- `fixtures/sample.pdf` — a small public-domain-style sample scan. In fixture
  mode any volume whose assets declare a text layer (`assets.pages`) is served
  this file, so the reader is fully exercisable offline. The
  `flora-rustica-1792` entry has the richest fixtures (About, ten pages of text,
  a Spanish translation, and annotations).

## Pointing it at the cloud

```
python3 tools/cloud_setup.py anon-key      # prints the snippet
```

Write it to `assets/config.js` — gitignored, because the project reference is
yours. The **anon** key belongs here, never the service_role key. Row-level
security is what protects the project: `docs/cloud/schema.sql` grants anon
exactly two reads, `volumes` and `releases`, and nothing else.

## Publishing

Any static host. The site is plain files; `browse.html?q=…&year=…` keeps its
query in the URL, so deep links and the back button work without a router.

Two things to decide before uploading volumes:

- **Storage.** The `volumes` bucket is public — that is the point of a public
  library. Supabase's free tier gives 1 GB, and the local collection is 62 PDFs
  of which one is 129 MB. Volumes will need Supabase Pro, or an R2/B2 bucket.
  The schema anticipates this: a volume carries `pdf_path` (the Supabase bucket)
  *or* `pdf_url` (anywhere), and readers prefer `pdf_url`. Moving storage later
  is a column update, not a migration.
- **Copyright.** Only publish what is public domain. The desktop's copyright tag
  exists to answer that question, and its Info panel shows the registration and
  renewal records behind the verdict.
