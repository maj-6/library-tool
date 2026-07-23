# The `.lib` book file — format spec and the `lib/2` revision plan

Status: **design draft** (2026-07-17). The `.lib` exporter/importer ships on the
`facsimile` branch as format `lib/1`; this document specifies the `lib/2`
revision and the surrounding surface (Python API, docs, desktop integration).
Implementation lands on/after the `facsimile` merge — `lib/1` code is the base.

Design goal, in one sentence: **a `.lib` file must carry everything an external
tool — including an AI assistant with no prior knowledge of Library Tool —
needs to understand, edit, and return it without breaking it.** Dropping a
`.lib` into an assistant and saying *"translate this into Japanese and colorize
the illustrations"* should be sufficient; the file itself teaches the assistant
the structure, the editing rules, and the invariants.

The mutable working-store boundary and ownership of capture images, derived
artifacts, spatial annotations, human assertions, review records, jobs, and UI
profiles are fixed by
[ADR 0001](adr/0001-corrections-workbench-boundary.md). A `.lib` archive is a
sealed interchange projection of that state; it is never the Corrections
workbench's mutable database and never carries client UI-profile state.

---

## 1. What a `.lib` is today (`lib/1`, on `facsimile`)

A ZIP archive:

```
<slug>.lib
├─ book.json          # manifest: format tag, biblio meta, stylesheet,
│                     # templates, figure inventory, page list
├─ pages/<N>.json     # one per region page: dims, state, items[] (regions)
└─ assets/img/<name>  # figure crops referenced by figure regions
```

- A region: `{id, role, src_type, order, box{x,y,w,h 0..1 fractions}, text, norm?}`.
- Roles come from a fixed vocabulary (`tools/layout_roles.py`); furniture roles
  are excluded from the compiled body text.
- Import re-sanitizes everything, caps sizes, and defends against zip-slip and
  deflate bombs; collisions skip-unless-`overwrite` (figures: never overwrite).
- Version marker: the bare string `"format": "lib/1"`, equality-gated.

What it lacks (the `lib/2` motivation): self-description, honest feedback on
what import dropped, stable identity, an extension namespace, and any way for a
third party to learn the rules from the artifact itself.

## 2. `lib/2` — the revision

### 2.1 Self-description: the file teaches its reader

New mandatory members:

```
├─ INSTRUCTIONS.md    # human/LLM-readable: what this is, how to edit it
├─ schema.json        # JSON Schema for book.json and pages/*.json
```

**`INSTRUCTIONS.md`** is the LLM contract. Generated at export, it contains:

1. *What this file is* — one paragraph: a book from a Library Tool archive;
   the zip layout; which member holds what.
2. *The data model* — pages, regions, roles (the full vocabulary **as data**,
   with each role's meaning and whether it is furniture), the two text layers
   (`text` = diplomatic transcription, faithful to the scan; `norm` = the
   modern-edition reading), boxes as 0..1 page fractions, figures and their
   `![id](id)` placeholder linkage.
3. *Editing rules* — the invariants, stated imperatively:
   - Never renumber or rename `pages/<N>.json` — the page number is the key.
   - Never invent roles; use the vocabulary listed here. Custom data goes in
     `ext` (see §2.4), never in new roles or new top-level keys.
   - Translations and modernized text go in each region's `norm` layer (or a
     `translations/<lang>.json` member, §2.5) — never overwrite `text`.
   - Reworked/colorized images: write a **new** file under `assets/img/`, add a
     figure entry with `rework_of: "<original>"` — never replace the original.
   - Do not touch `format_version`, `book_id`, region `rid`s, or `provenance`.
4. *Per-book instructions* — the `instructions.book` text (§2.2) verbatim, e.g.
   "the marginalia in this volume are 18th-century annotations by a later hand;
   keep them attributed and untranslated."
5. *A worked example* — the Japanese-translation + colorize walk-through (§6),
   concretely: which members to read, which keys to write, what the result must
   validate against.

**`schema.json`** is a standard JSON Schema (draft 2020-12) covering both
document shapes, so a tool can validate mechanically without reading prose.

### 2.2 Manifest additions (`book.json`)

```jsonc
{
  "format_version": "2.0",            // replaces "format": "lib/1" (see §2.3)
  "generator": "library-tool/0.8.0",
  "book_id": "b-9f2c…-uuid",          // stable UUID, minted once per book
  "created_at": "…",
  "source": "primary",
  "meta": { …unchanged… },
  "capabilities": ["norm-layer", "templates", "figures", "translations"],
  "roles": {                          // the vocabulary AS DATA
    "body":        { "furniture": false, "note": "main text flow" },
    "marginalia":  { "furniture": true,  "note": "margin notes" },
    "drop-capital":{ "furniture": false, "note": "joins the next region's text" },
    …
  },
  "instructions": {
    "general_ref": "INSTRUCTIONS.md",
    "book": "free text: per-book guidance for editors and AI assistants"
  },
  "stylesheet": { … }, "templates": { … }, "figures": { … }, "pages": [ … ],
  "ext": { }                          // third-party namespace, round-tripped
}
```

The per-book `instructions.book` text is editable in the Replica tab (a small
"Instructions for editors/AI" field) and travels with every export.

### 2.3 Versioning rule

`format_version: "MAJOR.MINOR"`.

- **MINOR is additive.** New optional keys/members only. An importer MUST
  accept any file whose MAJOR it knows, ignoring unknown *declared* additions
  (they still round-trip via §2.4 preservation).
- **MAJOR breaks.** An importer rejects a higher MAJOR with a clear message.
- `lib/1` files remain importable forever: the reader treats `"format": "lib/1"`
  as `format_version: "1.0"` and upgrades on import (mint `book_id`, assign
  `rid`s, default `roles`/`capabilities`).

### 2.4 Stable identity + extension namespace

- **`book_id`**: a UUID minted at first export and persisted in the entry
  folder, so re-exports of the same book carry the same id (third-party tools
  can track a book across revisions).
- **`rid`**: each region gets a short random id (`"rid": "k3f9a2"`), assigned
  once and *preserved* by the sanitizer instead of today's positional
  regeneration. Order stays `order`; identity stays `rid`. External tools can
  then annotate/diff regions across round-trips.
- **`ext`**: any member may carry an `ext: {}` object (manifest-level, page-
  level, region-level). Import **preserves `ext` verbatim** (size-capped),
  export re-emits it. This is the sanctioned home for third-party/AI data —
  everything else unknown is still dropped, but now there is a place that
  isn't.

### 2.5 Translations as first-class members

`translations/<bcp47>.json` — page-aligned translated text keyed by page and
`rid`, matching the app's existing page-aligned translation model:

```jsonc
{ "lang": "ja", "pages": { "7": { "k3f9a2": "翻訳されたテキスト…", … } } }
```

Import routes these into the entry folder's translation store. This gives the
"translate this into Japanese" flow a target that is unambiguous, validatable,
and additive (no `norm` overwrites unless that is what the user wants).

### 2.6 Honest import: the receipt and the linter

Two changes to the import path:

- **Nothing is dropped silently.** The import receipt (already returned as
  JSON) grows a `warnings[]` array: every coerced role, skipped figure,
  truncated text, dropped state flag, and ignored member is named with its
  location and reason.
- **`POST /api/lib/validate`** (multipart, no side effects): runs the same
  sanitize/lint pass and returns the receipt without writing anything —
  external tools and CI can check a `.lib` before shipping it. The same logic
  is exposed in Python as `libformat.validate()` (§3).

Also fixed in `lib/2` semantics: with `overwrite=1`, a figure whose name
collides is replaced **iff** the incoming entry carries `rework_of` naming the
original member or itself — deliberate rework wins, accidental collision still
skips (with a warning).

## 3. The Python API: `tools/libformat.py`

A standalone, Flask-free module beside `libcommon.py`/`layout_roles.py` —
the single implementation both the server routes and external programs use:

```python
import libformat

doc = libformat.read_lib("herbal.lib")          # -> LibDocument (dataclasses)
issues = libformat.validate(doc)                # -> [Issue(level, loc, msg)]
doc.pages[7].items[0].norm = "…"
libformat.write_lib(doc, "herbal-edited.lib")   # seals + validates

libformat.ROLE_VOCAB      # role -> {furniture, note}
libformat.FORMAT_VERSION  # "2.0"
```

- `read_lib`/`write_lib` own the zip layout, caps, and `INSTRUCTIONS.md`/
  `schema.json` generation.
- The `_rw_sanitize_*` functions move here from `server.py` (routes become thin
  wrappers) — one sanitizer, no drift between the API and the app.
- Depends only on the standard library + `libcommon` + `layout_roles`; safe for
  external scripts (`pip`-installable later via the existing `pyproject`).
- This is also the enforcement point for "external processing must not break
  Library Tool": a tool that round-trips through `libformat` cannot produce a
  file the app rejects.

## 4. Documentation

- **`docs/lib-format.md`** (this file) becomes the canonical spec, versioned
  with the format.
- **Website:** a new **API & file format** page (`website/api.html`), linked
  from the docs nav — deliberately separate from the desktop-app and Book
  Capture user docs. Contents: the `.lib` spec, the `libformat` Python API,
  the validate endpoint, and a downloadable **sample `.lib`** fixture (in
  `website/fixtures/`) that tool authors and LLMs can test against.
- The desktop app's Help menu links the same page.

## 5. Desktop integration: icon + file association

- **Icon:** `desktop/build/lib-file.ico` (in-repo now) — a document page with
  a folded corner and a green herb sprig; sizes 16–256 px.
- **Association** (electron-builder, `desktop/package.json`):

```jsonc
"build": {
  "fileAssociations": [{
    "ext": "lib",
    "name": "Library Tool Book",
    "description": "Library Tool book archive",
    "icon": "build/lib-file.ico",
    "role": "Editor"
  }]
}
```

  NSIS then registers the association so Explorer shows the herb icon and
  "Open With → Library Tool" (default).
- **Open flow** (`desktop/main.js`): parse a trailing `*.lib` from
  `process.argv` on first launch; read the `argv` of the existing
  `second-instance` handler (currently discarded); add `app.on("open-file")`
  for macOS; forward the path to the renderer via a preload IPC channel.
- **The UX gap to close:** today's import endpoint targets an *existing* build.
  A double-clicked `.lib` needs a destination chooser: a small dialog offering
  **"Create new book from this file"** (new endpoint: build minted from
  `book.json.meta`, then the normal import) or **"Import into an existing
  book…"** (picker). Create-new is the default.

## 6. The canonical example (what `lib/2` makes possible)

User drops `herbal.lib` into an AI assistant: *"translate this into Japanese
and colorize the illustrations."* The assistant:

1. Unzips; reads `INSTRUCTIONS.md` (+ `schema.json`, and `book.json`'s
   `instructions.book`: e.g. "Latin plant names stay untranslated").
2. Learns: text layers, `rid` addressing, figure rework rules, the ext
   namespace, "never renumber pages".
3. Writes `translations/ja.json` keyed by page + `rid` (leaving `text` and
   `norm` untouched), honoring the per-book note.
4. For each figure: renders a colorized `assets/img/<name>-color.png`, adds a
   figure entry with `rework_of: "<name>"`.
5. Re-zips. The user imports; the receipt reports pages applied, translation
   pages added, figures added — zero warnings. Nothing broke; provenance
   (`src_type`, `rework_of`, untouched `text`) records exactly what the AI did.

## 7. Implementation order (post-`facsimile`-merge)

1. `tools/libformat.py` — lift the sanitizers, add `read/write/validate`,
   `rid` preservation, `ext` round-trip, receipt warnings. (Tests mirror
   `test_layout_regions.py`'s round-trip suite.)
2. `lib/2` export: `format_version`, `book_id`, `roles`, `capabilities`,
   `instructions`, `INSTRUCTIONS.md` + `schema.json` members; `lib/1` upgrade
   path on import.
3. `POST /api/lib/validate` + the receipt UI (import dialog lists warnings).
4. Translations members + the rework-overwrite rule.
5. Desktop: fileAssociations + open-file flow + create-new-book-from-`.lib`.
6. Website `api.html` + sample fixture; per-book instructions field in the
   Replica tab.
