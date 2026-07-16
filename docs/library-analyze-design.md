# The library view, categories, and the Analyze tab

Design contract for three features that land together. Frozen 2026-07-10;
touched up 2026-07-15 to match the code as built (marked *as built* where
the shape changed). The schema half lives in `docs/cloud/schema.sql`
(taxonomy, volume_texts, volume_pages, volume_notes,
author_pages/author_index, volumes.category_paths + volumes.assets).

## 1. Categories: a hierarchical taxonomy

The comma-separated `categories` text fields (and the WHL rows' scraped
`subject`) are **deprecated**. They stay readable — the CH master list and the
WHL CSV are read-only sources — but nothing edits them any more.

**The vocabulary** is a tree of nodes in `DATA_ROOT/output/categories.json`:

```json
{ "version": 1,
  "nodes": { "<12-hex id>": { "name": "Herbals", "parent": "<id or ''>",
                              "created_at": "...", "updated_at": "..." } } }
```

Names are unique among siblings (case-insensitive); cycles are rejected.
Served by `/api/categories` (GET tree; POST node; PATCH rename/re-parent;
DELETE re-parents children to the deleted node's parent; POST merge moves
assignments and children then deletes). Synced across machines through the
`taxonomy` cloud table by `tools/store_sync.py`, exactly like builds.

**Assignments** are `category_ids` lists (of node ids) on the three record
types the user owns:

- builds — new `_BUILD_FIELDS` entry, list-typed (exempt from `str()`
  coercion, like `pdf_sources`)
- manual entries — new `MANUAL_ENTRY_FIELDS` entry, list-typed
- checked books — `book.category_ids` in the client-state blob (shape-agnostic
  through the books mirror)

**Migration** (`POST /api/categories/adopt`, with a `dry_run` preview): scans
builds + manual entries + checked books, splits their legacy `categories`
strings on commas, creates one root-level node per distinct label, assigns
`category_ids`, and leaves the legacy text untouched. The tree is then curated
by hand (re-parent, merge) in the taxonomy manager.

**UI**: a chip picker replaces the plain text inputs in the build editor, the
book editor, and the manual-entry form — chips with an autocomplete popover
over the taxonomy, each suggestion labelled with its full path ("Botany ›
Herbals"). The taxonomy manager is a window (Tools → Categories…) with the
tree, add/rename/re-parent/merge/delete, and the adopt-legacy action.

**Publish**: `category_ids` resolve to root→leaf name paths.
`volumes.category_paths` gets the array-of-paths JSON; the flat
`volumes.categories` text becomes the rendering of the same paths (" › "
within a path, ", " between) so fts search keeps working.

## 2. The Analyze tab

A new top-level tab, AI-driven, DeepSeek by default. It operates on **builds**
opened from the Editor, and only builds whose status is `ready` or `uploaded`
("verified"); drafts are hidden (the book list's verified-only filter, on by
default), not shown locked. *As built* it did not become an extra tab: it
merged with the former OCR tab into one top-level **Analyze** tab whose
workspace switches between **Document** (the OCR facsimile and page staging)
and **Analysis** (the AI panel described here), sharing one book list.

**Provider**: the existing Settings → AI section (base URL / model /
instructions, plus optional temperature/timeout settings overriding the
per-call defaults) plus the API key under Settings → Credentials is the
provider config. When base or model are blank the
app uses `https://api.deepseek.com` and `deepseek-chat` — so pasting a
DeepSeek key is the whole setup. All Analyze calls go server-side
(`_ai_chat()`, urllib, OpenAI-compatible `/chat/completions`, credentials
read via `_client_settings()` so background jobs survive the client closing).

**Artifacts** live in the entry folder (the established per-book bundle,
mirrored to R2 by store_sync):

```
output/entries/<build_id>/
  about.md               the About article (Markdown, hand-edited or AI-seeded)
  summary.md             working summary
  annotations.json       {"version":1,"notes":[{id,page,quote,kind,body,
                          status:"suggested|approved|rejected",source,
                          created_at,updated_at}]}
  analysis/page-analysis-<pages>-<job>.md
                         per-page analyses ("Analyze pages" below)
  translations/<lang>.txt page-aligned, "--- page N ---" markers (the OCR
                          docs' exact convention, so the same parser reads it)
```

Summarize and the About save both mirror their text into the build's Editor
Description — the public catalog description in the volumes row — so Analyze
output publishes without a second copy/paste step. The summary is a seed for
public metadata, not an internal note; only relevance stays internal.

**Operations** (long ones follow the OCR-job pattern — in-memory job dict,
daemon thread, `GET /api/analyze/job/<id>` polling):

- Summarize: map-reduce over the page sections of the build's OCR text
  (chunked to the model's context), writes `summary.md`.
- Analyze pages: staged analysis of selected OCR pages (`POST
  /api/analyze/pages`), one AI pass per chunk, saved to
  `analysis/page-analysis-<pages>-<job>.md` in the entry folder.
- About draft: writes `about.md` from summary + metadata; editable in a
  Markdown editor in the tab.
- Suggest categories: metadata + summary + the current taxonomy → suggested
  paths (existing or new), each accept-able; "auto-assign" applies all
  existing-path suggestions.
- Translate: per-page over the OCR text into `translations/<lang>.txt`;
  saves as it goes and only translates still-untranslated pages, so an
  interrupted job resumes on re-run.
- Annotate: per-chunk pass proposing anchored notes (page + short quote +
  note); they arrive as `suggested` and are curated (approve/reject/edit) in
  a grid.
- Relevance: scores the work against the custom criteria edited in the
  Analyze tab's Relevance pane
  (`settings.relevanceCriteria = [{id,name,description}]`), writes
  `build.relevance = {assessed_at, model, overall, criteria:[{id,name,score,
  rationale}]}`. **Internal only**: `relevance` never enters `_volume_row`,
  so it never reaches the anon-readable volumes table (it still syncs
  between the user's machines via the service_role-only builds table).

**Bundle interface**: a panel listing the publishable artifacts with include
toggles, stored on the build as
`bundle = {"about": bool, "annotations": bool, "pages_text": bool,
"translations": ["es", ...]}`. Publish uploads exactly what the bundle says:
about → `volume_texts`; original text pages (`pages_text`) and translations →
`volume_pages`; approved annotations → `volume_notes`; and writes the
`volumes.assets` manifest. Republishing prunes artifacts that left the bundle.

## 3. The website library view

The Library is a **separate design** from the About/Docs/Downloads pages — a
serious archival catalogue (`library.css`; the marketing pages keep
`site.css`). Four pages:

- `browse.html` — faceted catalogue: search, category facets (built from
  `volumes?select=slug,category_paths,language,year`), year range, language,
  sort; records rendered as catalogue entries (title, author, imprint line,
  categories, description snippet), linking to their item pages.
- `book.html?slug=…` — the item record: full metadata table, the About
  article (rendered Markdown from `volume_texts`), categories linking back to
  the filtered catalogue, Read / Download actions, and notes/translations
  affordances driven by `volumes.assets`.
- `read.html?slug=…` — the reader: self-hosted pdf.js (vendored under
  `assets/vendor/pdfjs/`, keeping the no-CDN promise) streaming the PDF by
  HTTP Range from R2/Supabase; thumbnails, zoom/fit, keyboard nav, remembered
  position per slug; margin annotations from `volume_notes`; a page-aligned
  text/translation panel from `volume_pages`.
- `author.html?author=…` — the author page (*as built*, after the freeze):
  works grouped on the exact `volumes.authors` string (messy free text —
  never split on delimiters) plus an optional curated bio from
  `author_pages`; the `author_index` view (name + work count) feeds author
  suggestions.

Category filtering uses the flat text rendering:
`categories=ilike.*<path text>*` — because a child path's text begins with
its parent's, filtering by "Botany" matches the whole subtree.

Security stance unchanged: every rendered string through `esc()`, every href
through `safeHttpUrl()`/`pdfHref()`; `volume_texts.body` is Markdown rendered
by a minimal, escaping-first renderer (no raw HTML pass-through).

## What stays private

anon can read: volumes (+ category_paths, assets), volume_texts,
volume_pages, volume_notes, author_pages (+ the author_index view),
releases. Everything else — taxonomy, builds (including `relevance` and
`bundle`), books, ia_catalog, corrections — is service_role only; captures
is writable by signed-in apps but RLS-scoped to its owner, so it is still
never anon-readable.
The rule of thumb: *if it isn't in the bundle, it doesn't leave the desk.*
