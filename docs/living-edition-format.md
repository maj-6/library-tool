# `.whled` living-edition package and entity contract (`whled/0.1`)

Status: **prototype, implemented** (2026-08-12).

This document is normative for the portable data contract. The interaction and
application design is specified separately in
[`living-edition-application-design.md`](living-edition-application-design.md).
The existing [`.lib` version 3](lib-format.md) remains the Library Tool capture
and correction interchange format. A `.whled` file is a distinct scholarly
edition profile with parallel readings, stable citations, entity anchors, and
edition history. Neither extension is an alias for the other.

The guiding rule is simple: **page images are evidence; readings and
interpretations are revisioned claims about that evidence**. Machine output,
human transcription, translation, entity resolution, and commentary can
coexist without overwriting each other.

## 1. Boundaries and design decisions

`whled/0.1` is a sealed ZIP projection for reading, comparison, review, and
exchange. It is not the workbench's mutable event store. A new editorial state
is sealed as a new edition revision. Frozen releases remain byte-addressable and
citable even when the living edition advances.

The package may hold:

- immutable manuscript canvas images;
- any number of region, transcription, translation, entity, knowledge,
  commentary, note, and guided-reprocessing layer revisions;
- a complete review/provenance/history record for each layer revision;
- an optional immutable JSON snapshot of an external authority; and
- catalog metadata sufficient to describe the digital witness.

The package must not hold:

- the mutable plant-authority SQLite database;
- workbench queues, locks, credentials, local paths, API keys, or UI state;
- a model output promoted to approved content without a named human review; or
- an entity interpretation written into the transcription layer.

The prototype chooses JSON and normalized canvas geometry because they are
transparent and durable. It does not try to replace IIIF, Web Annotation,
TEI, ALTO, PAGE XML, or W3C PROV. Stable IDs and explicit mapping boundaries
leave future exporters possible without making any of those larger standards a
hidden dependency of the desktop prototype.

## 2. Archive layout

```text
book.whled
├── manifest.json
├── INSTRUCTIONS.md
├── checksums.sha256
├── schemas/
│   ├── whled-manifest.schema.json
│   └── whled-layer.schema.json
├── canvases/<canvas image>
├── layers/<stable-layer-id>/<revision>.json
├── authority-snapshots/<snapshot>.json       # optional, immutable
└── assets/<declared derivative>               # optional
```

Every file is a regular ZIP member; directory entries and symbolic links are
forbidden. Paths are portable ASCII, relative, case-distinct, and contain only
safe segments. Unknown top-level members are invalid. Every resource below
`canvases/`, `layers/`, `authority-snapshots/`, or `assets/` is declared in
`manifest.resources` with media type, role, byte length, and SHA-256.

`checksums.sha256` covers every member except itself using the conventional
line form:

```text
<64 lowercase hex digits><two spaces><member path>
```

The reference sealer fixes ZIP timestamps, permissions, member order, JSON key
order, indentation, and compression decisions. Identical logical input
therefore produces identical bytes. The source PDF itself is represented by a
catalog/source fingerprint and extracted canvases; it need not be duplicated in
the package.

### 2.1 Safety caps

The reference validator rejects:

- archives larger than 2 GiB;
- more than 20,000 members;
- an individual member larger than 256 MiB;
- total inflated content larger than 4 GiB;
- suspicious compression ratios over 500:1 for members over 1 MiB;
- encrypted, linked, duplicate, case-colliding, absolute, traversing, or
  backslash paths;
- JSON larger than 32 MiB, duplicate keys, non-UTF-8 text, non-finite numbers,
  or nesting deeper than 128; and
- any member ending in `.sqlite`, `.sqlite3`, `.db`, or `.db3`.

These are prototype implementation caps, not promises that every viewer can
render a 2 GiB package interactively. A production profile should add streamed
resource access and a smaller web-delivery rendition.

## 3. Manifest

`manifest.json` uses
`https://worldherblibrary.org/schemas/whled-manifest-0.1.json` and has this
top-level shape:

```json
{
  "$schema": "https://worldherblibrary.org/schemas/whled-manifest-0.1.json",
  "format": "whled/0.1",
  "package_id": "pkg-yale-16156709-herbal",
  "created_at": "2026-08-12T00:00:00Z",
  "generator": "world-herb-library/living-edition-prototype",
  "edition": {},
  "catalog": {},
  "source": {},
  "canvases": [],
  "region_types": [],
  "layers": [],
  "resources": [],
  "authority_snapshots": [],
  "external_authorities": [],
  "registries": {
    "layer_kinds": [],
    "selector_types": [],
    "resource_roles": [],
    "region_relation_predicates": [],
    "capabilities": []
  },
  "capabilities": [],
  "ext": {}
}
```

IDs are opaque portable strings. Revisions are also opaque strings: clients
must never parse them as integers or infer chronology from them. Time, explicit
`supersedes`, history events, and current flags establish sequence.

### 3.1 Edition identity and release state

`edition` contains a stable `id`, immutable `revision`, one of `working`,
`review`, `published`, or `frozen`, a label, named `steward`, nullable
`previous_revision`, and optional release/citation text. A citation names the
edition revision or frozen release, never “latest.” A living URL may resolve to
the current revision but must say when a cited older revision has been
superseded.

### 3.2 Catalog entry metadata

The catalog block distinguishes a witness from its edition and source file. It
requires:

- local catalog `record_id` and title;
- material type, repository, shelf/call number;
- qualified date records and BCP 47 language tags;
- outward identifiers and the repository source URL; and
- rights text, plus an optional license.

It optionally carries alternative titles, contributors with roles, extent,
description, subjects, and other outward identifiers. Values are assertions
about this physical/digital witness; a future catalog model may crosswalk these
to separate work, edition, volume, and copy records.

The initial herbal prototype can truthfully carry the metadata printed in the
source PDF:

- title: *Herbal in prose and verse*;
- repository: Yale University Library;
- call number: `Takamiya MS 46 1`;
- date display: `[ca. 1400-1425]`;
- extent of digitization: completely digitized;
- Yale record: `16156709`; and
- source URL: `https://collections.library.yale.edu/catalog/16156709`.

The rights statement must be copied accurately from the repository record; this
format does not infer a license from age.

### 3.3 Source and canvases

`source` records only portable facts: display filename, media type, checksum,
page count, deliberately excluded page numbers, public source URL when known,
and extraction method. It must not store `C:\...`, `~/...`, or another local
locator.

Each canvas record contains:

```json
{
  "id": "canvas-0007",
  "revision": "sha256-6308f8682cec",
  "sequence": 7,
  "label": "1r",
  "source_page": 8,
  "source_label": "1r Image ID: 16156797",
  "image_member": "canvases/canvas-0007.jpeg",
  "dimensions": {"width": 1160, "height": 2000},
  "ext": {}
}
```

Canvas sequence is a complete, unique, 1-based order. `source_page` retains the
PDF page address. Canvas identity is not a filename or list position. If image
bytes or pixel geometry change, the editor makes a new canvas revision and
remaps or flags selectors; it never mutates evidence behind an existing
revision.

### 3.4 Open registries, capabilities, and generic resources

The core profile is intentionally small. It does not encode each future
analysis, media derivative, selector geometry, or relationship as another
hard-coded manifest property. Five extension registries declare additions:

- `layer_kinds`;
- `selector_types`;
- `resource_roles`;
- `region_relation_predicates`; and
- `capabilities`.

An extension identifier is namespaced (`example.org:layout-analysis`) or begins
with `x-`. Its registry record provides label, description, optional public
schema URI, a portable renderer hint, and `ext`. Extensions cannot redefine a
core identifier. A namespaced value used by the package must be declared; an
undeclared value is treated as a likely typo and rejected.

Resources all use the same generic `{member, media_type, role, sha256, bytes,
ext}` envelope. The format therefore does not need a new top-level property for
spectral images, audio, TEI exports, model feature maps, or later asset types.
A client that does not implement an extension can verify, list, inspect as raw
JSON, and export its resource without interpreting it. It must preserve the
bytes and registry metadata when resealing.

Core layer payloads receive semantic validation. A declared custom layer keeps
the common identity/revision/provenance/dependency/history/review envelope and
an inspectable JSON `data` object; its registered schema may impose additional
rules. This makes new analysis kinds additive without allowing unreviewed
opaque binaries to masquerade as core scholarly content.

`manifest.capabilities` lists features this particular package actually uses.
Core capability IDs are defined by the profile; extension capabilities are
registered with the same self-description. Readers use these as feature flags,
not as permission to skip validation.

## 4. Region geometry, types, flows, and relations

Regions live in a `region` layer revision, not directly in the manifest. This
allows the local pass, Mistral OCR 4, and human editor to retain independent
segmentation hypotheses.

### 4.1 Selectors

Canonical coordinates are floating-point fractions of the exact cited canvas
revision, origin at the top left, x increasing right, y increasing down.

A box:

```json
{
  "type": "box",
  "coordinate_space": "canvas-normalized",
  "canvas_revision": "sha256-6308f8682cec",
  "x": 0.13,
  "y": 0.19,
  "width": 0.58,
  "height": 0.12
}
```

A polygon has the same first three fields and `points`, an ordered list of 3 to
256 `{x,y}` objects. It must have non-zero area. Every selector remains within
0..1. A viewer may derive source-pixel values from canvas dimensions for
drawing, but normalized coordinates remain authoritative and must survive that
round trip without material drift.

A declared custom selector uses the same `type`, `coordinate_space`, and
`canvas_revision`, plus an inspectable `data` object and a required core
box/polygon `fallback`. The fallback is what an unaware viewer draws and what
search/crop tools use. Thus a future curved baseline, Bézier zone, or externally
defined SVG selector is extensible without making a region invisible in an
older safe reader.

### 4.2 Hierarchical, multi-facet region types

`manifest.region_types` is an edition-specific controlled vocabulary. A type
has stable ID, human label, `facet`, nullable parent, description, a `custom`
flag, optional color, and `ext`. Parent and child belong to the same facet and
cycles are invalid.

```json
[
  {"id":"layout:text","label":"Text","facet":"layout","parent_id":null,
   "description":"Written textual material","custom":false,"ext":{}},
  {"id":"layout:marginalia","label":"Marginalia","facet":"layout",
   "parent_id":"layout:text","description":"Text outside the main body flow",
   "custom":false,"ext":{}},
  {"id":"hand:scribal","label":"Scribal hand","facet":"hand",
   "parent_id":null,"description":"A distinguished writer/hand","custom":false,"ext":{}},
  {"id":"hand:hand-b","label":"Hand B","facet":"hand",
   "parent_id":"hand:scribal","description":"Project-defined second hand",
   "custom":true,"ext":{}}
]
```

A region carries an array of `type_ids`, so `layout:marginalia` and
`hand:hand-b` can apply simultaneously. This is why “hand B” is a type
subclass, not a new fixed top-level role. Type changes require a new layer
revision and history event.

Each region also has stable ID, canvas/revision, selector, nullable parent
region, fallback integer `order`, label, optional numeric machine confidence,
and `ext`. `order` is only the simplest single-flow fallback.

### 4.3 Named reading flows

A region layer must carry `reading_flows`, allowing body flow, marginal flow,
columns, or alternative scholarly readings to coexist:

```json
{
  "id": "flow-main",
  "label": "Main body",
  "direction": "ltr",
  "ordered_region_ids": ["region-01", "region-03", "region-04"],
  "ext": {}
}
```

Direction is `ltr`, `rtl`, `ttb`, `btt`, or `mixed`. Region IDs in one flow are
unique and reference that exact region-layer revision. One region may
participate in more than one named flow.

### 4.4 Region relations

`relations` retain structure that an order cannot express:

```json
{
  "id": "relation-gloss-1",
  "subject_region_id": "region-margin-1",
  "predicate": "marginalia-of",
  "object_region_id": "region-body-3",
  "confidence": "certain",
  "ext": {}
}
```

Predicates are open portable identifiers (`marginalia-of`, `continues-at`,
`caption-of`, `interlinear-gloss-of`). A relation is directed, may not point to
itself, and pins regions within one revision. Confidence is one of `certain`,
`likely`, `possible`, `disputed`, or `unresolved`.

## 5. Common layer envelope

Every layer JSON uses
`https://worldherblibrary.org/schemas/whled-layer-0.1.json`:

```json
{
  "$schema": "https://worldherblibrary.org/schemas/whled-layer-0.1.json",
  "id": "transcription-mistral",
  "revision": "mistral-ocr4-run-20260812",
  "kind": "transcription",
  "label": "Mistral OCR 4 diplomatic draft",
  "status": "machine-draft",
  "language": "enm",
  "provenance": {},
  "dependencies": [],
  "history": [],
  "reviews": [],
  "data": {},
  "ext": {}
}
```

The manifest lists every historical layer revision. At most one revision of a
stable layer ID is `current: true`. `variant` distinguishes purposes such as
`diplomatic`, `normalized`, `literal`, or `readable`; it is not identity.

Core layer kinds are `region`, `transcription`, `translation`, `entity`,
`knowledge`, `commentary`, `notes`, and `reprocessing`. Experimental kinds use
an `x-` prefix. Unknown fields belong under bounded `ext` so a validator can
distinguish a declared extension from a typo.

### 5.1 Provenance

Provenance records the actor, timestamp, method, parameters, source evidence,
and `ext`. Actor type is `human`, `software`, `model`, or `import`, with stable
ID, display label, optional version and URI. For OCR this means preserving the
provider, exact model/engine version, settings, preprocessing, source canvas
digest, and time. “AI generated” is not sufficient provenance.

### 5.2 Dependencies and staleness

Dependencies pin exact layer ID and revision plus a relation such as
`segmented-by`, `transcribed-from`, `translated-from`, `mentions-in`, or
`summarizes`. They may not point to a missing revision or to the layer itself.

Changing a source never mutates its dependents. The workbench marks dependent
current layers stale, presents the exact old/new source difference, and asks a
person to reaffirm or revise them. Frozen releases retain their original graph.

### 5.3 History and review

History is chronological and non-empty. Each event identifies action, actor,
timestamp, message, nullable base revision, and extension data. A layer change
creates a new revision; old JSON members stay in the package when history or
citation needs them.

Reviews are append-only records with decision (`approve`, `reject`,
`request-changes`, `abstain`), reviewer, timestamp, rationale, and the exact
layer revision reviewed. Only a human actor may approve or reject. An
`approved` or `frozen` layer requires a human approval of that exact revision.
A model can propose but cannot review itself or another model into publication.

## 6. Layer payloads

### 6.1 Transcription and translation

Both use `data.passages[]`. A passage has stable ID, canvas/revision,
revision-pinned `region_ref`, fallback order, text, optional confidence,
alignment refs, uncertainty spans, and `ext`.

Uncertainty spans use half-open character offsets and one of `illegible`,
`conjectural`, `disputed`, `supplied`, or `deleted`. A diplomatic transcription
keeps what the page says. Normalization is another transcription variant;
modern English is a translation layer. Multiple translations may coexist, such
as `literal` and `readable`, and align to one or more pinned passages rather
than sharing mutable array positions.

### 6.2 Entity mentions and durable triple anchors

An entity layer carries `data.mentions[]`. Each occurrence is unique; identical
strings on two pages are two mentions. A mention retains:

1. the canvas and revision plus a revision-pinned region reference;
2. an optional page selector for a tighter word/illustration polygon; and
3. a text anchor: transcription layer/revision/passage, character range, exact
   string, prefix, suffix, and anchor status.

The exact text length must equal `end - start`, and the package validator checks
it against the pinned passage. Anchor states are `current`, `stale`, `repaired`,
or `ambiguous`. When transcription changes, the editor searches the exact quote
and context inside the durable region. One unambiguous result appends a repaired
anchor; no or multiple results require human review.

`authority_refs` name the external `database_id`, sealed `snapshot_id`, node
type and ID, link role, and assertion IDs. A direct concept foreign key is not
enough: the assertion is the editable, auditable scholarly claim.

### 6.3 Knowledge and commentary

These use `data.entries[]` with stable ID, target URI, title, text, citations,
and `ext`. Knowledge can hold a concise factual/structural layer; commentary
can hold edition-specific interpretation. Neither changes the transcription.
External quotations/citations should remain short, attributable, and
rights-aware.

### 6.4 Notes at book, page, region, and entity scope

Notes use `data.notes[]`: stable ID, target URI, text, author, creation time,
tags, visibility (`private`, `project`, `public`), and `ext`. Scope is determined
by its target, not by placing separate note arrays on every record. Notes can
therefore target the whole book, a canvas, one region, one layer item, or an
external entity node without duplicating the note model.

### 6.5 Guided reprocessing

Reprocessing directives use `data.directives[]` and retain the exact failure a
naive pass needs help with: target URI, instruction, reason, requested output
types, engine constraints, priority, state, author/time, nullable resolution,
and `ext`.

Examples include “use hand B model only inside this polygon,” “exclude the
pressed-plant shadow,” “treat these marks as interlinear additions,” and
“separate this marginal gloss from the body flow.” States are `open`, `queued`,
`running`, `resolved`, and `cancelled`. A reprocessor reads directives as
inputs, writes a new machine layer revision, and attaches its result/diff to
the directive; it never overwrites the human annotation that guided it.

## 7. Stable cross-navigation addresses

The archive and external authority share URI-shaped logical addresses. These
are application routes/identifiers, not filesystem URLs and not promises that
the custom schemes resolve outside a WHL client.

Edition addresses:

```text
whl://book/{book_id}
whl://book/{book_id}/edition/{edition_id}
whl://book/{book_id}/canvas/{canvas_id}
whl://book/{book_id}/canvas/{canvas_id}/region/{region_id}
whl://book/{book_id}/layer/{layer_id}/revision/{revision}/item/{item_id}
whl://book/{book_id}/canvas/{canvas_id}/region/{region_id}/mention/{mention_id}
```

Authority addresses:

```text
whl-entity://name/{id}
whl-entity://concept/{id}
whl-entity://referent/{id}
whl-entity://assertion/{id}
whl-entity://evidence/{id}
whl-entity://review/{id}
```

IDs and each path segment must be percent-encoded by the URI implementation.
The current POC identifiers already fit the portable identifier grammar. A
client resolving a region must know the selected region-layer revision; a
citation pins it explicitly even when a convenience route displays the current
revision.

Cross-navigation works in both directions:

- catalog record → edition → canvas → region → mention;
- mention → written name → competing concept assertions → referents;
- assertion → all evidence and append-only reviews;
- evidence page/region → exact image crop and pinned transcription passage; and
- concept/name/referent → all mentions across witnesses, subject to rights.

No endpoint may infer historical identity by following a chain of likely
links. A UI can show related claims; it cannot synthesize a new claim without a
new assertion record.

## 8. External plant authority and snapshot

The authoritative mutable POC is
`data/plant-authority-poc/plant-authority.sqlite3`, built from `schema.sql` and
`seed.json`. It remains outside the `.whled` package because:

- one authority serves many works;
- the workbench needs transactions, constraints, lookup indexes, and
  append-only reviews;
- a book package must not leak unrelated witness mentions; and
- embedding the database would create untracked stale forks.

The relational model contains:

| Record | Purpose |
|---|---|
| `name_form` | Literal, normalized lookup key, language, script, transliteration, period and place; original spelling is never discarded |
| `mention` + `mention_anchor` | One occurrence with geometry plus revisioned text/context anchors |
| `concept` | Historical meaning scoped by kind, tradition, period and region |
| `referent` | External authority identifier and dated cached display label, or an explicit unresolved referent |
| `assertion` | Reified one-hop subject/predicate/object claim with author/model, method, confidence, state and supersession |
| `evidence` | Page/region, quote, reasoning, or external citation supporting an assertion |
| `review` | Append-only named-human decision and rationale |

SQLite triggers enforce node kinds, legal assertion endpoint pairs, human-only
review, machine proposals staying `proposed`, and append-only assertions,
evidence, reviews, and anchor repairs. The reconciliation view follows exactly
one `historical-name-for` assertion; it does not compute transitive closure.

`snapshot.json` is a deterministic publication projection with database ID,
release, creation time, license, scope note, content digest, complete record
arrays, and a convenience `entities[]` projection. Each plant concept entity
lists all *currently stored and asserted* written forms with language, script,
period, assertion, confidence, and state, plus proposed modern referents. In the
POC this is deliberately not a claim of exhaustive global coverage. “All known
written names” is the target property of the curated authority, and the release
scope/coverage note must state the actual boundary.

A `.whled` entity ref must cite a `snapshot_id` sealed into that package. The
snapshot's `database_id` must match a declared external authority. Updating the
master DB never silently repoints an existing edition. A new snapshot and new
entity layer revision are required.

## 9. Versioning and compatibility

The format tag is `whled/MAJOR.MINOR`.

- A minor revision may add optional fields, layer kinds, region predicates, or
  capabilities. Unknown fields are still invalid unless declared by the newer
  schema or stored under `ext`.
- A major revision may change invariants and must be rejected by an older
  reader.
- `0.x` is explicitly experimental. Before `1.0`, the project must test real
  multi-hand manuscripts, right-to-left and vertical scripts, rotated/cropped
  canvases, entity re-anchoring, large editions, and round trips through the
  actual editor.

A `.lib/3` importer can seed a `.whled` edition by mapping representations to
canvases and artifacts to initial machine layers, but the conversion is not
lossless unless it creates stable layer identities, explicit revisions,
catalog/edition identities, history, and authority snapshot references.

## 10. Reference commands

Build and validate an edition whose resource files mirror their manifest paths
beneath a working directory:

```powershell
python tools/living_edition/build_whled.py build `
  work/manifest.json work/herbal.whled --resource-root work
python tools/living_edition/build_whled.py validate work/herbal.whled
python tools/living_edition/build_whled.py inspect work/herbal.whled
```

Build and export the external authority POC:

```powershell
python tools/living_edition/plant_authority.py init `
  data/plant-authority-poc/plant-authority.sqlite3
python tools/living_edition/plant_authority.py validate `
  data/plant-authority-poc/plant-authority.sqlite3
python tools/living_edition/plant_authority.py export `
  data/plant-authority-poc/plant-authority.sqlite3 `
  data/plant-authority-poc/snapshot.json
```

The reference build is inventory-driven: it reads only declared resources. It
does not recursively zip a work directory, so scratch files, secrets, source
PDFs, and the external SQLite database cannot enter accidentally.

## 11. Prototype acceptance gates

The portable contract is ready to support interface refinement when all of
these remain true:

1. identical logical input seals byte-identically and validates cleanly;
2. checksum tampering, traversal, duplicates, links, and embedded databases are
   rejected before content is trusted;
3. local OCR, Mistral OCR 4, and human region/transcription revisions coexist;
4. boxes and polygons round-trip against pinned canvas revisions;
5. custom hand/layout type hierarchies, named reading flows, and marginalia
   relations survive save/reopen;
6. notes and reprocessing directives retain book/page/region/entity targets;
7. a transcription change stales rather than silently detaches entity anchors
   and translations;
8. a model cannot approve its own output;
9. the package embeds only the cited authority JSON snapshot, never the master
   SQLite database; and
10. every catalog/entity/evidence/review route can navigate back to the exact
    visible region and pinned layer revision.

The staged mockup and application decision gates that precede the full Electron
build are defined in the companion application design specification.
