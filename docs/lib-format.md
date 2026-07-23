# The `.lib` book file — format specification (`lib/1`, `lib/2`, and `lib/3`)

Status: **implemented through `lib/3.0`** (2026-07-22). The legacy Replica
exporter deliberately remains `lib/2`; the Flask-free `libformat` core reads
and writes capture-aware `lib/3`. `lib/1` and `lib/2` imports retain their
existing semantics.

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
libformat.FORMAT_VERSION          # "2.0" (legacy Replica writer)
libformat.CAPTURE_FORMAT_VERSION  # "3.0"
```

- `read_lib`/`write_lib` own the zip layout, caps, and `INSTRUCTIONS.md`/
  `schema.json` generation.
- A `LibDocument(format=(3, 0))` exposes `representations`,
  `artifacts`, and `resources[member]` without any Flask dependency.
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

## 8. `lib/3` — capture-aware representation and artifact graph (normative)

`lib/2` remains the page/region interchange format. It cannot safely describe
a capture-only book whose primary evidence is a set of phone photographs and
derived artifacts. `lib/3.0` adds first-class `representations[]` and
`artifacts[]` to `book.json` while retaining every `lib/2` page, figure,
translation, template, role, and extension member.

The archive layout is:

```text
book.lib
├─ book.json
├─ INSTRUCTIONS.md
├─ schema.json
├─ representations/<portable segments>   # immutable source/rendition bytes
├─ artifacts/<portable segments>         # generated/extracted/review bytes
├─ pages/<N>.json                         # optional legacy Replica pages
├─ assets/img/<name>                      # optional legacy figures
└─ translations/<bcp47>.json              # optional legacy translations
```

A capture-only book has `pages: []`. It is valid when it has at least one
representation. An archive must not use an empty graph and an empty page list
as a content-free shell.

### 8.1 Representation record

```json
{
  "id": "rep-capture-original",
  "revision": "capture-r1",
  "role": "capture-original",
  "media_type": "image/jpeg",
  "member": "representations/capture-original.jpg",
  "content_sha256": "…64 lowercase hex characters…",
  "dimensions": {
    "width": 3024,
    "height": 4032,
    "orientation": 6
  },
  "lineage": [],
  "ext": {}
}
```

- `id` is stable, opaque identity; a filename, list position, or display label
  is never identity.
- `revision` pins the exact state being exported.
- `role` distinguishes such states as `capture-original`,
  `capture-display`, and `corrected-rendition`. It is extensible through the
  portable identifier syntax.
- `member` is the only portable resource address. It must be below
  `representations/`, use portable path segments, exist exactly once in the
  ZIP, and match `content_sha256`.
- Raster media require positive pixel dimensions and EXIF orientation 1–8.
- `lineage[]` contains `{representation_id, representation_revision,
  relation}`. Every local target and target revision must exist in the sealed
  archive. Corrections use relations such as `derived-from` or `rework-of`.

Original representation members are immutable. A crop, perspective
correction, binary adjustment, or other rendition is a new representation
with new bytes, identity/revision, checksum, and revision-pinned lineage.

### 8.2 Artifact record

```json
{
  "id": "artifact-box-4",
  "revision": "box-r5",
  "kind": "spatial-annotation",
  "media_type": "application/json",
  "member": "artifacts/mistral-box-4.json",
  "content_sha256": "…64 lowercase hex characters…",
  "source": {
    "representation_id": "rep-corrected",
    "representation_revision": "capture-r4",
    "canvas_id": "canvas-corrected",
    "canvas_revision": "canvas-r3"
  },
  "provenance": {
    "origin": "ocr",
    "provider_id": "mistral",
    "model": "mistral-ocr",
    "generated_at": "2026-07-22T12:00:00Z",
    "ext": {}
  },
  "category_assignments": [],
  "caption_assertions": [],
  "role_assignments": [],
  "selector": {
    "type": "polygon",
    "coordinate_space": "canvas-normalized",
    "coordinate_space_revision": "canvas-r3",
    "points": [
      {"x": 0.1, "y": 0.2},
      {"x": 0.8, "y": 0.2},
      {"x": 0.8, "y": 0.6},
      {"x": 0.1, "y": 0.6}
    ]
  },
  "relationships": [],
  "ext": {}
}
```

The four primary artifact classes required by Corrections are:

1. `generated-metadata`;
2. `ocr-text`;
3. `spatial-annotation` (OCR/Mistral boxes and polygons);
4. `raster-image` (captured, processed, corrected, extracted, or generated).

The open `kind` vocabulary also carries reusable `transform-recipe` and
`correction-review` artifacts. It may grow additively without changing the
graph machinery.

Every artifact has stable `id`/`revision`, one declared member and checksum,
an exact source representation/revision, provenance, optional raster
dimensions, bounded `ext`, and revision-pinned `relationships[]` of the form
`{artifact_id, artifact_revision, relation}`. Relations such as
`extracted-from`, `derived-from`, and `rework-of` preserve parent/rework
history. A relationship may not point to itself or to a missing/mismatched
local revision.

A captured image is represented by two graph records without duplicating its
bytes: the representation owns the immutable physical member, and a
`raster-image` artifact over that same member owns category, caption, and
other assertions. This is the only shared-member exception. The artifact must
pin the owning representation's exact ID and revision and must have identical
media type, checksum, and dimensions. Representation-to-representation
sharing, artifact-only sharing, mismatched aliases, and case-only member
aliases are invalid.

A polygon selector uses normalized 0–1 coordinates. Its
`coordinate_space_revision` must pin the source canvas revision when one is
present, otherwise the source representation revision. Spatial-annotation
artifacts require a selector. Legacy `{x,y,w,h}` page regions remain in their
unchanged `pages/<N>.json` representation.

### 8.3 Human and machine assertions

Image categories use the separate canonical vocabulary:
`title_page`, `cover`, `spine`, `content_specimen`, and `other`.
`category_assignments[]` records category, origin (`manual`, `inherited`, or
`suggested`), assertion revision, optional confidence/provenance, and the
inherited source when applicable.

`caption_assertions[]` retains machine, imported, inherited, and manual
captions as separate revisioned evidence. A manual caption overrides the
effective display value without deleting the machine caption. Clearing the
manual assertion reveals retained evidence.

`role_assignments[]` similarly retains machine/imported/manual spatial roles.
The stored canonical values are `marginalia` and `figure`; `MAR` and `ILL` are
UI aliases and never enter the archive as new roles.

External tools must preserve all manual assertions and all machine evidence.
Rerunning OCR may add new machine artifacts/assertions but must not overwrite
human values.

### 8.4 Review export policy and excluded runtime state

`book.json.review_policy.mode` is one of:

- `all-durable`: include active attention and resolved audit history;
- `active-only`: include only active attention records;
- `none`: include no `correction-review` artifacts.

Resolved review history, when exported, is a normal revisioned artifact.
Active job state, progress, cancellation tokens, credentials, resource grants,
local paths, UI layout, shortcuts, window state, and remembered image-adjust
brightness are never portable archive data.

### 8.5 Extension and resource safety

`lib/3` fails closed before accepting graph bytes:

- no absolute paths, drive paths, backslashes, `.`/`..` segments, symbolic
  links, duplicate members, case-insensitive aliases, encryption, or
  undeclared members;
- `book.json.pages` contains unique integer page numbers from 1 through 99999,
  and it matches the physical `pages/<N>.json` members exactly;
- representation members stay below `representations/`; artifact-owned
  members stay below `artifacts/`. A byte-identical `raster-image` assertion
  artifact may instead reference its source representation's exact
  `representations/` member under the rules in §8.2;
- each physical resource exists once, is bounded, and matches every permitted
  declaration's SHA-256;
- `book.json.pages` is a unique, bounded list of valid page numbers and must
  exactly match the physical `pages/<N>.json` members;
- the archive, member count, individual resources, and total declared
  inflation are capped before decompression;
- graph fields and nested `ext` data may not contain local paths, URLs,
  filenames, storage keys/locators, opaque live resource references, or other
  private locators;
- non-finite JSON, duplicate object keys, invalid revisions/selectors, missing
  relationship targets, and unbounded extension data are errors.
- Required graph arrays and `ext` objects must be present even when empty.
  Omission is distinct from an empty array/object, and `null` is never a
  substitute. Optional structured fields such as `dimensions`, `selector`,
  assertion provenance, and nested `ext` are validated whenever present.
- Optional string fields are likewise validated whenever present. Empty
  strings are accepted only where the schema explicitly uses
  `optionalPortableId` or `optionalRevision` (and for unconstrained strings);
  `null`, booleans, numbers, arrays, and objects are not omission.

`LibError.code` and `LibError.details` provide a framework-neutral failure
receipt. The engine archive planner exposes the equivalent typed
`ValidationError` codes. Neither path needs Flask.

### 8.6 Version and import behavior

- Reading `lib/1` still produces version `(1, 0)` and the existing importer
  assigns its compatibility book/region identities exactly as before.
- Sealing a `lib/1` or `lib/2` `LibDocument` still writes `lib/2.0`.
- No `lib/2` document is implicitly promoted to `lib/3`; attaching a capture
  graph to an older document is an error. This prevents older readers from
  silently dropping capture evidence.
- A reader may inspect additive `3.x` manifests it understands, but this
  `3.0` writer refuses to re-seal a higher minor as `3.0`; it will not erase
  additions it cannot promise to preserve.
- A `LibDocument(format=(3, 0))` reads/writes the complete graph through
  `LibRepresentation`, `LibArtifact`, and `resources[member]`.
- The current existing-item importer has no canonical raster/spatial
  persistence adapter. It therefore rejects every non-empty `lib/3` graph,
  including graph resource members that are undeclared by empty arrays, with
  `lib3_capture_graph_import_unsupported` before applying pages or discarding
  bytes. Other undeclared `lib/3` members fail with
  `undeclared_lib3_member`. The Flask-free format core can still validate and
  round-trip the archive. A future adapter must consume the exposed parsed
  graph and replace this explicit refusal; it must not route the graph into a
  browser-owned or legacy sidecar.

`INSTRUCTIONS.md` generated for `lib/3` repeats these invariants for external
tools: preserve originals, stable identities, provenance, source revisions,
lineage, checksums, extension data, and human overrides.

### 8.7 Deterministic sealing and capture associations

`libformat.seal_lib(...)` returns the exact bytes written by `write_lib(...)`.
The writer uses fixed ZIP member timestamps, modes, member order, and
compression settings, so the same canonical document, explicit `book_id`, and
generator produce the same archive bytes. Semantic timestamps remain manifest
data supplied by the caller; filesystem wall-clock metadata never changes the
archive digest.

Desktop capture intake associates a sealed archive through the engine's
`org.whl.capture-lib-association` version 1 sidecar. The portable association
contains `capture_id`, stable `book_id`, archive SHA-256 and byte count,
`format_version`, `state` (`current` or `stale`), `generated_at`,
`source_revision`, and a canonical source fingerprint. It never contains a
local path. The archive, association, and replay receipt are one recoverable
publication; a receipt is not successful unless its exact association and
archive remain verifiable.

The initial book identity is UUID5-derived from the canonical capture identity,
so retries and independent LAN/cloud delivery cannot mint a second book. A
later promotion copies this identity. Canonical edits mark the association
`stale` or create an explicit reseal; the archive is a snapshot, never the live
Corrections database.
